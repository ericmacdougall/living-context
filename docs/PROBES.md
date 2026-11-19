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
