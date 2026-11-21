"""The Muse Spark model path over HTTPS (GoodQA #16441): Meta's Model API or OpenRouter, beside the Claude CLI path.

`MusePort` has the CLI runner's interface (`__call__(prompt) -> claude_cli.RunOut`, `argv()`), so `claude_cli.CappedPort` and
`s2_ports.ResilientPort` run over it unchanged: the durable counter is charged before every request, and the prompt checks, the
throttle and allowance rules, the strict-validity checks and the call log are the same code. `RunOut.stdout` is a JSON envelope in
the CLI's shape (`result`, `is_error`, `usage`, `modelUsage`, `duration_ms`, `num_turns`) plus `throttled`, `limited`, `error`,
`model` (the model id the response reports) and a `muse` block (provider, HTTP status, finish reason, token details, retry-after).
docs/MUSE-PORT.md records the provider facts this module relies on, with their sources.

How a response reaches CappedPort's decisions (CappedPort matches `claude_cli.LIMIT` and `claude_cli.THROTTLE` on stderr):
  * throttled (HTTP 429; 503 overloaded; OpenRouter's 402 in-flight budget): rc 0, `is_error` true and a fixed stderr phrase that
    matches THROTTLE and not LIMIT, so CappedPort raises `StagePaused` and ResilientPort backs off. rc 0 keeps a burst of throttles
    out of CappedPort's consecutive-failure streak (five non-zero exits in a row stop the stage);
  * limited (HTTP 402 billing or credits; OpenRouter's 403 budget limit): rc 1 and a fixed stderr phrase that matches LIMIT, so
    CappedPort raises `AllowanceRefused`;
  * any other failure (5xx, 3xx, malformed JSON, no choices, a model other than the one asked, a timeout): rc 1 (124 for a timeout)
    and a fixed stderr phrase that matches neither. Server text never reaches stderr; the server's error type and code reach the
    envelope only as short identifiers, and are dropped if they would change CappedPort's reading of the call.

One request per call, no retries (the runner owns retry, back-off, the call cap and the journal), no redirects, no proxies, the
system's default TLS verification. Thread-safe: no state is shared between calls (each call reads the key file and builds its own
opener).

The API key comes from exactly one of two sources. `key_file`: read inside this process at construction (to refuse a bad file
before any work) and again at every call (it is never kept on the object); the file must be a regular file (not a symlink),
owned by the running user, mode 0600 or narrower, holding one bare key or one `NAME=value` line whose value starts with the
selected provider's key prefix. `key_secret=(project, secret name)`, both given at run time and never committed: the gcloud CLI
(`gcloud`, an absolute path) runs once per process as a child with a fixed argument vector and no shell (`secrets versions access
latest`), its stdout is held in memory only (a module-level cache, not the object) and checked like a key file's contents, its
stderr is discarded unread, and a failure reports only the exit code or a fixed phrase. The key goes
only into the Authorization header of an HTTPS request to the selected provider's fixed host. It never appears in an exception,
repr, return value, log line, journal field, environment variable, process argument or file; every string this module returns is
scrubbed of it. The `_loopback_test_port` mode exists only for the unit tests' stub server on 127.0.0.1: it sends a fixed dummy
key, never the file's contents.
"""
from __future__ import annotations

import datetime
import email.utils
import http.client
import json
import os
import re
import socket
import shutil
import ssl
import stat
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .claude_cli import LIMIT, SYSTEM_PROMPT, THROTTLE, RunOut

RETRY_AFTER_MAX_S = 10 ** 7          # about 116 days: a longer Retry-After is held here (any run's ceiling is far below)

API_ARGV0 = "muse-chat-completions-api"
LOOPBACK_HOST = "127.0.0.1"
LOOPBACK_DUMMY_KEY = "loopback-test-dummy-key"
MAX_KEY_FILE_BYTES = 4096
MAX_BODY_BYTES = 16 * 1024 * 1024
REASONING_EFFORT = ("minimal", "low", "medium", "high", "xhigh", "max")   # "none" is documented as a 400 on reasoning models
REDACTED = "[REDACTED]"

_KEY_CHARS = re.compile(r"[A-Za-z0-9._~+/=-]{8,512}")
_ASSIGN = re.compile(r"(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)")
_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/:-]{0,127}")
_IDENT = re.compile(r"[A-Za-z0-9_.:/-]{1,128}")
# key shapes, case-sensitive and anchored to the documented forms; applied to server-supplied identifiers, never to the answer
_KEYISH = re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]+|\bLLM_[0-9]+_[A-Za-z0-9._~+/=-]+|\bsk-or-v1-[A-Za-z0-9]+")

