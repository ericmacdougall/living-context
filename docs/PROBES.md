# Platform probe results (stub-first build)

Probes live in `tools/probes/`, each prints one JSON verdict line, and the
result of the run on the build host (Linux 6.8, ext4, Python 3.12) is in
`tools/probes/results/`. Ids here are the build's own (LC-PA to LC-PC); LC-PC
is BUILD-PLAN P5 extended.

| Id | Probe | Result | Effect on the design |
|---|---|---|---|
| LC-PA | atomic exchange of two names (`renameat2` with `RENAME_EXCHANGE`) from Python 3.12 | PASS. The stdlib has no wrapper; both `libc.renameat2` and the raw syscall (x86_64 316, aarch64 276) work through `ctypes`. 20,000 exchanges under 4 concurrent readers: no missing name, no content other than one of the two, sizes unchanged. An invalid flag is refused (control). | DESIGN 4.4 compaction is implementable as written on this filesystem. Provisioning must still run the exchange test on the journal directory of each deployment. |
| LC-PB | effective `SO_SNDBUF` against the 64 KiB per-connection bound | CONTRADICTS the design as worded (see below). | Main adopted the queue-capped hand-off below (ASSUMPTION, not the owner's words); DESIGN 4.4 now says so. |
| LC-PC | does a returned `_refresh` make every acknowledged write searchable (P5 extended) | PASS on Elasticsearch 9.5.6 single node: 85,915 acknowledged single and bulk writes into two indices under 6 concurrent writers, 200 refresh rounds, 0 acknowledged writes missing after the refresh, 0 retirement updates (`gen_to` added) invisible. Controls: before the refresh nothing is searchable; realtime GET by id works without a refresh; the broken variant that skips the refresh loses acknowledged writes (9,043,526 misses counted, run reports FAIL). | The searchable-publication barrier of DESIGN 5.6 holds on this node. |

| P2 | API key with `lc-tena-*` and no cluster privilege against `tenb`, the look-alike `tenbx`, their `authz` indices and an alias, 94 requests (search, get, mget, msearch, count, kNN, percolate, terms lookup, `_cat/indices`, `_field_caps`, `_mapping`, wildcard and comma lists, the cluster APIs); control key with `cluster: monitor`; security off must say "not established" | FAIL as worded, one narrow channel: no content, no count, no foreign index name from any cluster API (the key was refused or shown only `lc-tena-*`), but `_mget` on another tenant's ALIAS name returns HTTP 200 with a per-document error whose `_index` is the concrete index behind the alias. 70 refusals echoed the name the caller asked for (not a disclosure; the gateway normalises errors, T3). The monitor control leaks through `_cluster/state`, `_cat/shards`, `_nodes/stats` and `_mget` (4 request kinds), so the probe can fail. With security off the probe reports "security off: P2 not established" (run on a scratch node). | The design says a P2 failure moves dedicated tenants to separate clusters. The leak names only a physical index (and, with DESIGN 6.2's `-p<n>` suffix, a purge count) and needs a key and a known alias name that no non-gateway principal holds. Decision for the owner: accept with mitigations (gateway never forwards an Elasticsearch error or sub-response; random physical suffixes) or separate clusters. The build goes on under the first reading and labels it ASSUMPTION. |
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
3. Fix, adopted by main as an ASSUMPTION (DESIGN 4.4, a96a808): the hand-off reads `SIOCOUTQ` under the shared side of the send
   fence and writes at most `64 KiB - outq` bytes. With that cap the peak queue
   was at most 65,536 bytes for TCP and 63,232 for unix in every row, at all
   four requested sizes (`gateway_capped_peak_queue` in the result file). The
   cost is one `ioctl` per hand-off and smaller writes.

Not verified: behaviour with a real (non-loopback) TCP peer, with TLS, and
with autotuning enabled on the peer side.

## Results bound to script, image and N (P1 to P16)

Every row below is a file `tools/probes/results/<id>.json` holding {id, verdict,
expected, observed, script, script_sha256, image_digest, n, commit, date}. The
verdict is relative to the expected result of BUILD-PLAN section 2. N is the
executed-check count and equals `tools/probes/pinned_n.json`. The image is the
digest pinned in `docs/HOST.md`. `tools/probe_gate.py` refuses a step whose
result is missing, whose script changed since (hash), whose image digest
differs or whose N differs, and accepts a FAIL or LIMITED verdict only with a
record in `docs/decisions/`. All runs: single node, security on, Basic licence,
Elasticsearch 9.5.6, Linux 6.8.

