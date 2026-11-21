# Muse Spark model port (GoodQA #16441)

`evalkit/muse_api.py` lets the experiment runner make its model calls to Meta's Muse Spark (Contributor tier) over HTTPS,
either on Meta's own Model API or through OpenRouter, instead of through the Claude CLI. `MusePort` has the CLI runner's
interface (`__call__(prompt) -> RunOut`, `argv()`), so `claude_cli.CappedPort` and `s2_ports.ResilientPort` run over it
unchanged. This file records the provider facts the module relies on, the decisions it makes and how to wire and pilot it.

## Provider facts

Read on 2026-11-20 from each provider's own documentation (developer.meta.com/ai redirects to dev.meta.ai, a Meta domain).
None of the pages shows a date. The pages were read through a fetch tool that summarises, so the wording below is a
paraphrase, not a quotation. Confidence: high means the page states it directly; medium means it is inferred from a schema
or an example; "not documented" means the page is silent and nothing here fills the gap from third-party sources.

### Meta Model API

| Fact | Value | Source | Confidence |
|---|---|---|---|
| Base URL, endpoint | `https://api.meta.ai/v1`, `POST /chat/completions` (OpenAI-compatible; the OpenAI SDK works with this `base_url`) | https://dev.meta.ai/docs, https://dev.meta.ai/docs/api-reference/chat-completions/create-chat-completion, https://dev.meta.ai/docs/protocols/chat-completions | high |
| Auth | `Authorization: Bearer <key>`; keys start `LLM_`, then a numeric id and the secret; keys belong to a team | https://dev.meta.ai/docs/authentication | high |
| Contributor model ids | `muse-spark-1.3-contributor` (1.3 is "recommended for new work"), `muse-spark-1.2-contributor` | https://dev.meta.ai/docs/muse-spark | high |
| Request | `model`, `messages`, `max_completion_tokens` (`max_tokens` deprecated), `reasoning_effort` (minimal to max; default high on Spark; `max` not available on `muse-spark-1.3-contributor`; `none` is a 400), `response_format`, `stream`; only `n=1` | https://dev.meta.ai/docs/api-reference/chat-completions/schemas, https://dev.meta.ai/docs/muse-spark | high |
| Response | `choices[0].message.content` (and optional `reasoning_content`), `finish_reason` in stop, length, tool_calls, content_filter, function_call; `model`; `usage.prompt_tokens`, `completion_tokens`, `total_tokens`, `prompt_tokens_details.cached_tokens`, `completion_tokens_details.reasoning_tokens` | https://dev.meta.ai/docs/api-reference/chat-completions/schemas | high |
| Rate limits, Contributor | 150 requests and 3,000,000 tokens per minute per model, per team (all keys of a team share them); standard Spark is 3,000 RPM and 4M TPM | https://dev.meta.ai/docs/pricing-rate-limits | high |
| Price, Contributor | $0.10 input, $0.002 cached input, $0.20 output per million tokens (standard: $1.25, $0.15, $4.25); pay as you go | https://dev.meta.ai/docs/pricing-rate-limits | high |
| Rate limit signal | HTTP 429, type `rate_limit_error`, code `rate_limit_exceeded`, a `Retry-After` header | https://dev.meta.ai/docs/error-handling | high |
| Billing or quota signal | HTTP 402, type `billing_error` (example code `billing_not_configured`; insufficient balance or a lapsed payment method). No separate quota error is documented | https://dev.meta.ai/docs/error-handling | high |
| Other errors | body `{"error": {message, type, param, code}}`; 401 `invalid_api_key`; 403 no permission for the model; 503 `service_overloaded`, `backend_unavailable`, `server_shutting_down` with `Retry-After`; 504 `gateway_timeout` for a long non-streaming request | https://dev.meta.ai/docs/error-handling | high |
| Context, max output | context 1,048,576 tokens; Spark's maximum output is not documented (`max_completion_tokens` default is model-dependent, counts reasoning tokens, and a value over the maximum is a 400) | https://dev.meta.ai/docs/muse-spark, https://dev.meta.ai/docs/protocols/chat-completions | high / not documented |
| Structured output | `response_format: {"type": "json_schema", "json_schema": {name, schema, strict}}`; `json_object` is in the schema, but `json_schema` is preferred | https://dev.meta.ai/docs/structured-output | high |
| Redirects | not documented | | |
| Contributor data use | your prompts, code, files and context may be used to train future Meta models, in exchange for the lower price; the mode is meant for prototyping and testing. Retention and content restrictions are not documented | https://dev.meta.ai/docs/pricing-rate-limits (Contributor mode section) | high |

