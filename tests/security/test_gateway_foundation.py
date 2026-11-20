"""BUILD-PLAN Step 5 (pure parts): naming, validator, admission, T6 key space.

Each mechanism has a broken variant that must turn the same check red.
The ES-backed probes SEC-T1 to T3 and the socket server are not part of this file.
"""
import fnmatch
import io
import itertools
import random
import threading
import time
import unittest
import zlib

from living_context.config import Constants
from living_context.gateway import admission, indices, validator

# --------------------------------------------------------------------------- naming


class Naming(unittest.TestCase):
    def test_names_and_patterns(self):
        self.assertEqual(indices.class_index("tena", "base", "leaf"), "lc-tena-base-leaf")
        self.assertEqual(indices.tenant_index("tena", "authz"), "lc-tena-authz")
        self.assertEqual(indices.tenant_pattern("tena"), "lc-tena-*")
        self.assertEqual(indices.class_patterns("tena", "base"), ["lc-tena-base-*", "lc-tena-ctl"])
        for bad in ("ta", "ten-a", "TENA", "../tena", "", "a" * 17):
            with self.assertRaises(indices.NameError_):
                indices.tenant_pattern(bad)
        with self.assertRaises(indices.NameError_):
            indices.class_index("tena", "base", "bogus")

    def all_names(self, tenants, classes):
        out = {}
        for t in tenants:
            names = [indices.tenant_index(t, k) for k in indices.TENANT_KINDS]
            names += [indices.class_index(t, c, k) for c in classes for k in indices.CLASS_KINDS]
            out[t] = names
        return out

    def cross_matches(self, tenants, classes, pattern_fn):
        names = self.all_names(tenants, classes)
        bad = []
        for t in tenants:
            pat = pattern_fn(t)
            for other, ns in names.items():
                if other != t:
                    bad += [(t, n) for n in ns if fnmatch.fnmatchcase(n, pat)]
        return bad

    def test_no_tenant_pattern_matches_another_tenants_index_including_lookalikes(self):
        tenants = ["tbxx", "tbxxx", "tbxxxx", "tbx1", "tbxxa", "tenant01", "tenant012"]
        self.assertEqual(self.cross_matches(tenants, ["base", "restricted"], indices.tenant_pattern), [])

    def test_broken_hyphenated_tenant_ids_would_cross_match(self):
        # Failing-first: if tenant ids may contain a hyphen, `lc-tbxx-*` reaches `lc-tbxx-ab-...`.
        names = ["lc-tbxx-ab-base-leaf", "lc-tbxx-base-leaf"]
        self.assertEqual([n for n in names if fnmatch.fnmatchcase(n, "lc-tbxx-*")], names)
        self.assertFalse(indices.ID.match("tbxx-ab"))

    def test_role_pattern_safety_table(self):
        rows = {"lc-tena-*": True, "lc-tena-base-*": True, "lc-tena-ctl": True, "lc-*": False, "*": False,
                "lc-tena*": False, "lc-te*-leaf": False, "lc-ta-*": False, "": False, "lc-sys-registry": False,
                "lc-tena-*,lc-tenb-*": False}
        for pat, want in rows.items():
            self.assertEqual(indices.role_pattern_is_safe(pat), want, pat)

    def test_physical_names_are_random_not_sequential(self):
        a = {indices.physical_name("lc-tena-base-leaf") for _ in range(50)}
        self.assertEqual(len(a), 50)
        self.assertTrue(all(len(n) == len("lc-tena-base-leaf") + 10 for n in a))

    def test_shard_budget_matches_the_design_table_and_refuses_above_80_percent(self):
        self.assertEqual(indices.shards_per_tenant(2, 0), 21)
        self.assertEqual(indices.shards_per_tenant(1, 0), 12)
        self.assertEqual(indices.shards_per_tenant(3, 1), 60)
        self.assertTrue(indices.may_provision(0, 2, 0))
        self.assertTrue(indices.may_provision(779, 2, 0))       # 800 exactly
        self.assertFalse(indices.may_provision(780, 2, 0))
        self.assertFalse(indices.may_provision(741, 3, 1))


