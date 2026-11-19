# Decision record: probe P6 (API key invalidation latency)

Probe: P6
Verdict: LIMITED
Decided by: main, ASSUMPTION (main's decision, not the user's words): accepted as a stated limit; the 5 s bound of T7 holds.

Measured 1.01 s from invalidation to the first refusal on a single node, against an expected figure under 1 s. The plan's fallback is to report the measured latency as a limit; the security bound (T7) is 5 s and is met.

Mitigation: revocation does not wait for key invalidation; the gateway is the only client and drops its client for a revoked tenant at once, so the Elasticsearch-side latency bounds only a leaked key, not a revoked principal. The figure is reported as a limit in DESIGN 20 and PROBES.md. Multi-node latency is not measured.