### OpenRouter

| Fact | Value | Source | Confidence |
|---|---|---|---|
| Base URL, endpoint | `https://openrouter.ai/api/v1`, `POST /chat/completions` | https://openrouter.ai/docs/api/reference/authentication, https://openrouter.ai/docs/api/reference/overview | high |
| Auth | `Authorization: Bearer <key>`; keys are `sk-or-v1-` followed by 64 hex characters | https://openrouter.ai/docs/api/reference/authentication | high |
| Model id | request `meta/muse-spark-1.3-contributor`. Its endpoint is named `meta/muse-spark-1.3-contributor-20260902` (sole provider Meta, tag `meta`, context 1,048,576, max completion 943,718 tokens, $0.10 / $0.20 per million, cache read $0.002) | https://openrouter.ai/meta/muse-spark-1.3-contributor, https://openrouter.ai/api/v1/models/meta/muse-spark-1.3-contributor/endpoints | high |
| Pinned snapshot | the docs call `canonical_slug` the permanent reference to a model checkpoint, and say the response `model` field is the versioned name, which is "not reliably comparable" to the request slug. They do not say that a dated name is accepted as the request `model` | https://openrouter.ai/docs/api_reference/models, https://openrouter.ai/docs/guides/features/broadcast | medium |
| No other model | model fallback is triggered by a `models` array. The docs do not state that a request with a single `model` never falls back | https://openrouter.ai/docs/guides/routing/model-fallbacks | medium |
| One upstream provider | `provider: {order: [slug], allow_fallbacks: false}`; also `only`, `require_parameters`; `data_collection` defaults to `allow` | https://openrouter.ai/docs/features/provider-routing | high |
| Errors | body `{"error": {code, message, metadata}}`, with the HTTP status equal to `error.code`. 402 insufficient credits; 408 timeout; 429 rate limited (`Retry-After` may be sent; `metadata.provider_code` marks an upstream limit); 502 model down or invalid upstream response; 503 no provider meets the routing requirements (`Retry-After`); 403 moderation, guardrail or budget limit (`metadata.limit_source`) | https://openrouter.ai/docs/api-reference/errors | high |
| Credit exhaustion | 402 with `metadata.limit_source`: `openrouter_in_flight_budget` means wait for `Retry-After` and retry; `openrouter_key_limit` and `openrouter_credits` do not clear by waiting | https://openrouter.ai/docs/api/reference/limits | high |
| Errors inside a 200 | an error during generation can come back as HTTP 200, with `choices[0].error` or an `error` object and no choices, and `finish_reason` `error` | https://openrouter.ai/docs/api-reference/errors | high |
| Rate limit for this model | none documented for paid models ("additional accounts or API keys will not affect your rate limits"); Meta's own team limit for the upstream call is not described by OpenRouter | https://openrouter.ai/docs/api/reference/limits | not documented |
| Usage | `prompt_tokens`, `completion_tokens`, `total_tokens`, `prompt_tokens_details.cached_tokens`, `completion_tokens_details.reasoning_tokens`, `cost`; always included | https://openrouter.ai/docs/guides/administration/usage-accounting | high |
| Data logging default | provider routing defaults to `data_collection: allow`. OpenRouter's own prompt-logging default is not stated on the logging page read | https://openrouter.ai/docs/guides/privacy/logging | not documented |

Not documented by either provider: whether `prompt_tokens` includes `cached_tokens` (the OpenAI convention is that it does), and
whether a quota sent as a 429 can be told apart from an ordinary throttle (it cannot, from the documented fields).

## What the port does

- **Provider choice.** `provider="meta"` or `provider="openrouter"`, nothing else. Each has a fixed host (`api.meta.ai`,
  `openrouter.ai`), base path, key prefix and default model. The request URL is built from these constants. `base_url` is
  optional and, if given, must be exactly the provider's documented base (https, that host, port 443, no user information,
  query or fragment), otherwise construction fails before any request.
