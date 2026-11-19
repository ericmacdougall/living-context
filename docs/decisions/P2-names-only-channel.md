# Decision record: probe P2 (tenant key boundary)

Probe: P2
Verdict: FAIL
Decided by: the project owner: accept with mitigations (relayed by the coordinating agent; the owner's wording is held in the ticket record)
Status: decided. The probe still reports FAIL as worded and its result file is unchanged. P2 no longer forces `S0 INCOMPLETE`; every other open item still does.

## What failed

Against a tenant API key (role descriptor on `lc-tena-*`, no cluster privilege) the probe found no content, no document count and no foreign index name from any search, kNN, percolate, terms-lookup, `_cat`, `_field_caps`, `_mapping` or cluster API. One narrow channel remains: `_mget` naming another tenant's ALIAS returns HTTP 200 with a per-document error whose `_index` is the concrete index behind the alias. The positive control (`cluster: monitor`) leaks through four request kinds, so the probe can fail.

## Mitigation

Mitigation: tenant keys never leave the gateway; the gateway forwards no Elasticsearch error body, sub-response, `_index` field or index name (normalised `unavailable`, tested by T1 and T3 and AC-14); physical index suffixes are random (at least 32 bits, not sorted, not an arithmetic sequence), so a leaked physical name tells nothing about purge history; the provisioning interface keeps the cluster boundary a configuration choice so one cluster per tenant stays possible.

## The decision

The owner accepted the mitigation above: tenant keys never leave the gateway, no Elasticsearch error or sub-response is forwarded, and physical index names are random. One cluster per tenant stays a configuration choice for tenants that need a hard boundary (DESIGN 8.7 and 20).