# The fixed phrases CappedPort reads (tests assert each matches the regex it must and not the other).
MSG_THROTTLED = "muse api %s: rate limit or overloaded (throttled)"
MSG_LIMITED = "muse api %s: credit balance or usage limit refused (limited)"
MSG_ERROR = "muse api error: %s"


@dataclass(frozen=True)
class Provider:
    name: str
    host: str
    base_path: str
    key_prefix: str
    default_model: str
    max_tokens_field: str
    # response model ids accepted for a requested id beyond the id itself (a documented pinned snapshot of it)
    response_aliases: Mapping[str, Tuple[str, ...]] = field(default_factory=dict)
    # fixed request fields (OpenRouter: one upstream provider, no fallback)
    extra_body: Mapping[str, Any] = field(default_factory=dict)

    @property
    def base_url(self) -> str:
        return "https://%s%s" % (self.host, self.base_path)


PROVIDERS: Dict[str, Provider] = {
    "meta": Provider(
        name="meta", host="api.meta.ai", base_path="/v1", key_prefix="LLM_",
        default_model="muse-spark-1.3-contributor", max_tokens_field="max_completion_tokens"),
    "openrouter": Provider(
        name="openrouter", host="openrouter.ai", base_path="/api/v1", key_prefix="sk-or-v1-",
        default_model="meta/muse-spark-1.3-contributor", max_tokens_field="max_tokens",
        response_aliases={"meta/muse-spark-1.3-contributor": ("meta/muse-spark-1.3-contributor-20260902",)},
        extra_body={"provider": {"order": ["meta"], "only": ["meta"], "allow_fallbacks": False, "require_parameters": True}}),
}


class MuseConfigError(ValueError):
    """A refused configuration: unknown provider, a base_url that is not the provider's documented one, a bad parameter."""


class KeyFileError(MuseConfigError):
    """A refused key file. The message names the path and the reason, never the contents."""


class KeySecretError(MuseConfigError):
    """A refused or failed Secret Manager fetch. The message carries only an exit code or a fixed phrase: never the child's
    output, the project id or the secret name."""