- **Key file.** Read inside the process at construction (to refuse a bad file early) and again at every call (the key is never
  kept on the object). The file must be a regular file (a symlink is refused), owned by the running user, mode 0600 or 0400,
  with one bare key or one `NAME=value` line (`export` and quotes allowed, `#` comments ignored). The key must start with the
  provider's prefix (`LLM_` for Meta, `sk-or-v1-` for OpenRouter), so a key file for one provider is refused for the other, at
  construction and at every call. Errors name the path and the reason, never the contents.
- **Secret Manager source.** Instead of `key_file`, `key_secret=(<project>, <secret name>)` with `gcloud=<absolute path to
  gcloud>`; exactly one of the two sources is accepted. Both names are given at run time and are not in any committed file. The
  port runs `gcloud secrets versions access latest --secret <name> --project <project> --quiet` as a child process with that
  fixed argument vector, no shell and stdin closed, once per process (later ports and calls use an in-memory module cache, not
  an attribute of the object). Its stdout is checked like a key file's contents (one key, the provider's prefix, a key's
  characters); its stderr is discarded unread. A failure reports only the exit code or a fixed phrase ("gcloud timed out",
  "gcloud could not be started"), never the child's output, the project or the secret name. The key is never on an argument
  vector or in the environment. `argv()` records only `key_source: secret_manager`.
- **Key exposure.** The key goes only into the Authorization header of the HTTPS request to the selected host. The opener has
  no proxy handler and no redirect handler (a 3xx is an error and is not followed) and uses the default verified TLS context.
  Every string the port returns is scrubbed of the key, of the test dummy and of anything shaped like a Meta or OpenRouter key
  or a bearer header. Server error text never reaches stderr or the envelope; error types and codes do, as short identifiers.
- **One request per call, no retries.** The runner owns retry, back-off, the call cap and the journal.
- **Thread-safe.** No state is shared between calls (each call reads the key file and builds its own opener).
- **Pinned model.** The response's `model` must be the requested id, or a pinned snapshot name listed for it (on OpenRouter:
  `meta/muse-spark-1.3-contributor-20260902` for `meta/muse-spark-1.3-contributor`), or one named in `accept_response_models`.
  Anything else is the error `model_mismatch`, so a silent re-pointing of the alias stops the stage instead of mixing models.
  OpenRouter requests carry `provider: {order: ["meta"], only: ["meta"], allow_fallbacks: false, require_parameters: true}`
  and no `models` or `route` field. `data_collection: deny` is not set: the Contributor tier collects data by design, so it would
  leave no eligible provider.

### The envelope (`RunOut.stdout`) and how CappedPort reads it

The JSON keeps the CLI's fields (`type`, `is_error`, `result`, `usage`, `modelUsage`, `num_turns`, `duration_ms`,
`total_cost_usd`) and adds `throttled`, `limited`, `error`, `model` (the model the response reports) and a `muse` block
(provider, requested model, HTTP status, finish reason, response id, raw token counts, reasoning tokens, retry-after,
error type and code).

| Answer | rc | stderr (fixed text) | CappedPort |
|---|---|---|---|
| 200 with an answer | 0 | empty | parses `result` as usual |
| 429; 503; OpenRouter 402 in-flight budget; the same codes inside a 200 | 0, `is_error` | matches `THROTTLE`, not `LIMIT` | `StagePaused`, ResilientPort backs off |
| 402; OpenRouter 403 with `limit_source`; the same inside a 200 | 1 | matches `LIMIT` | `AllowanceRefused` |
| 3xx, other 4xx and 5xx, malformed JSON, no choices, wrong model, connection error | 1 | matches neither | failed call; five in a row stop the stage |
| timeout | 124 | matches neither | failed call |

A throttle returns rc 0 on purpose: CappedPort counts every non-zero exit towards its five-failure streak before it looks for a
throttle, so at 150 requests per minute a burst of 429s across 32 threads would otherwise stop the stage instead of pausing it.
A 503 is treated as a throttle, like the CLI's 529 "overloaded". Because CappedPort also searches the stdout of a failed call, the
port drops any server identifier (and, as a last resort, every optional field) that would make a plain error read as a throttle
or a limit.