| Id | Verdict | N | Observed |
|---|---|---|---|
| P1 | PASS | 10 | rrf, linear, DLS, FLS refused with a licence error; `semantic_text` mapping accepted but indexing refused (403); `bbq_disk`, lexical percolate, int8_hnsw kNN at 384 dims, alias and force merge work |
| P2 | FAIL | 94 | as above (names-only `_mget` channel on an alias); decision record `docs/decisions/P2-names-only-channel.md`, accepted by the owner with mitigations |
| P3 | PASS | 1 | the credential file authenticates; 0 container log lines and 0 environment values hold it |
| P4 | PASS | 3 | storing a `knn` query in a percolator field is refused ("the [knn] query is unsupported inside a percolator query"), top level and inside `bool`; a stored match query is accepted (control) |
| P5 | PASS | 200 | 200 refresh rounds under 6 writers, 0 acknowledged writes missing after a returned refresh, 0 retirement updates invisible; the broken variant without the refresh is red (millions of misses counted); realtime GET works without refresh |
| P6 | LIMITED | 1 | about 1.01 s to the first refusal, expected under 1 s; inside T7's 5 s; decision record `docs/decisions/P6-invalidation-latency.md` (proposed by the task owner, awaiting main) |
| P7 | PASS | 40 | 40 of 40 restrictive-filter kNN queries (20 of 5,000 documents) returned k=10 hits; the control with `post_filter` returned fewer than k in 40 of 40 |
| P8 | PASS | 400 | 400 atomic alias swaps under 4 readers (5,892 reads): 0 anomalies; the two-request control showed 3,558 anomalies in 6,417 reads |
| P9 | PASS | 3 | `expunge_deletes_allowed: 0` accepted; `docs.deleted` 2,000 (soft-delete tombstones double the count) to 0 with repeated `only_expunge_deletes` passes |
| P10 | PASS | 3 | `docs.deleted` reached 0 about 30.4 s after the delete in all three trials (30.37 to 30.44 s). Elasticsearch drops soft-delete tombstones only after the retention lease advances, a periodic sync (30 s default); one expunge pass right after the delete leaves them, so the purge must repeat the pass. The receipt bound is this measured figure plus the polling interval |
| P11 | PASS | 100 | 100 of 100 stale `if_seq_no` writes got 409 and left the stored source unchanged; the current-value write succeeds (control) |
| P12 | PASS | 50 | 50 of 50 `_msearch` calls with a missing-index sub-search: the other two answered, the failure was per item |
| P13 | PASS | 4 | a terms query of 10,000 ids returns all 10,000 documents; default `index.max_terms_count` 65,536; 65,536 ids accepted, 65,537 refused |
| P14 | LIMITED | 3 | on a manual `read_only_allow_delete` block writes get 429 and reads work, unblocking restores writes; on a scratch node with a 160 MB tmpfs data directory and absolute watermarks (70/60/50 MB, monitor interval 10 s) filled until the node applied the block itself, writes got 429 about 14 s later and reads worked. The premise holds, so the verdict is LIMITED: the expected result (the gateway's revoke is effective at once under both blocks) is not exercised here. The Step 15 gate stays closed on P14 until that test exists or a decision record is written. Open: `PENDING_REAL_TEST P14` |
| P15 | PASS | 6 | a key owner (index privileges on its own tenant, `manage_own_api_key` only) creates keys that carry at most its privileges, rotates (create then invalidate) and invalidates its own keys; it cannot invalidate another owner's key; a gateway-style key (no cluster privilege) can neither create nor invalidate keys. Finding: the owner's invalidate request must carry `"owner": true`, without it Elasticsearch answers 403 |
| P16 | PASS | 7 | slow-log thresholds of -1 are accepted and read back on all 12 index kinds, search and indexing; with default logger levels a random marker in a document, a search body and failing requests reaches no container log line or log file (count 0); with thresholds at 0 the marker appears for search (2) and indexing (1); setting -1 again stops it |

N is a pinned count of executed checks per script, chosen by the task owner (ASSUMPTION, noted in `pinned_n.json`; P1 and P14 were adjusted to match their scripts).

Security-off check: every probe script prints `security off: P2 not established`
and exits 2 against a cluster that answers an unauthenticated request
(`tests/unit/test_probe_security_off.py` runs each script against a stub
server).

Not verified: multi-node behaviour of P6, P8, P9, P10 and P14; a real disk
(the P14 volume is a tmpfs); the gateway's behaviour under the P14 blocks
(Step 15).

## Measured limits

| Item | Measurement | Limit |
|---|---|---|
| Keystore `VACUUM` after each destroy (`Keystore.destroy`) | fleet host, Python 3.12, ext4, 64-byte payloads, five destroys each: 3 ms at 1,000 keys (217 KB file); 167 to 184 ms at 100,000 keys (20 MB file) | A destroy rewrites the whole file, so its cost grows linearly with the keys of that tenant (about 1.7 microseconds per key). One call destroys a whole set of leaves with one `VACUUM` (`leaf_ids=`), and a call that finds no key does none. A tenant with millions of keys needs the keystore split by class or a different residue-free delete; not built. |