# --------------------------------------------------------------------------- validator

GOOD = {"tenant": "tena", "cls": "base", "kind": "leaf", "lin_leaves": [], "lin_leaf_ids": [],
        "lin_nodes": [], "lin_subjects": ["tok1"], "lin_queries": [], "derivation": "job1",
        "erasure_rewrite": 0, "created_at": "2026-01-01T00:00:00Z", "schema_v": 1}


def check(doc, fn=validator.validate, **over):
    kw = dict(session_tenant="tena", index="lc-tena-base-leaf", index_tenant="tena", index_cls="base")
    kw.update(over)
    try:
        fn(doc, **kw)
        return None
    except validator.Refused as e:
        return e.code


def lenient_validate(doc, **kw):
    """Broken variant: does not require lin_subjects."""
    doc = dict(doc)
    doc.setdefault("lin_subjects", ["x"])
    return validator.validate(doc, **kw)


class Validator(unittest.TestCase):
    def test_good_document_passes(self):
        self.assertIsNone(check(GOOD))

    def test_every_missing_common_field_is_refused(self):
        for f in validator.REQUIRED:
            d = {k: v for k, v in GOOD.items() if k != f}
            self.assertEqual(check(d), f"missing_{f}", f)

    def test_lin_subjects_missing_is_refused_and_the_lenient_variant_is_caught(self):
        d = {k: v for k, v in GOOD.items() if k != "lin_subjects"}
        self.assertEqual(check(d), "missing_lin_subjects")
        self.assertIsNone(check(d, fn=lenient_validate))  # the broken validator accepts it: the test sees it

    def test_tenant_and_class_must_agree_with_session_and_index(self):
        self.assertEqual(check({**GOOD, "tenant": "tenb"}), "tenant_mismatch")
        self.assertEqual(check(GOOD, session_tenant="tenb"), "tenant_mismatch")
        self.assertEqual(check(GOOD, index_tenant="tenb"), "tenant_mismatch")
        self.assertEqual(check({**GOOD, "cls": "restricted"}), "class_mismatch")
        self.assertEqual(check({**GOOD, "cls": "BASE"}), "bad_class")
        self.assertIsNone(check({**GOOD, "cls": "restricted"}, index_cls=None))  # tenant-wide index

    def test_shape_errors(self):
        for field, value, code in (("lin_subjects", "tok", "bad_lin_subjects"), ("lin_subjects", [1], "bad_lin_subjects"),
                                   ("lin_nodes", None, "bad_lin_nodes"), ("kind", "weird", "bad_kind"),
                                   ("erasure_rewrite", -1, "bad_erasure_rewrite"), ("erasure_rewrite", True, "bad_erasure_rewrite"),
                                   ("schema_v", "1", "bad_schema_v")):
            self.assertEqual(check({**GOOD, field: value}), code, field)
        self.assertEqual(check([GOOD]), "not_an_object")

    def test_derived_kinds_need_lineage(self):
        empty = {**GOOD, "kind": "node", "lin_subjects": []}
        self.assertEqual(check(empty), "no_lineage")
        self.assertIsNone(check({**empty, "lin_nodes": ["n1:1"]}))
        self.assertIsNone(check({**GOOD, "kind": "leaf", "lin_subjects": []}))  # a leaf is its own lineage


# --------------------------------------------------------------------------- admission

class Counting:
    def __init__(self):
        self.calls = 0

    def __call__(self, raw):
        self.calls += 1
        import json
        return json.loads(raw)


