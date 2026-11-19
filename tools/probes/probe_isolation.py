"""Probes P2 (key boundary), P3 (password by file) and P6 (key invalidation latency).

Usage: probe_isolation.py <es_url> <ca_path> <bootstrap_credential_file>

P2: three tenants ta, tb and the look-alike tbx, each with leaf, authz and ctl
indices and an alias. API keys are created with role descriptors that name
only `lc-<t>-*` and grant no cluster privilege. With the `ta` key every read
API of BUILD-PLAN P2 is tried against `tb` and `tbx`, plus the cluster APIs,
and no response may carry a tb or tbx index name or marker. Control: a `ta`
key that also holds `cluster: monitor` must leak at least one name through a
cluster API (so the probe can fail). The probe first checks that an
unauthenticated request is refused; with security off it reports
"security off: P2 not established" and gives no verdict.
P3: the bootstrap file, mounted by file path, authenticates as `elastic`.
P6: seconds from invalidating a key to the first refusal of that key.
Prints one JSON line. No credential is printed.
"""
import json
import os
import ssl
import sys
import tempfile
import time
import urllib.error
import urllib.request

from living_context.gateway.es import EsClient, EsError

URL, CA, PWFILE = sys.argv[1], sys.argv[2], sys.argv[3]
MARK = {"ta": "MARKER-ALPHA-7f3a", "tb": "MARKER-BRAVO-91c2", "tbx": "MARKER-XRAY-5d0e"}
KINDS = ["base-leaf", "authz", "ctl"]


def unauth_status():
    ctx = ssl.create_default_context(cafile=CA) if URL.startswith("https") else None
    try:
        with urllib.request.urlopen(URL + "/", context=ctx, timeout=15) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def make_key(admin, name, tenant, cluster=()):
    st, body = admin.request("POST", "/_security/api_key", {
        "name": name, "expiration": "1d",
        "role_descriptors": {"r": {"cluster": list(cluster), "indices": [
            {"names": [f"lc-{tenant}-*"],
             "privileges": ["read", "write", "create_index", "view_index_metadata", "monitor"]}]}}})
    assert st == 200, st
    return body


def stash(keydir, name, body):
    p = os.path.join(keydir, name)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(body["encoded"])
    return p, body["id"]