# ---- the key file -----------------------------------------------------------------------------------------------------
def _load_key(path: Path, prefix: str) -> Tuple[Optional[str], str]:
    """(key, "") or (None, reason). Returns instead of raising so that no frame of a raised exception holds the file's bytes."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(str(path), flags)
    except FileNotFoundError:
        return None, "missing"
    except OSError:
        return None, "cannot be opened (a symlink, a directory or unreadable)"
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return None, "not a regular file"
        if stat.S_IMODE(st.st_mode) & ~0o600:
            return None, "mode %04o is wider than 0600" % stat.S_IMODE(st.st_mode)
        if hasattr(os, "getuid") and st.st_uid != os.getuid():
            return None, "not owned by the running user"
        if st.st_size > MAX_KEY_FILE_BYTES:
            return None, "larger than %d bytes" % MAX_KEY_FILE_BYTES
        data = os.read(fd, MAX_KEY_FILE_BYTES + 1)
    finally:
        os.close(fd)
    return _parse_key(data, prefix)


def _parse_key(data: bytes, prefix: str) -> Tuple[Optional[str], str]:
    """One bare key or one NAME=value line, with the provider's prefix and a key's characters. (key, "") or (None, reason)."""
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError:
        text = None                       # decided outside the handler: no exception context holds the bytes
    del data
    if text is None:
        return None, "not ASCII text"
    lines = [l.strip() for l in text.splitlines()]
    del text
    lines = [l for l in lines if l and not l.startswith("#")]
    if not lines:
        return None, "empty"
    if len(lines) > 1:
        return None, "holds more than one entry"
    line = lines[0]
    del lines
    key = line
    m = _ASSIGN.fullmatch(line)
    if m:
        v = m.group(2).strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
            v = v[1:-1]
        if v.startswith(prefix):
            key = v
        del v
    del line, m
    if not key.startswith(prefix):
        return None, "does not hold a key for this provider (expected prefix %s)" % prefix
    if not _KEY_CHARS.fullmatch(key):
        return None, "the key has characters a key cannot have, or a wrong length"
    return key, ""


# ---- the Secret Manager source -----------------------------------------------------------------------------------------
_PROJECT_ID = re.compile(r"[a-z][a-z0-9-]{4,28}[a-z0-9]")
_SECRET_NAME = re.compile(r"[A-Za-z0-9_-]{1,255}")
_SECRET_CACHE: Dict[Tuple[str, str, str, str], str] = {}      # fetched once per process, held in memory only
_SECRET_LOCK = threading.Lock()


def gcloud_argv(gcloud: str, project: str, secret: str) -> List[str]:
    """The fixed argument vector (no shell). It names the secret, never holds its value."""
    return [gcloud, "secrets", "versions", "access", "latest", "--secret", secret, "--project", project, "--quiet"]


def _fetch_secret(gcloud: str, project: str, secret: str, prefix: str, timeout: float) -> Tuple[Optional[str], str]:
    """(key, "") or (None, reason). The child's stdout is held in memory only; its stderr is discarded unread; neither reaches
    the reason, which carries only an exit code or a fixed phrase. Nothing is raised from a frame that holds the value."""
    cache_key = (gcloud, project, secret, prefix)
    with _SECRET_LOCK:
        if cache_key in _SECRET_CACHE:
            return _SECRET_CACHE[cache_key], ""
        why = ""
        out = b""
        try:
            r = subprocess.run(gcloud_argv(gcloud, project, secret), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, shell=False, timeout=timeout, check=False)
            rc, out = r.returncode, r.stdout
            del r
        except subprocess.TimeoutExpired:
            rc, why = None, "gcloud timed out"            # the exception holds partial output: it is dropped with the handler
        except OSError:
            rc, why = None, "gcloud could not be started"
        if why:
            return None, why
        if rc != 0:
            del out
            return None, "gcloud exited with code %d" % rc
        if len(out) > MAX_KEY_FILE_BYTES:
            del out
            return None, "the secret is larger than %d bytes" % MAX_KEY_FILE_BYTES
        key, why = _parse_key(out, prefix)
        del out
        if why:
            return None, "the secret value " + why
        _SECRET_CACHE[cache_key] = key
        return key, ""


def _clear_secret_cache() -> None:
    """For the unit tests only."""
    with _SECRET_LOCK:
        _SECRET_CACHE.clear()


# ---- the port ---------------------------------------------------------------------------------------------------------
class MusePort:
    def __init__(self, key_file=None, model: Optional[str] = None, base_url: Optional[str] = None, timeout: float = 240.0, *,
                 provider: str = "meta", key_secret: Optional[Tuple[str, str]] = None, gcloud: Optional[str] = None,
                 secret_timeout: float = 60.0, max_tokens: Optional[int] = None, reasoning_effort: Optional[str] = None,
                 response_format: Optional[str] = None, accept_response_models: Tuple[str, ...] = (),
                 _loopback_test_port: Optional[int] = None):
        if (key_file is None) == (key_secret is None):
            raise MuseConfigError("give exactly one of key_file or key_secret")
        if provider not in PROVIDERS:
            raise MuseConfigError("provider must be one of %s" % sorted(PROVIDERS))
        self.provider = PROVIDERS[provider]
        p = self.provider
        self.model = p.default_model if model is None else model
        if not isinstance(self.model, str) or not _MODEL_ID.fullmatch(self.model):
            raise MuseConfigError("a model id is required (letters, digits and ._/:- only)")
        self._loopback = _loopback_test_port
        if _loopback_test_port is not None:
            if base_url is not None:
                raise MuseConfigError("the loopback test mode takes no base_url")
            if not (isinstance(_loopback_test_port, int) and 0 < _loopback_test_port < 65536):
                raise MuseConfigError("the loopback test port must be an integer 1 to 65535")
            self._url = "http://%s:%d%s/chat/completions" % (LOOPBACK_HOST, _loopback_test_port, p.base_path)
        else:
            if base_url is not None:
                self._check_base_url(base_url)
            # built from the provider's constants, never from the caller's string
            self._url = "https://%s%s/chat/completions" % (p.host, p.base_path)
        if not (isinstance(timeout, (int, float)) and not isinstance(timeout, bool) and 0 < timeout <= 3600):
            raise MuseConfigError("timeout must be a number of seconds above 0 and at most 3600")
        self.timeout = float(timeout)
        if max_tokens is not None and not (isinstance(max_tokens, int) and not isinstance(max_tokens, bool) and max_tokens >= 1):
            raise MuseConfigError("max_tokens must be a positive integer")
        if reasoning_effort is not None and reasoning_effort not in REASONING_EFFORT:
            raise MuseConfigError("reasoning_effort must be one of %s" % (REASONING_EFFORT,))
        if reasoning_effort == "max" and self.model.endswith("muse-spark-1.3-contributor"):
            raise MuseConfigError("reasoning_effort max is not available on muse-spark-1.3-contributor")
        if response_format not in (None, "json_object"):
            raise MuseConfigError("response_format must be None or json_object")
        self.max_tokens, self.reasoning_effort, self.response_format = max_tokens, reasoning_effort, response_format
        for m in accept_response_models:
            if not isinstance(m, str) or not _MODEL_ID.fullmatch(m):
                raise MuseConfigError("accept_response_models holds an invalid model id")
        self.accepted_models = frozenset((self.model,) + tuple(p.response_aliases.get(self.model, ())) + tuple(accept_response_models))
        self._ssl = ssl.create_default_context()          # verified TLS; one context shared read-only by every call's opener
        self._key_file: Optional[Path] = None
        self._secret: Optional[Tuple[str, str, str, float]] = None
        if key_file is not None:
            self._key_file = Path(key_file)
            key, why = _load_key(self._key_file, p.key_prefix)
            key = None                                      # checked here, read again at every call, never kept
            if why:
                raise KeyFileError("key file %s: %s" % (self._key_file, why))
        else:
            self._secret = self._check_secret_source(key_secret, gcloud, secret_timeout)
            key, why = _fetch_secret(*self._secret[:3], p.key_prefix, self._secret[3])
            key = None                                      # held in the module's in-memory cache only, never on the object
            if why:
                raise KeySecretError("key secret: %s" % why)

    @staticmethod
    def _check_secret_source(key_secret, gcloud, secret_timeout) -> Tuple[str, str, str, float]:
        if not (isinstance(key_secret, (tuple, list)) and len(key_secret) == 2 and all(isinstance(x, str) for x in key_secret)):
            raise KeySecretError("key_secret must be a (project id, secret name) pair")
        project, secret = key_secret
        if not _PROJECT_ID.fullmatch(project):
            raise KeySecretError("key_secret: the project id is not a valid project id")
        if not _SECRET_NAME.fullmatch(secret):
            raise KeySecretError("key_secret: the secret name is not a valid secret name")
        exe = gcloud if gcloud is not None else shutil.which("gcloud")
        if not exe or not os.path.isabs(exe) or not os.path.isfile(exe) or not os.access(exe, os.X_OK):
            raise KeySecretError("key_secret: the gcloud executable must be an absolute path to an executable file")
        if not (isinstance(secret_timeout, (int, float)) and not isinstance(secret_timeout, bool) and 0 < secret_timeout <= 600):
            raise KeySecretError("secret_timeout must be a number of seconds above 0 and at most 600")
        return exe, project, secret, float(secret_timeout)

    def _key(self) -> Tuple[Optional[str], str]:
        if self._key_file is not None:
            return _load_key(self._key_file, self.provider.key_prefix)
        return _fetch_secret(*self._secret[:3], self.provider.key_prefix, self._secret[3])

    def _check_base_url(self, base_url: str) -> None:
        p = self.provider
        try:
            u = urllib.parse.urlsplit(base_url)
            port = u.port
        except (ValueError, TypeError, AttributeError):
            raise MuseConfigError("base_url is not a URL") from None
        if u.scheme != "https":
            raise MuseConfigError("base_url must be https")
        if u.username is not None or u.password is not None or "@" in u.netloc:
            raise MuseConfigError("base_url must not carry user information")
        if u.hostname != p.host:
            raise MuseConfigError("base_url host must be %s for provider %s" % (p.host, p.name))
        if port not in (None, 443):
            raise MuseConfigError("base_url port must be 443")
        if u.query or u.fragment or u.path.rstrip("/") != p.base_path:
            raise MuseConfigError("base_url must be %s" % p.base_url)

    def __repr__(self) -> str:
        return "MusePort(provider=%r, model=%r, host=%r, loopback_test=%r)" % (
            self.provider.name, self.model, self.provider.host if self._loopback is None else LOOPBACK_HOST, self._loopback is not None)

    # ---- what the stamp and every call row record (no secret, no path) ----
    def request_body(self, prompt: str) -> Dict[str, Any]:
        body: Dict[str, Any] = {"model": self.model, "stream": False,
                                "messages": [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": prompt}]}
        if self.max_tokens is not None:
            body[self.provider.max_tokens_field] = self.max_tokens
        if self.reasoning_effort is not None:
            body["reasoning_effort"] = self.reasoning_effort
        if self.response_format is not None:
            body["response_format"] = {"type": self.response_format}
        body.update(json.loads(json.dumps(self.provider.extra_body)))
        return body

    def record(self) -> Dict[str, Any]:
        r = self.request_body("")
        r.pop("messages")
        r.update(api=API_ARGV0, provider=self.provider.name, host=self.provider.host, endpoint=self.provider.base_path + "/chat/completions",
                 accepted_response_models=sorted(self.accepted_models), timeout_s=self.timeout, loopback_test=self._loopback is not None,
                 key_source="file" if self._key_file is not None else "secret_manager")    # the kind only: no path, project or name
        return r

    def argv(self) -> List[str]:
        return [API_ARGV0, json.dumps(self.record(), sort_keys=True)]

    # ---- one call ----
    def _opener(self) -> urllib.request.OpenerDirector:
        """Only what one request needs: no proxy handler, no redirect handler (a 3xx becomes an HTTPError), verified TLS."""
        o = urllib.request.OpenerDirector()
        handlers = [urllib.request.HTTPSHandler(context=self._ssl), urllib.request.HTTPDefaultErrorHandler(),
                    urllib.request.HTTPErrorProcessor()]
        if self._loopback is not None:
            handlers.append(urllib.request.HTTPHandler())
        for h in handlers:
            o.add_handler(h)
        return o

    def _open(self, req: urllib.request.Request, timeout: float):
        return self._opener().open(req, timeout=timeout)

    def __call__(self, prompt: str) -> RunOut:
        t0 = time.monotonic()
        key: Optional[str] = None
        req = None
        try:
            key, why = self._key()
            if why:
                return self._finish(self._error_env("key_file_unavailable"), t0, key)
            send = LOOPBACK_DUMMY_KEY if self._loopback is not None else key
            data = json.dumps(self.request_body(prompt)).encode("utf-8")
            req = urllib.request.Request(self._url, data=data, method="POST", headers={
                "Authorization": "Bearer " + send, "Content-Type": "application/json", "Accept": "application/json",
                "User-Agent": "living-context-evalkit/1"})
            send = None
            status, headers, raw, failure = self._send(req)
            req = None
            if failure:
                return self._finish(self._error_env(failure, rc=124 if failure == "timeout" else 1), t0, key)
            return self._finish(self._classify(status, headers, raw), t0, key)
        except Exception:                                  # never let an exception (and its frames) leave with the key in scope
            return self._finish(self._error_env("internal_error"), t0, key)
        finally:
            key = None
            req = None
            send = None

    def _send(self, req) -> Tuple[int, Any, bytes, str]:
        try:
            resp = self._open(req, self.timeout)
            try:
                return int(resp.status), resp.headers, resp.read(MAX_BODY_BYTES + 1), ""
            finally:
                resp.close()
        except urllib.error.HTTPError as e:
            try:
                raw = e.read(MAX_BODY_BYTES + 1) if e.fp is not None else b""
            except Exception:
                raw = b""
            finally:
                e.close()
            return int(e.code), e.headers, raw, ""
        except (socket.timeout, TimeoutError):
            return 0, None, b"", "timeout"
        except urllib.error.URLError as e:
            if isinstance(e.reason, (socket.timeout, TimeoutError)):
                return 0, None, b"", "timeout"
            return 0, None, b"", "connection_error"
        except (http.client.HTTPException, OSError, ValueError):
            return 0, None, b"", "connection_error"

    # ---- mapping a response ----
    @staticmethod
    def _ident(v: Any) -> Optional[str]:
        if isinstance(v, bool) or v is None:
            return None
        s = str(v)
        return s if _IDENT.fullmatch(s) else "unrecognised"

    @staticmethod
    def _retry_after(headers) -> Optional[int]:
        """Retry-After as whole seconds (RFC 9110 10.2.3): delay-seconds (digits) or an HTTP date (seconds from now, 0 once past).
        A long wait is kept, so the run can stop above its ceiling instead of retrying early (goodqa#16562 F2); a value past
        RETRY_AFTER_MAX_S is held there (still far above any ceiling). Anything else is None."""
        try:
            v = headers.get("Retry-After") if headers is not None else None
        except AttributeError:
            return None
        if v is None:
            return None
        s = str(v).strip()
        if s.isascii() and s.isdigit():
            return min(int(s), RETRY_AFTER_MAX_S)
        try:
            when = email.utils.parsedate_to_datetime(s)
        except (TypeError, ValueError, IndexError, OverflowError):
            return None
        if when is None:
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=datetime.timezone.utc)
        left = (when - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
        return int(min(max(0.0, left), RETRY_AFTER_MAX_S))

    def _env(self, **kw) -> Dict[str, Any]:
        env = {"type": "result", "is_error": True, "result": "", "num_turns": 1, "throttled": False, "limited": False, "error": None,
               "model": None, "usage": {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 0,
                                        "cache_read_input_tokens": 0},
               "modelUsage": {}, "total_cost_usd": None,
               "muse": {"provider": self.provider.name, "requested_model": self.model, "http_status": None}, "_rc": 1}
        muse = kw.pop("muse", {})
        env.update(kw)
        env["muse"].update(muse)
        return env

    def _error_env(self, error: str, rc: int = 1, **muse) -> Dict[str, Any]:
        return self._env(error=error, _rc=rc, muse=muse)

    def _refusal(self, status: int, err: Mapping[str, Any], headers) -> Optional[Dict[str, Any]]:
        """A throttle or an allowance refusal, from an HTTP status or an error code carried in a 200 body; None for anything else."""
        meta = err.get("metadata") if isinstance(err.get("metadata"), dict) else {}
        limit_source = meta.get("limit_source")
        muse = {"http_status": status, "retry_after_s": self._retry_after(headers), "error_type": self._ident(err.get("type")),
                "error_code": self._ident(err.get("code")), "limit_source": self._ident(limit_source)}
        is_or = self.provider.name == "openrouter"
        if status == 429 or status == 503 or (is_or and status == 402 and limit_source == "openrouter_in_flight_budget"):
            return self._env(throttled=True, error="throttled", _rc=0, muse=muse)
        if status == 402 or (is_or and status == 403 and limit_source):
            return self._env(limited=True, error="limited", _rc=1, muse=muse)
        return None

    def _classify(self, status: int, headers, raw: bytes) -> Dict[str, Any]:
        if 300 <= status < 400:
            return self._error_env("redirect_refused", http_status=status)
        if len(raw) > MAX_BODY_BYTES:
            return self._error_env("response_too_large", http_status=status)
        try:
            body = json.loads(raw.decode("utf-8")) if raw else None
        except (ValueError, UnicodeDecodeError):
            body = None
            bad_json = True
        else:
            bad_json = False
        err = body.get("error") if isinstance(body, dict) and isinstance(body.get("error"), dict) else {}
        if not (200 <= status < 300):
            r = self._refusal(status, err, headers)
            if r is not None:
                return r
            return self._error_env("http_%d" % status, http_status=status, error_type=self._ident(err.get("type")),
                                   error_code=self._ident(err.get("code")))
        if bad_json or not isinstance(body, dict):
            return self._error_env("malformed_json", http_status=status)
        choices = body.get("choices")
        ch = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else None
        cherr = ch.get("error") if ch is not None and isinstance(ch.get("error"), dict) else {}
        embedded = err or cherr or ({"code": None} if ch is not None and ch.get("finish_reason") == "error" else {})
        if embedded:                                        # an error reported inside a 200 (OpenRouter: during generation)
            code = embedded.get("code")
            if isinstance(code, int) and not isinstance(code, bool):
                r = self._refusal(code, embedded, headers)
                if r is not None:
                    r["muse"]["embedded"] = True
                    return r
            return self._error_env("provider_error", http_status=status, embedded=True, error_code=self._ident(code),
                                   error_type=self._ident(embedded.get("type")))
        if ch is None:
            return self._error_env("missing_choices", http_status=status)
        msg = ch.get("message")
        if not isinstance(msg, dict):
            return self._error_env("missing_message", http_status=status)
        reported = body.get("model")
        reported_s = reported if isinstance(reported, str) and _MODEL_ID.fullmatch(reported) else None
        if reported_s is None or reported_s not in self.accepted_models:
            return self._error_env("model_mismatch", http_status=status,
                                   response_model=reported_s or ("missing" if reported is None else "unrecognised"))
        content = msg.get("content")
        text = content if isinstance(content, str) else ""
        u = body.get("usage") if isinstance(body.get("usage"), dict) else {}
        n = lambda d, k: int(d.get(k)) if isinstance(d, dict) and isinstance(d.get(k), (int, float)) and not isinstance(d.get(k), bool) and d.get(k) >= 0 else 0
        prompt_t, out_t = n(u, "prompt_tokens"), n(u, "completion_tokens")
        cached = min(n(u.get("prompt_tokens_details"), "cached_tokens"), prompt_t)
        cost = u.get("cost") if isinstance(u.get("cost"), (int, float)) and not isinstance(u.get("cost"), bool) else None
        return self._env(
            is_error=False, result=text, model=reported_s, _rc=0, total_cost_usd=cost,
            # CLI semantics: input_tokens is the uncached part, so input + cache_read = prompt_tokens (ASSUMPTION, docs/MUSE-PORT.md)
            usage={"input_tokens": prompt_t - cached, "output_tokens": out_t, "cache_creation_input_tokens": 0,
                   "cache_read_input_tokens": cached},
            modelUsage={reported_s: {}},
            muse={"http_status": status, "finish_reason": self._ident(ch.get("finish_reason")), "content_null": content is None,
                  "refusal": isinstance(msg.get("refusal"), str) and bool(msg.get("refusal")), "response_id": self._ident(body.get("id")),
                  "prompt_tokens": prompt_t, "completion_tokens": out_t, "total_tokens": n(u, "total_tokens"),
                  "reasoning_tokens": n(u.get("completion_tokens_details"), "reasoning_tokens"), "usage_missing": not u,
                  "upstream_provider": self._ident(body.get("provider"))})

    # ---- the RunOut, scrubbed and consistent with CappedPort's reading ----
    @staticmethod
    def _scrub(s: str, key: Optional[str], shapes: bool = True) -> str:
        """The exact file key and the test dummy always; key-shaped strings too unless `shapes` is off (the model's answer)."""
        for secret in (key, LOOPBACK_DUMMY_KEY):
            if secret:
                s = s.replace(secret, REDACTED)
        return _KEYISH.sub(REDACTED, s) if shapes else s

    @classmethod
    def _scrub_env(cls, v: Any, key: Optional[str], shapes: bool = True) -> Any:
        """Scrub every string of the envelope before it is serialised (never the JSON text, which a pattern could break)."""
        if isinstance(v, dict):
            return {cls._scrub(str(k), key): cls._scrub_env(x, key, shapes and k != "result") for k, x in v.items()}
        if isinstance(v, list):
            return [cls._scrub_env(x, key, shapes) for x in v]
        if isinstance(v, str):
            return cls._scrub(v, key, shapes)
        return v

    def _finish(self, env: Dict[str, Any], t0: float, key: Optional[str]) -> RunOut:
        latency = time.monotonic() - t0
        rc = env.pop("_rc")
        env["duration_ms"] = int(latency * 1000)
        if env["throttled"]:
            stderr = MSG_THROTTLED % env["muse"].get("http_status")
        elif env["limited"]:
            stderr = MSG_LIMITED % env["muse"].get("http_status")
        elif env["is_error"]:
            stderr = MSG_ERROR % env["error"]
        else:
            stderr = ""
        stdout = json.dumps(self._scrub_env(env, key), sort_keys=True)
        plain_error = env["is_error"] and not env["throttled"] and not env["limited"]
        misread = lambda s: bool(THROTTLE.search(s) or LIMIT.search(s))
        if plain_error and misread(stdout):
            # CappedPort searches stdout of a failed call: a server-supplied identifier (or even a number such as a 429 ms
            # latency) would make it read a plain error as a throttle or a limit. Drop the identifiers, then everything optional.
            for k in ("error_type", "error_code", "limit_source", "response_model"):
                if env["muse"].get(k) is not None:
                    env["muse"][k] = "withheld"
            stdout = json.dumps(self._scrub_env(env, key), sort_keys=True)
            if misread(stdout):
                env = self._env(error=env["error"], muse={"withheld": True})
                env.pop("_rc")
                env["duration_ms"] = None
                stdout = json.dumps(self._scrub_env(env, key), sort_keys=True)
        return RunOut(rc, stdout, self._scrub(stderr, key), latency)
