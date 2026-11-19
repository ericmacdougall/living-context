"""Probe P15: a tenant key owner manages its own API keys and no more.

A native user holds index privileges on `lc-tenp-*` and `manage_own_api_key`
only. Checks (N=6): (1) a key it creates with an over-broad descriptor
(`*`, all) still cannot read another tenant's index (created keys carry at
most the owner's privileges); (2) it can create a second key and use it
(rotation: create new, retire old); (3) it can invalidate its own old key,
which is then refused; (4) it cannot invalidate a key it does not own (the request needs `owner: true`; the foreign key stays valid);
(5) a gateway-style key (index privileges, no cluster privilege) cannot
create keys; (6) nor invalidate them. The user's password is generated in
memory, written to a 0600 file in a fresh directory and never printed; the
user, role and keys are deleted at the end.
"""
import os
import secrets
import sys
import tempfile
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from living_context.gateway.es import EsClient  # noqa: E402
from tools.probes import pcommon  # noqa: E402

pcommon.start()
admin = pcommon.admin_client()
URL, CA = os.environ["LC_TEST_ES_URL"], os.environ["LC_TEST_CA"]


def keyfile(d, name, text):
    p = os.path.join(d, name)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    return p


def main():
    run = uuid.uuid4().hex[:6]
    own, oth = f"lc-tenp{run}-x", f"lc-tenq{run}-x"
    for ix in (own, oth):
        pcommon.create(admin, ix, {"k": {"type": "keyword"}})
        pcommon.bulk_index(admin, ix, [("1", {"k": "v"})])
    role, user = f"lcp15r_{run}", f"lcp15u_{run}"
    admin.request("PUT", f"/_security/role/{role}", {
        "cluster": ["manage_own_api_key"],
        "indices": [{"names": [f"lc-tenp{run}-*"], "privileges": ["read", "write", "create_index", "view_index_metadata"]}]})
    d = tempfile.mkdtemp(prefix="lcp15-")
    pw = secrets.token_urlsafe(24)
    st, _ = admin.request("PUT", f"/_security/user/{user}", {"password": pw, "roles": [role]})
    assert st == 200
    owner = EsClient(URL, CA, keyfile(d, "owner", pw), scheme="basic", user=user)
    st, k1 = owner.request("POST", "/_security/api_key", {"name": f"o1-{run}", "expiration": "1h", "role_descriptors": {
        "broad": {"cluster": ["all"], "indices": [{"names": ["*"], "privileges": ["all"]}]}}})
    k1c = EsClient(URL, CA, keyfile(d, "k1", k1["encoded"]))
    c1 = (st == 200 and k1c.request("GET", f"/{own}/_search")[0] == 200
          and k1c.request("GET", f"/{oth}/_search")[0] == 403 and k1c.request("GET", "/_cluster/state")[0] == 403)
    st, k2 = owner.request("POST", "/_security/api_key", {"name": f"o2-{run}", "expiration": "1h"})
    k2c = EsClient(URL, CA, keyfile(d, "k2", k2["encoded"])) if st == 200 else None
    c2 = st == 200 and k2c.request("GET", f"/{own}/_search")[0] == 200
    st, inv = owner.request("DELETE", "/_security/api_key", {"ids": [k1["id"]], "owner": True})
    refused = False
    for _ in range(100):
        if k1c.request("GET", f"/{own}/_search")[0] in (401, 403):
            refused = True
            break
        time.sleep(0.05)
    c3 = st == 200 and k1["id"] in inv.get("invalidated_api_keys", []) and refused
    c3_detail = {"status": st, "listed": k1["id"] in inv.get("invalidated_api_keys", []), "refused_after": refused}
    st, ak = admin.request("POST", "/_security/api_key", {"name": f"adm-{run}", "expiration": "1h"})
    akc = EsClient(URL, CA, keyfile(d, "ak", ak["encoded"]))
    st_nf, _ = owner.request("DELETE", "/_security/api_key", {"ids": [ak["id"]]})  # without owner=true: refused
    st, inv2 = owner.request("DELETE", "/_security/api_key", {"ids": [ak["id"]], "owner": True})
    time.sleep(1.5)
    c4 = (ak["id"] not in (inv2.get("invalidated_api_keys", []) if isinstance(inv2, dict) else [])
          and akc.request("GET", "/_security/_authenticate")[0] == 200)
    st, gk = admin.request("POST", "/_security/api_key", {"name": f"gw-{run}", "expiration": "1h", "role_descriptors": {
        "r": {"indices": [{"names": [f"lc-tenp{run}-*"], "privileges": ["read", "write"]}]}}})
    gwc = EsClient(URL, CA, keyfile(d, "gw", gk["encoded"]))
    st_c, _ = gwc.request("POST", "/_security/api_key", {"name": f"x-{run}"})
    c5 = st_c in (401, 403)
    st_i, _ = gwc.request("DELETE", "/_security/api_key", {"ids": [ak["id"]]})
    c6 = st_i in (401, 403)
    ids = [x for x in (k2 and k2.get("id"), ak["id"], gk["id"]) if x]
    admin.request("DELETE", "/_security/api_key", {"ids": ids})
    admin.request("DELETE", f"/_security/user/{user}")
    admin.request("DELETE", f"/_security/role/{role}")
    pcommon.drop(admin, own, oth)
    res = [c1, c2, c3, c4, c5, c6]
    return pcommon.finish("P15", __file__, "a key owner manages only its own keys; created keys carry at most its privileges",
                          "checks 1-6: " + ", ".join(str(int(x)) for x in res), all(res), 6,
                          {"owner_invalidates_own_detail": c3_detail, "foreign_invalidate_without_owner_flag_status": st_nf, "foreign_invalidate_with_owner_flag_status": st, "checks": dict(zip(["limited_by_owner", "rotation_create", "owner_invalidates_own",
                                               "cannot_invalidate_foreign", "gateway_key_cannot_create",
                                               "gateway_key_cannot_invalidate"], res))})


if __name__ == "__main__":
    sys.exit(main())
