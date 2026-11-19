"""Standard-library HTTPS client for Elasticsearch (DESIGN 19; BUILD-PLAN 1.1).

No third-party package holds a credential. Credentials are read from files by
path (mode 0600 enforced), kept in memory only as an authorization header
value, and never placed in a URL, argv, environment value, log line or
exception text.
"""
from __future__ import annotations

import base64
import json
import ssl
import urllib.error
import urllib.request
from typing import Any, Optional

from ..config import read_secret_file


class EsError(Exception):
    """Transport failure. Carries no response body and no credential."""


class EsClient:
    def __init__(self, url: str, ca_path: str, credential_path: str, scheme: str = "apikey",
                 user: Optional[str] = None, timeout: float = 30.0):
        """`scheme` is "apikey" (the file holds the encoded key) or "basic" (the file holds
        the password of `user`)."""
        if not url.startswith("https://"):
            raise EsError("Elasticsearch is reached over TLS only")
        secret = read_secret_file(credential_path)
        if scheme == "apikey":
            self._auth = "ApiKey " + secret
        elif scheme == "basic":
            if not user:
                raise EsError("basic scheme needs a user name")
            self._auth = "Basic " + base64.b64encode(f"{user}:{secret}".encode()).decode()
        else:
            raise EsError("unknown scheme")
        self._base = url.rstrip("/")
        self._ctx = ssl.create_default_context(cafile=ca_path)
        self._timeout = timeout

    def request(self, method: str, path: str, body: Any = None, *, ndjson: bool = False) -> tuple[int, Any]:
        """Return (status, parsed JSON or text). Non-2xx statuses are returned, not raised."""
        if ndjson:
            data = ("\n".join(json.dumps(x) for x in body) + "\n").encode()
            ctype = "application/x-ndjson"
        else:
            data = None if body is None else json.dumps(body).encode()
            ctype = "application/json"
        req = urllib.request.Request(self._base + path, data=data, method=method,
                                     headers={"Authorization": self._auth, "Content-Type": ctype})
        try:
            with urllib.request.urlopen(req, timeout=self._timeout, context=self._ctx) as resp:
                status, raw = resp.status, resp.read()
        except urllib.error.HTTPError as e:
            status, raw = e.code, e.read()
        except (urllib.error.URLError, OSError, ssl.SSLError) as e:
            raise EsError(type(e).__name__) from None  # no message: it could echo a URL
        text = raw.decode("utf-8", "replace")
        try:
            return status, json.loads(text) if text else {}
        except ValueError:
            return status, text
