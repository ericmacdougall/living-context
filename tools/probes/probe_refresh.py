"""Probe LC-PC (BUILD-PLAN P5, extended): does a returned `_refresh` make every
write acknowledged before the refresh call began searchable?

DESIGN 5.6 publishes the write-path watermark P and generation heads only
after an explicit refresh has returned. Checks, against the cluster at argv[1]
(http://host:port, no credential is sent; run against a scratch cluster):
  control   with refresh_interval -1, acknowledged writes are NOT searchable
            before the explicit refresh (so the probe can fail);
  realtime  a GET by id returns an acknowledged document without any refresh;
  barrier   200 rounds: writers keep indexing (single and bulk) into two
            indices; the checker snapshots the set of acknowledged ids, calls
            `_refresh` on both, and requires every snapshotted id to be found
            by a terms query on both; also for retirement updates (an update
            that adds a value to a `gen_to` field), which must be visible as a
            changed document after the refresh.
Prints one JSON verdict line.
"""
import json
import sys
import threading
import urllib.request
import uuid

BASE = sys.argv[1].rstrip("/")
BROKEN = len(sys.argv) > 2 and sys.argv[2] == "no-refresh"  # broken variant: must FAIL


def req(method, path, body=None):
    data = None if body is None else json.dumps(body).encode()
    r = urllib.request.Request(BASE + path, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=60) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def bulk(index, docs):
    lines = []
    for i, d in docs:
        lines.append(json.dumps({"index": {"_index": index, "_id": i}}))
        lines.append(json.dumps(d))
    r = urllib.request.Request(BASE + "/_bulk", data=("\n".join(lines) + "\n").encode(), method="POST",
                               headers={"Content-Type": "application/x-ndjson"})
    with urllib.request.urlopen(r, timeout=60) as resp:
        out = json.loads(resp.read())
    return [it["index"]["_id"] for it in out["items"] if it["index"]["status"] in (200, 201)]


def count_ids(index, ids):
    found = set()
    ids = list(ids)
    for k in range(0, len(ids), 5000):
        _, r = req("POST", f"/{index}/_search", {"size": 10000, "_source": False,
                                                 "query": {"terms": {"k": ids[k:k + 5000]}}})
        found |= {h["_id"] for h in r["hits"]["hits"]}
    return found


def main():
    run = uuid.uuid4().hex[:8]
    idx = [f"lcpc-{run}-a", f"lcpc-{run}-b"]
    out = {"probe": "LC-PC"}
    for ix in idx:
        req("PUT", f"/{ix}", {"settings": {"number_of_shards": 1, "number_of_replicas": 0,
                                            "refresh_interval": "-1"},
                              "mappings": {"properties": {"k": {"type": "keyword"},
                                                          "gen_to": {"type": "long"}}}})
    # control + realtime
    st, _ = req("PUT", f"/{idx[0]}/_doc/ctl1", {"k": "ctl1"})
    _, s = req("POST", f"/{idx[0]}/_search", {"query": {"terms": {"k": ["ctl1"]}}})
    out["control_not_searchable_before_refresh"] = s["hits"]["total"]["value"] == 0
    st, g = req("GET", f"/{idx[0]}/_doc/ctl1")
    out["realtime_get_without_refresh"] = st == 200 and g.get("found") is True
    req("POST", f"/{idx[0]}/_refresh")
    _, s = req("POST", f"/{idx[0]}/_search", {"query": {"terms": {"k": ["ctl1"]}}})
    out["searchable_after_refresh"] = s["hits"]["total"]["value"] == 1

    acked = {ix: set() for ix in idx}
    lock = threading.Lock()
    stop = threading.Event()

    def writer(n):
        c = 0
        while not stop.is_set():
            ix = idx[c % 2]
            c += 1
            if c % 3 == 0:
                items = [(f"w{n}-{c}-{j}", {"k": f"w{n}-{c}-{j}"}) for j in range(20)]
                ok = bulk(ix, items)
            else:
                i = f"w{n}-{c}"
                st, _ = req("PUT", f"/{ix}/_doc/{i}", {"k": i})
                ok = [i] if st in (200, 201) else []
            with lock:
                acked[ix].update(ok)

    ws = [threading.Thread(target=writer, args=(n,)) for n in range(6)]
    for w in ws:
        w.start()
    rounds, missing_total, retire_missing = 200, 0, 0
    anomalies = []
    verified = {ix: set() for ix in idx}
    for r_ in range(rounds):
        with lock:
            snap = {ix: set(acked[ix]) - verified[ix] for ix in idx}
        # retirement update of one already-acked doc, acknowledged before the refresh
        retire = None
        if snap[idx[0]]:
            retire = sorted(snap[idx[0]])[r_ % len(snap[idx[0]])]
            st, _ = req("POST", f"/{idx[0]}/_update/{retire}", {"doc": {"gen_to": [r_ + 1]}})
            if st != 200:
                retire = None
        for ix in idx:
            if not BROKEN:
                req("POST", f"/{ix}/_refresh")
        for ix in idx:
            miss = snap[ix] - count_ids(ix, snap[ix])
            verified[ix] |= snap[ix] - miss
            if miss:
                missing_total += len(miss)
                anomalies.append((ix, r_, len(miss)))
        if retire is not None:
            _, s = req("POST", f"/{idx[0]}/_search",
                       {"query": {"bool": {"filter": [{"term": {"k": retire}}, {"term": {"gen_to": r_ + 1}}]}}})
            if s["hits"]["total"]["value"] != 1:
                retire_missing += 1
    stop.set()
    for w in ws:
        w.join()
    out.update({"rounds": rounds, "note": "each round verifies the ids acknowledged since the previous round", "acked_total": sum(len(v) for v in acked.values()),
                "acked_but_not_searchable_after_refresh": missing_total,
                "retirement_updates_not_visible_after_refresh": retire_missing,
                "anomalies": anomalies[:5]})
    for ix in idx:
        req("DELETE", f"/{ix}")
    ok = (out["control_not_searchable_before_refresh"] and out["realtime_get_without_refresh"]
          and out["searchable_after_refresh"] and missing_total == 0 and retire_missing == 0)
    out["broken_variant"] = BROKEN
    out["verdict"] = "PASS" if ok else "FAIL"
    print(json.dumps(out, sort_keys=True))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