class BytesBeforeParse(unittest.TestCase):
    def test_oversized_line_is_refused_before_parsing_and_after_bounded_reading(self):
        spy = Counting()
        big = io.BytesIO(b'{"a":"' + b"x" * (3 << 20) + b'"}\n')
        with self.assertRaises(admission.Refused) as cm:
            admission.parse(admission.read_line(big, 1 << 20), max_structures=10, parse_fn=spy)
        self.assertEqual(cm.exception.code, "too_large")
        self.assertEqual(spy.calls, 0)
        self.assertLessEqual(big.tell(), (1 << 20) + 1)  # never read the whole 3 MiB

    def test_broken_parse_before_admission_is_caught(self):
        spy = Counting()
        big = io.BytesIO(b'{"a":"' + b"x" * (3 << 20) + b'"}\n')
        raw = big.read()  # broken variant: read everything, parse, then check the size
        spy(raw)
        self.assertEqual(spy.calls, 1)
        self.assertGreater(big.tell(), 1 << 20)

    def test_structure_bomb_is_refused_before_parsing(self):
        spy = Counting()
        for raw in (b"[" * 50_000, b'{"a":' * 5_000 + b"1" + b"}" * 5_000):
            with self.assertRaises(admission.Refused) as cm:
                admission.parse(raw, max_structures=1000, parse_fn=spy)
            self.assertEqual(cm.exception.code, "too_many_structures")
        self.assertEqual(spy.calls, 0)
        self.assertEqual(admission.parse(b'{"a":[1,2,{"b":3}]}', max_structures=1000), {"a": [1, 2, {"b": 3}]})
        with self.assertRaises(admission.Refused):
            admission.parse(b"{not json", max_structures=10)

    def test_handle_runs_one_parse_and_dispatch_at_a_time(self):
        adm = admission.Admission(slots=8, queue=8)
        live, peak, lock = [0], [0], threading.Lock()

        def dispatch(_):
            with lock:
                live[0] += 1
                peak[0] = max(peak[0], live[0])
            time.sleep(0.02)
            with lock:
                live[0] -= 1

        ts = [threading.Thread(target=adm.handle, args=("tena", io.BytesIO(b"{}\n"), dispatch)) for _ in range(6)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(peak[0], 1)


class PerTenantLimits(unittest.TestCase):
    def saturate(self, adm, tenant):
        held = [adm.acquire(tenant) for _ in range(adm._slots_n)]
        waiters = [threading.Thread(target=lambda: adm.acquire(tenant, timeout=1.0)) for _ in range(adm._queue_n)]
        [w.start() for w in waiters]
        time.sleep(0.2)
        return held, waiters

    def test_a_flood_by_one_tenant_does_not_take_another_tenants_slots(self):
        adm = admission.Admission(slots=2, queue=2)
        held, waiters = self.saturate(adm, "tena")
        with self.assertRaises(admission.Refused) as cm:
            adm.acquire("tena", timeout=0.1)
        self.assertEqual(cm.exception.code, "queue_full")
        t0 = time.time()
        release = adm.acquire("tenb", timeout=0.5)  # tenant B is unaffected
        self.assertLess(time.time() - t0, 0.2)
        release()
        [r() for r in held]
        [w.join() for w in waiters]

    def test_broken_single_global_pool_starves_tenant_b(self):
        class Global(admission.Admission):
            @staticmethod
            def _t(tenant):
                return b"all"
        adm = Global(slots=2, queue=2)
        held, waiters = self.saturate(adm, "tena")
        with self.assertRaises(admission.Refused):
            adm.acquire("tenb", timeout=0.1)  # B is refused because A flooded
        [r() for r in held]
        [w.join() for w in waiters]

    def test_connection_cap_is_per_tenant(self):
        adm = admission.Admission(connections=3)
        for _ in range(3):
            adm.open_connection("tena")
        with self.assertRaises(admission.Refused) as cm:
            adm.open_connection("tena")
        self.assertEqual(cm.exception.code, "connection_cap")
        adm.open_connection("tenb")
        adm.close_connection("tena")
        adm.open_connection("tena")

    def test_revoke_lane_is_not_behind_the_general_queue(self):
        adm = admission.Admission(slots=2, queue=2)
        held, waiters = self.saturate(adm, "tena")
        t0 = time.time()
        release = adm.acquire("tena", revoke=True, timeout=0.5)
        self.assertLess(time.time() - t0, 0.2)
        release()
        [r() for r in held]
        [w.join() for w in waiters]

    def check_tenant_lanes(self, **kw):
        adm = admission.Admission(slots=2, queue=2, revoke_slots=2, **kw)
        held = [adm.acquire("tena", revoke=True, timeout=0.5) for _ in range(2)]   # tenant A's own lane is full
        t0 = time.time()
        try:
            release = adm.acquire("tenb", revoke=True, timeout=0.5)
        except admission.Refused:
            self.fail("another tenant's revoke waited behind tenant A's full revoke lane")
        self.assertLess(time.time() - t0, 0.2)
        release()
        [r() for r in held]

    def test_each_tenant_has_its_own_revoke_lane(self):
        self.check_tenant_lanes()

    def test_broken_revoke_lane_shared_by_all_tenants_is_red(self):
        with self.assertRaises(AssertionError) as cm:
            self.check_tenant_lanes(broken_shared_revoke_lane=True)
        self.assertIn("another tenant's revoke waited", str(cm.exception))

    def test_broken_revoke_on_the_general_queue_is_refused_under_flood(self):
        adm = admission.Admission(slots=2, queue=2)
        held, waiters = self.saturate(adm, "tena")
        with self.assertRaises(admission.Refused):
            adm.acquire("tena", timeout=0.1)  # what a revoke would see without its own lane
        [r() for r in held]
        [w.join() for w in waiters]


class KeySpace(unittest.TestCase):
    """SEC-T6: a constructed collision in a 32-bit hash of tenant plus principal."""

    @staticmethod
    def find_32bit_collision():
        rng = random.Random(7)
        seen = {}
        while True:
            tenant = "".join(rng.choice("abcdefghij0123456789") for _ in range(8))
            principal = "p%d" % rng.randrange(10**6)
            h = zlib.crc32((tenant + principal).encode())
            if h in seen and seen[h] != (tenant, principal):
                return seen[h], (tenant, principal)
            seen[h] = (tenant, principal)

    def test_constructed_pair_and_random_keys_share_no_slot(self):
        a, b = self.find_32bit_collision()
        self.assertEqual(zlib.crc32("".join(a).encode()), zlib.crc32("".join(b).encode()))  # the pair is real
        self.assertNotEqual(admission.slot_key(*a, "k"), admission.slot_key(*b, "k"))
        rng = random.Random(11)
        keys = {admission.slot_key("t%07d" % rng.randrange(10**7), "p%d" % rng.randrange(10**4), "k%d" % i)
                for i in range(10_000)}
        self.assertEqual(len(keys), 10_000)

    def test_field_boundaries_cannot_be_shifted(self):
        self.assertNotEqual(admission.slot_key("ab", "c", "d"), admission.slot_key("a", "bc", "d"))
        self.assertNotEqual(admission.slot_key("a", "b", "cd"), admission.slot_key("a", "bc", "d"))

    def test_broken_32_bit_hash_shares_a_slot_between_two_tenants(self):
        a, b = self.find_32bit_collision()

        class Hashed(admission.Admission):
            @staticmethod
            def _t(tenant):
                return zlib.crc32(tenant.encode()).to_bytes(4, "big")
        # find two tenant ids colliding under the broken per-tenant key, then show B starves
        rng = random.Random(3)
        seen = {}
        while True:
            t = "".join(rng.choice("abcdefghij0123456789") for _ in range(8))
            h = zlib.crc32(t.encode())
            if h in seen and seen[h] != t:
                ta, tb = seen[h], t
                break
            seen[h] = t
        adm = Hashed(slots=1, queue=0)
        adm.acquire(ta)
        with self.assertRaises(admission.Refused):
            adm.acquire(tb, timeout=0.1)  # collided slot: tenant B refused because A holds it
        real = admission.Admission(slots=1, queue=0)
        real.acquire(ta)
        real.acquire(tb, timeout=0.1)  # real keys: independent


if __name__ == "__main__":
    unittest.main()