def main():
    out = {"probe": "P2+P3+P6"}
    us = unauth_status()
    out["unauthenticated_status"] = us
    if us == 200:
        print(json.dumps({"probe": "P2", "verdict": "security off: P2 not established"}))
        return 2
    admin = EsClient(URL, CA, PWFILE, scheme="basic", user="elastic")
    st, _ = admin.request("GET", "/_security/_authenticate")
    out["p3_bootstrap_file_authenticates"] = st == 200
    st, lic = admin.request("GET", "/_license")
    out["license_type"] = lic["license"]["type"] if st == 200 else None
    for t in ("ta", "tb", "tbx"):
        for k in KINDS:
            ix = f"lc-{t}-{k}"
            admin.request("DELETE", f"/{ix}")
            props = {"text": {"type": "text"}, "tenant": {"type": "keyword"}, "q": {"type": "percolator"},
                     "vec": {"type": "dense_vector", "dims": 3, "similarity": "cosine"}}
            admin.request("PUT", f"/{ix}", {"settings": {"number_of_replicas": 0}, "mappings": {"properties": props}})
            admin.request("PUT", f"/{ix}/_doc/d1?refresh=true",
                          {"text": MARK[t], "tenant": t, "vec": [1, 0, 0], "q": {"match": {"text": MARK[t]}}})
        admin.request("POST", "/_aliases",
                      {"actions": [{"add": {"index": f"lc-{t}-base-leaf", "alias": f"lc-{t}-alias-leaf"}}]})
    keydir = tempfile.mkdtemp(prefix="lcp2-")
    ta_path, ta_id = stash(keydir, "ta", make_key(admin, "p2-ta", "ta"))
    ctl_path, ctl_id = stash(keydir, "ta-monitor", make_key(admin, "p2-ta-monitor", "ta", cluster=["monitor"]))
    ta = EsClient(URL, CA, ta_path)
    ctl = EsClient(URL, CA, ctl_path)

    other = ["lc-tb-base-leaf", "lc-tb-authz", "lc-tb-ctl", "lc-tbx-base-leaf", "lc-tbx-authz", "lc-tbx-ctl",
             "lc-tb-alias-leaf"]
    tests = []
    for ix in other:
        tests += [("GET", f"/{ix}/_search", None), ("GET", f"/{ix}/_doc/d1", None),
                  ("POST", "/_mget", {"docs": [{"_index": ix, "_id": "d1"}]}),
                  ("GET", f"/{ix}/_count", None), ("GET", f"/{ix}/_field_caps?fields=*", None),
                  ("GET", f"/{ix}/_mapping", None), ("GET", f"/_cat/indices/{ix}", None),
                  ("POST", f"/{ix}/_search",
                   {"knn": {"field": "vec", "query_vector": [1, 0, 0], "k": 3, "num_candidates": 5}}),
                  ("POST", f"/{ix}/_search",
                   {"query": {"percolate": {"field": "q", "document": {"text": MARK["tb"]}}}}),
                  ("POST", "/lc-ta-base-leaf/_search",
                   {"query": {"terms": {"tenant": {"index": ix, "id": "d1", "path": "tenant"}}}}),
                  ("POST", "/_msearch", [{"index": ix}, {"query": {"match_all": {}}}])]
    tests += [("GET", "/lc-*/_search", None), ("GET", "/lc-t*/_search", None),
              ("GET", "/lc-ta-base-leaf,lc-tb-base-leaf/_search", None), ("GET", "/_all/_search", None),
              ("GET", "/*/_search", None), ("GET", "/_cat/indices", None), ("GET", "/_cat/aliases", None),
              ("GET", "/_cluster/state", None), ("GET", "/_cat/shards", None),
              ("GET", "/_tasks?detailed=true", None), ("GET", "/_resolve/index/lc-*", None),
              ("GET", "/lc-tb-base-leaf/_search_shards", None), ("GET", "/_nodes/stats", None),
              ("GET", "/_cluster/stats", None), ("GET", "/_alias", None), ("GET", "/lc-t*/_mapping", None),
              ("GET", "/lc-*/_field_caps?fields=*", None)]
    cluster_apis = {"/_cluster/state", "/_cat/shards", "/_tasks?detailed=true", "/_resolve/index/lc-*",
                    "/lc-tb-base-leaf/_search_shards", "/_nodes/stats", "/_cluster/stats", "/_cat/aliases",
                    "/_alias"}

    import re
    name_re = re.compile(r"lc-tbx?-[a-z-]+")

    def leaked(body, request_text):
        """A leak is foreign content, or a foreign index name the request did not itself contain.
        (An error that echoes the name the caller asked for is not a disclosure; the gateway
        normalises such errors for T3.)"""
        text = body if isinstance(body, str) else json.dumps(body)
        if any(m in text for m in (MARK["tb"], MARK["tbx"])):
            return True
        return any(n not in request_text for n in name_re.findall(text))

    def run(client):
        res = {"n": 0, "leaks": [], "cluster_ok": [], "echo_only": 0, "answered_200": {}}
        for method, path, body in tests:
            try:
                st, rb = client.request(method, path, body, ndjson=(path == "/_msearch"))
            except EsError:
                st, rb = 0, ""
            res["n"] += 1
            req_text = path + json.dumps(body)
            if leaked(rb, req_text):
                res["leaks"].append(f"{method} {path.split('?')[0]}")
            elif any(n in (rb if isinstance(rb, str) else json.dumps(rb)) for n in name_re.findall(req_text)):
                res["echo_only"] += 1
            if path in cluster_apis and st == 200:
                res["cluster_ok"].append(path)
                res["answered_200"][path] = (rb if isinstance(rb, str) else json.dumps(rb))[:160]
        return res

    r_ta, r_ctl = run(ta), run(ctl)
    out["ta_key"] = {"requests": r_ta["n"], "leaks": r_ta["leaks"], "error_echoed_requested_name_only": r_ta["echo_only"],
                     "cluster_apis_answered_200_with_no_foreign_name": r_ta["answered_200"]}
    out["control_monitor_key"] = {"requests": r_ctl["n"], "leak_count": len(r_ctl["leaks"]),
                                  "examples": sorted(set(r_ctl["leaks"]))[:6]}
    st, rb = ta.request("GET", "/lc-ta-base-leaf/_search")
    out["ta_key_reads_own_index"] = st == 200 and MARK["ta"] in json.dumps(rb)
    p6_path, p6_id = stash(keydir, "p6", make_key(admin, "p6", "ta"))
    c6 = EsClient(URL, CA, p6_path)
    assert c6.request("GET", "/lc-ta-base-leaf/_search")[0] == 200
    t0 = time.time()
    admin.request("DELETE", "/_security/api_key", {"ids": [p6_id]})
    lat = None
    while time.time() - t0 < 30:
        if c6.request("GET", "/lc-ta-base-leaf/_search")[0] in (401, 403):
            lat = round(time.time() - t0, 3)
            break
        time.sleep(0.02)
    out["p6_invalidation_to_first_refusal_s"] = lat
    admin.request("DELETE", "/_security/api_key", {"ids": [ta_id, ctl_id]})
    ok = (not r_ta["leaks"] and out["ta_key_reads_own_index"]
          and len(r_ctl["leaks"]) > 0 and out["p3_bootstrap_file_authenticates"])
    out["verdict"] = "PASS" if ok else "FAIL"
    print(json.dumps(out, sort_keys=True))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
