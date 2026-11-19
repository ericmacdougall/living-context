# Decision record: probe P2 (tenant key boundary)

Probe: P2
Verdict: FAIL
Decided by: main, ASSUMPTION (not the owner's words)
Status: awaiting the owner. Until the owner decides, a Step 5 build and every later step stand on this assumption, and the stream's ticket ends `S0 INCOMPLETE` (it can never report `S0 PASS` while P2 is a recorded FAIL).

## What failed

Against a tenant API key (role descriptor on `lc-tena-*`, no cluster privilege) the probe found no content, no document count and no foreign index name from any search, kNN, percolate, terms-lookup, `_cat`, `_field_caps`, `_mapping` or cluster API. One narrow channel remains: `_mget` naming another tenant's ALIAS returns HTTP 200 with a per-document error whose `_index` is the concrete index behind the alias. The positive control (`cluster: monitor`) leaks through four request kinds, so the probe can fail.

## Mitigation

Mitigation: tenant keys never leave the gateway; the gateway forwards no Elasticsearch error body, sub-response, `_index` field or index name (normalised `unavailable`, tested by T1 and T3 and AC-14); physical index suffixes are random (at least 32 bits, not sorted, not an arithmetic sequence), so a leaked physical name tells nothing about purge history; the provisioning interface keeps the cluster boundary a configuration choice so one cluster per tenant stays possible.

## The decision that is the owner's

Accept the mitigation above, or move tenants that need a hard boundary to separate clusters (DESIGN 8.7 and 20). Recorded as an ask; until it is answered in the owner's own words this record is labelled `main, ASSUMPTION`.
