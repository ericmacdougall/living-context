# Decision record: probe P14 (revoke while the authz index refuses writes)

Probe: P14
Verdict: LIMITED
Decided by: main (accepted on the Step 15 test evidence; ASSUMPTION, the project owner has not been asked)

The probe script establishes the premise only: writes to a blocked authz index are refused while reads work, for a manual block and for the block the node applies itself at the disk flood-stage watermark. Its verdict therefore stays LIMITED as worded. The expected result, that the gateway's revoke is effective at once while the index is blocked, is tested by `tests/security/test_revoke_flood_es.py` on a scratch node of the same image whose data directory is a small tmpfs: the node applied the block 28.4 s after the volume was filled, and with the index refusing writes the revoke was denied in memory, durable in the epoch journal, returned within 2 s, and the table rebuilt from the journal alone after a restart held it. The variant that needs the Elasticsearch write first is red in the same test.

Mitigation: the revoke does not depend on the Elasticsearch write (DESIGN 4.4); the persist to `lc-<t>-authz` is best effort, retried and reported as `persisted` in the receipt. The scratch volume is a tmpfs and one node: a real disk and a multi-node cluster are not exercised (DESIGN 20, PROBES.md).
