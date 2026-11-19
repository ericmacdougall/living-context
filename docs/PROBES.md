# Platform probe results (stub-first build)

Probes live in `tools/probes/`, each prints one JSON verdict line, and the
result of the run on the build host (Linux 6.8, ext4, Python 3.12) is in
`tools/probes/results/`. Ids here are the build's own (LC-PA to LC-PC); LC-PC
is BUILD-PLAN P5 extended.

| Id | Probe | Result | Effect on the design |
|---|---|---|---|
| LC-PA | atomic exchange of two names (`renameat2` with `RENAME_EXCHANGE`) from Python 3.12 | PASS. The stdlib has no wrapper; both `libc.renameat2` and the raw syscall (x86_64 316, aarch64 276) work through `ctypes`. 20,000 exchanges under 4 concurrent readers: no missing name, no content other than one of the two, sizes unchanged. An invalid flag is refused (control). | DESIGN 4.4 compaction is implementable as written on this filesystem. Provisioning must still run the exchange test on the journal directory of each deployment. |
| LC-PB | effective `SO_SNDBUF` against the 64 KiB per-connection bound | CONTRADICTS the design as worded (see below). | The send-buffer mechanism of DESIGN 4.4 / 14.5 (Step 14) is stopped; the probe result and a candidate fix are below. |
| LC-PC | does a returned `_refresh` make every acknowledged write searchable (P5 extended) | PASS on Elasticsearch 9.5.6 single node: 85,915 acknowledged single and bulk writes into two indices under 6 concurrent writers, 200 refresh rounds, 0 acknowledged writes missing after the refresh, 0 retirement updates (`gen_to` added) invisible. Controls: before the refresh nothing is searchable; realtime GET by id works without a refresh; the broken variant that skips the refresh loses acknowledged writes (9,043,526 misses counted, run reports FAIL). | The searchable-publication barrier of DESIGN 5.6 holds on this node. |

| P2 | API key with `lc-ta-*` and no cluster privilege against `tb`, the look-alike `tbx`, their `authz` indices and an alias, 94 requests (search, get, mget, msearch, count, kNN, percolate, terms lookup, `_cat/indices`, `_field_caps`, `_mapping`, wildcard and comma lists, the cluster APIs); control key with `cluster: monitor`; security off must say "not established" | FAIL as worded, one narrow channel: no content, no count, no foreign index name from any cluster API (the key was refused or shown only `lc-ta-*`), but `_mget` on another tenant's ALIAS name returns HTTP 200 with a per-document error whose `_index` is the concrete index behind the alias. 70 refusals echoed the name the caller asked for (not a disclosure; the gateway normalises errors, T3). The monitor control leaks through `_cluster/state`, `_cat/shards`, `_nodes/stats` and `_mget` (4 request kinds), so the probe can fail. With security off the probe reports "security off: P2 not established" (run on a scratch node). | The design says a P2 failure moves dedicated tenants to separate clusters. The leak names only a physical index (and, with DESIGN 6.2's `-p<n>` suffix, a purge count) and needs a key and a known alias name that no non-gateway principal holds. Decision for the owner: accept with mitigations (gateway never forwards an Elasticsearch error or sub-response; random physical suffixes) or separate clusters. The build goes on under the first reading and labels it ASSUMPTION. |
| P3 | bootstrap credential passed by file (`ELASTIC_PASSWORD_FILE`) | PASS: the file authenticates as `elastic`; 0 of 506 container log lines and 0 of the container's environment values contain it (count-only grep with the file as pattern list). | none |
| P6 | API key invalidation latency | measured 1.01 to 1.04 s from invalidation to first refusal on this node (design expectation under 1 s; T7's bound is 5 s). | report the figure as a limit; T7 holds |

## LC-PB in detail

1. Linux doubles the value given to `setsockopt(SO_SNDBUF)` and reports the
   doubled value. Requesting 64 KiB gives an effective 131,072 bytes (inet and
   unix stream). The design's "effective size the kernel reports is at most
   64 KiB" is met only by requesting at most 32 KiB.
2. Even with an effective 65,536 bytes, the unsent bytes the kernel holds for a
   connection (`SIOCOUTQ`) exceeded 64 KiB when the peer did not read: 106,112
   on TCP loopback, 68,096 on a unix stream socket. With an effective 8,192
   bytes on TCP loopback the queue held 55,104. So the send buffer size does
   not bound the kernel-held bytes; "at most the connection's send buffer"
   (DESIGN 4.4) is false as stated, and the measurement at the gateway that the
   design specifies is exactly what exposes it.
3. Candidate fix (not adopted; it changes DESIGN 4.4, which is the owner's
   to decide): the hand-off reads `SIOCOUTQ` under the shared side of the send
   fence and writes at most `64 KiB - outq` bytes. With that cap the peak queue
   was at most 65,536 bytes for TCP and 63,232 for unix in every row, at all
   four requested sizes (`gateway_capped_peak_queue` in the result file). The
   cost is one `ioctl` per hand-off and smaller writes.

Not verified: behaviour with a real (non-loopback) TCP peer, with TLS, and
with autotuning enabled on the peer side.