Token mapping (ASSUMPTION, not documented): `input_tokens = prompt_tokens - cached_tokens` and
`cache_read_input_tokens = cached_tokens`, so the existing totals (`input + cache_creation + cache_read`) equal `prompt_tokens`.
`output_tokens = completion_tokens`, which includes reasoning tokens (Meta documents `max_completion_tokens` as counting them).
A `null` content or a `length` stop is an empty `result` (a parse failure in CappedPort), with `finish_reason` recorded.

## Wiring (for the runner owner)

```python
from evalkit import muse_api
runner = muse_api.MusePort(key_file, model=None, timeout=240.0, provider="meta",   # or "openrouter"
                           max_tokens=<stated per stage>, reasoning_effort=<stated per stage>)
# or, with the key in Secret Manager (names passed at run time, for example from flags, never committed):
runner = muse_api.MusePort(model=None, timeout=240.0, provider="openrouter", key_secret=(project, secret_name),
                           gcloud=shutil.which("gcloud"), max_tokens=<stated per stage>, reasoning_effort=<stated per stage>)
port = claude_cli.CappedPort(runner, budget, run_dir / "calls.jsonl", model_id=runner.model, argv_record=runner.argv())
```

`model=None` takes the provider's default (`muse-spark-1.3-contributor` on Meta, `meta/muse-spark-1.3-contributor` on
OpenRouter). `argv()` records the provider, host, endpoint, model, accepted response models and every fixed request field; it
holds no key and no path. Leave `_loopback_test_port` out of every runner flag: it exists only for the unit tests.
`max_tokens` and `reasoning_effort` are left to the server's defaults when not given (default effort on Spark is high; reasoning
tokens are billed as output). As with the earlier API path, a registered stage should state both.

## Pilot (one real call, after the key file is in place)

Python 3.11 or later (the package uses `tomllib`), from the repository root, with the key file at mode 0600:

```sh
python3 -c 'import json, sys; from evalkit.muse_api import MusePort; r = MusePort(sys.argv[1], provider=sys.argv[2], max_tokens=256, reasoning_effort="low")("Reply with exactly {\"ok\": true}"); e = json.loads(r.stdout); print(r.rc, r.stderr, e["result"], e["model"], e["usage"], e["muse"])' <key file> meta
```

With the OpenRouter key in Secret Manager:

```sh
python3 -c 'import json, shutil, sys; from evalkit.muse_api import MusePort; r = MusePort(provider="openrouter", key_secret=(sys.argv[1], sys.argv[2]), gcloud=shutil.which("gcloud"), max_tokens=256, reasoning_effort="low")("Reply with exactly {\"ok\": true}"); e = json.loads(r.stdout); print(r.rc, r.stderr, e["result"], e["model"], e["usage"], e["muse"])' <project> <secret name>
```

Expected: rc 0, an empty stderr, `{"ok": true}`, the model id, non-zero token counts. This call is not charged to a stage counter.
Two things only the pilot settles: whether Meta's API echoes the requested model id exactly (if not, every call is
`model_mismatch`; `accept_response_models` is the remedy once the reported id is known), and whether OpenRouter accepts the
`meta` provider slug and the routing block (a persistent 503 there is a routing problem that reads as a throttle).

## Tests

`python3 -m unittest tests.unit.test_muse_api` (stub server on 127.0.0.1, no network). They cover both providers: a normal
answer, 429, 503, 402 and OpenRouter's in-flight and budget refusals, 5xx, malformed JSON, missing choices, errors inside a 200,
a wrong model, a null answer, a timeout, every 3xx (and that no second request arrives), foreign and plain-http base URLs, the key
file checks, a key file of one provider refused for the other (also when the file is swapped after construction), server text
that imitates a throttle, the throttle, limit and failure-streak paths through the real `CappedPort`, secrets the server echoes in
any field, a key-like answer left intact, the Secret Manager source with a fake gcloud in a temporary directory (fixed argument
vector, one fetch per process, a failing, hanging or wrong-provider fetch, the value in no exception, repr, argv, log or
environment), and 16 threads each getting their own answer. Breaking the host check, the redirect refusal or the scrubbing turns the
suite red.

## Limits

- The timeout applies to each socket operation, not to the whole call; Meta ends long non-streaming calls itself with a 504.
- Proxies are not used (a host that needs one fails visibly with `connection_error`).
- Streaming is not used.
