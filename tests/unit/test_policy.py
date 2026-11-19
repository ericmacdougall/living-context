"""BUILD-PLAN Step 3: policy core, table-driven over every (role, operation, class)."""
import itertools
import unittest
from dataclasses import replace

from living_context.policy import (ClassError, ClassLattice, NoJoin, Op, Principal, Role, decide,
                                   derived_class)
from living_context.policy.roles import CLASS_READ

T, OTHER = "tenaaa", "tenbbb"
LAT = ClassLattice(["base", "restricted", "hrdept"], [("restricted", "base"), ("hrdept", "base")])


def principal(role, tenant=T, token="p1", read=("base",), write=("base",), own="base", **kw):
    return Principal(tenant, token, frozenset([role]), frozenset(read), frozenset(write), own, **kw)


PLAIN = Principal(T, "plain", frozenset([Role.WRITER]))
ADMIN2 = Principal(T, "adm2", frozenset([Role.TENANT_ADMIN]))
PRIV_TARGET = Principal(T, "tgt", frozenset([Role.REVOKER]))

# Independent expectation, written from DESIGN 4.3 and not from the implementation.
CLASS_OPS = {  # (role, op) -> which set governs
    (Role.READER, Op.RECALL): "read", (Role.READER, Op.LOOKUP): "read",
    (Role.READER, Op.ASK): "read", (Role.READER, Op.EXPLORE): "read",
    (Role.ERASURE_REQUESTER, Op.ERASE_RECEIPT_TOTALS): "read",
    (Role.WRITER, Op.REMEMBER): "write",
    (Role.REORGANISER, Op.REORG_READ): "own", (Role.REORGANISER, Op.REORG_WRITE): "own",
}
FREE_OPS = {(Role.ERASURE_REQUESTER, Op.ERASE_REQUEST), (Role.ERASURE_EXECUTOR, Op.ERASE_EXECUTE),
            (Role.VERIFIER, Op.VERIFY_READ), (Role.TENANT_KEY_OWNER, Op.ROTATE_KEYS),
            (Role.OPERATOR, Op.PROVISION), (Role.OPERATOR, Op.BREAK_GLASS_REVOKE)}
PEOPLE_OPS = {(Role.TENANT_ADMIN, Op.GRANT), (Role.TENANT_ADMIN, Op.REVOKE), (Role.REVOKER, Op.REVOKE),
              (Role.TENANT_ADMIN, Op.REVOKE_PRIVILEGED)}
STATES = {"in": "base", "out": "restricted", "none": None}


def expected(role, op, state):
    if (role, op) in CLASS_OPS:
        return state == "in"
    return (role, op) in FREE_OPS | PEOPLE_OPS


def call(fn, role, op, state, **over):
    p = principal(role)
    kw = dict(tenant=T, lattice=LAT, cls=STATES[state])
    if op in (Op.GRANT, Op.REVOKE):
        kw["target"] = PLAIN
    if op is Op.REVOKE_PRIVILEGED:
        kw["target"], kw["co_signer"] = PRIV_TARGET, ADMIN2
    if op is Op.REORG_WRITE:
        kw["input_classes"] = ("base",)
    kw.update(over)
    return bool(fn(p, op, **kw))


def check_table(fn):
    """Every (role, operation, class state); returns the mismatches."""
    bad = []
    for role, op, state in itertools.product(Role, Op, STATES):
        if call(fn, role, op, state) != expected(role, op, state):
            bad.append((role.value, op.value, state))
    return bad


def broken_reader_any_class(p, op, **kw):
    """'reader may read any class of its tenant'"""
    if op in CLASS_READ and Role.READER in p.roles and p.tenant == kw["tenant"] and kw["lattice"].declared(kw["cls"] or ""):
        return True
    return decide(p, op, **kw)


def broken_ignores_tenant(p, op, **kw):
    return decide(p, op, **{**kw, "tenant": p.tenant})


class DecisionTable(unittest.TestCase):
    def test_every_role_operation_class_combination(self):
        self.assertEqual(check_table(decide), [])
        self.assertEqual(len(list(itertools.product(Role, Op, STATES))), 10 * 18 * 3)

    def test_broken_reader_any_class_is_caught(self):
        bad = check_table(broken_reader_any_class)
        self.assertIn(("reader", "recall", "out"), bad)
        self.assertTrue(all(b[0] == "reader" for b in bad))

    def test_cross_tenant_is_refused_for_every_role_and_op(self):
        for role, op in itertools.product(Role, Op):
            if op in (Op.PROVISION, Op.BREAK_GLASS_REVOKE):
                continue
            self.assertFalse(decide(principal(role, tenant=OTHER), op, tenant=T, lattice=LAT, cls="base",
                                    target=PLAIN, co_signer=ADMIN2, input_classes=("base",)), (role, op))

    def test_broken_tenant_blind_variant_is_caught(self):
        p = principal(Role.READER, tenant=OTHER)
        self.assertFalse(decide(p, Op.RECALL, tenant=T, lattice=LAT, cls="base"))
        self.assertTrue(broken_ignores_tenant(p, Op.RECALL, tenant=T, lattice=LAT, cls="base"))

    def test_revoked_principal_and_malformed_sets_are_refused(self):
        self.assertFalse(decide(principal(Role.READER, revoked=True), Op.RECALL, tenant=T, lattice=LAT, cls="base"))
        # a read set that is not down-closed (restricted without base) is malformed
        bad = principal(Role.READER, read=("restricted",))
        self.assertFalse(decide(bad, Op.RECALL, tenant=T, lattice=LAT, cls="restricted"))
        good = principal(Role.READER, read=("restricted", "base"))
        self.assertTrue(decide(good, Op.RECALL, tenant=T, lattice=LAT, cls="base"))
        undeclared = principal(Role.READER, read=("base", "ghostly"))
        self.assertFalse(decide(undeclared, Op.RECALL, tenant=T, lattice=LAT, cls="base"))

    def test_no_role_holds_raw_content_read_and_unknown_ops_fail_closed(self):
        for role in Role:
            self.assertFalse(decide(principal(role), Op.READ_CONTENT_RAW, tenant=T, lattice=LAT, cls="base"))
        self.assertFalse(decide(principal(Role.READER), "recall", tenant=T, lattice=LAT, cls="base"))

    def test_multiple_roles_union_but_class_rules_still_apply(self):
        p = Principal(T, "multi", frozenset([Role.READER, Role.WRITER]), frozenset(["base"]), frozenset())
        self.assertTrue(decide(p, Op.RECALL, tenant=T, lattice=LAT, cls="base"))
        self.assertFalse(decide(p, Op.REMEMBER, tenant=T, lattice=LAT, cls="base"))  # empty write set

    def test_writer_cannot_read_and_reader_cannot_write(self):
        w = principal(Role.WRITER)
        r = principal(Role.READER)
        self.assertFalse(decide(w, Op.RECALL, tenant=T, lattice=LAT, cls="base"))
        self.assertFalse(decide(r, Op.REMEMBER, tenant=T, lattice=LAT, cls="base"))


class Reorganiser(unittest.TestCase):
    def test_only_own_class_and_nothing_derived_from_higher(self):
        hi = Principal(T, "re", frozenset([Role.REORGANISER]), own_class="restricted",
                       read_set=frozenset(["restricted", "base"]))
        lo = Principal(T, "rl", frozenset([Role.REORGANISER]), own_class="base", read_set=frozenset(["base"]))
        kw = dict(tenant=T, lattice=LAT)
        self.assertTrue(decide(hi, Op.REORG_WRITE, cls="restricted", input_classes=("base", "restricted"), **kw))
        self.assertFalse(decide(hi, Op.REORG_READ, cls="base", **kw))  # not the class it rebuilds
        self.assertFalse(decide(lo, Op.REORG_WRITE, cls="base", input_classes=("restricted",), **kw))
        self.assertFalse(decide(lo, Op.REORG_WRITE, cls="base", input_classes=(), **kw))
        # an incomparable class never flows in
        self.assertFalse(decide(hi, Op.REORG_WRITE, cls="restricted", input_classes=("hrdept",), **kw))


class PeopleOps(unittest.TestCase):
    kw = dict(tenant=T, lattice=LAT)

    def test_no_self_grant(self):
        a = principal(Role.TENANT_ADMIN, token="a1")
        self.assertFalse(decide(a, Op.GRANT, target=a, **self.kw))
        self.assertTrue(decide(a, Op.GRANT, target=PLAIN, **self.kw))
        self.assertFalse(decide(a, Op.GRANT, target=None, **self.kw))

    def test_single_administrator_cannot_revoke_a_revoker_or_an_administrator(self):
        a = principal(Role.TENANT_ADMIN, token="a1")
        self.assertFalse(decide(a, Op.REVOKE, target=PRIV_TARGET, **self.kw))
        self.assertFalse(decide(a, Op.REVOKE, target=ADMIN2, **self.kw))
        rv = principal(Role.REVOKER, token="rv")
        self.assertFalse(decide(rv, Op.REVOKE, target=ADMIN2, **self.kw))
        self.assertTrue(decide(rv, Op.REVOKE, target=PLAIN, **self.kw))

    def test_two_distinct_administrators_revoke_a_privileged_principal(self):
        a = principal(Role.TENANT_ADMIN, token="a1")
        ok = decide(a, Op.REVOKE_PRIVILEGED, target=PRIV_TARGET, co_signer=ADMIN2, **self.kw)
        self.assertTrue(ok)
        for co in (None, a, replace(ADMIN2, revoked=True), replace(ADMIN2, tenant=OTHER),
                   Principal(T, "x", frozenset([Role.REVOKER]))):
            self.assertFalse(decide(a, Op.REVOKE_PRIVILEGED, target=PRIV_TARGET, co_signer=co, **self.kw), co)
        # the target cannot be one of the two signers
        self.assertFalse(decide(a, Op.REVOKE_PRIVILEGED, target=ADMIN2, co_signer=ADMIN2, **self.kw))
        self.assertFalse(decide(principal(Role.REVOKER), Op.REVOKE_PRIVILEGED, target=PRIV_TARGET,
                                co_signer=ADMIN2, **self.kw))

    def test_operator_break_glass_cannot_be_withdrawn_by_a_tenant_role(self):
        op = principal(Role.OPERATOR, tenant="platform")
        self.assertTrue(decide(op, Op.BREAK_GLASS_REVOKE, tenant=OTHER, lattice=LAT, target=ADMIN2))
        for role in Role:
            if role is not Role.OPERATOR:
                self.assertFalse(decide(principal(role), Op.BREAK_GLASS_REVOKE, tenant=T, lattice=LAT))


class Lattice(unittest.TestCase):
    def test_join_table(self):
        L = ClassLattice(["base", "restricted", "hrdept", "execs"],
                         [("restricted", "base"), ("hrdept", "base"), ("execs", "restricted"), ("execs", "hrdept")])
        rows = [(("base",), "base"), (("base", "restricted"), "restricted"), (("restricted", "base"), "restricted"),
                (("restricted", "hrdept"), "execs"), (("base", "restricted", "hrdept"), "execs"),
                (("hrdept", "hrdept"), "hrdept")]
        for inputs, want in rows:
            self.assertEqual(L.join(*inputs), want, inputs)

    def test_incomparable_without_a_dominating_class_is_not_built(self):
        with self.assertRaises(NoJoin):
            LAT.join("restricted", "hrdept")
        self.assertIsNone(derived_class(LAT, "restricted", "hrdept"))
        self.assertEqual(derived_class(LAT, "base", "restricted"), "restricted")

    def test_no_least_upper_bound_is_not_built(self):
        L = ClassLattice(["clsa", "clsb", "clsc", "clsd"],
                         [("clsc", "clsa"), ("clsc", "clsb"), ("clsd", "clsa"), ("clsd", "clsb")])
        with self.assertRaises(NoJoin):
            L.join("clsa", "clsb")

    def test_undeclared_inputs_and_empty_joins_fail(self):
        with self.assertRaises(NoJoin):
            LAT.join("base", "ghostly")
        with self.assertRaises(ClassError):
            LAT.join()

    def test_join_is_commutative_and_idempotent_over_every_pair(self):
        for a, b in itertools.product(LAT.classes, repeat=2):
            try:
                j1 = LAT.join(a, b)
            except NoJoin:
                with self.assertRaises(NoJoin):
                    LAT.join(b, a)
                continue
            self.assertEqual(j1, LAT.join(b, a))
            self.assertEqual(LAT.join(j1, a), j1)

    def test_broken_first_input_join_is_caught(self):
        def first_input_join(*inputs):
            return inputs[0]
        rows = [(("base", "restricted"), "restricted"), (("restricted", "base"), "restricted")]
        wrong = [i for i, w in rows if first_input_join(*i) != w]
        self.assertEqual(len(wrong), 1)  # order-dependent: the table catches it
        for inputs, want in rows:
            self.assertEqual(LAT.join(*inputs), want)

    def test_declaration_validation(self):
        for bad in (dict(classes=[]), dict(classes=["ab"]), dict(classes=["Base1"]), dict(classes=["ba-se"]),
                    dict(classes=["clsa", "clsb"], dominates=[("clsa", "clsb"), ("clsb", "clsa")]),
                    dict(classes=["clsa"], dominates=[("clsa", "clsz")]),
                    dict(classes=[f"cls{i}" for i in range(5)])):
            with self.assertRaises(ClassError, msg=bad):
                ClassLattice(**bad)

    def test_dominance_is_transitive_and_down_closure(self):
        L = ClassLattice(["clsa", "clsb", "clsc"], [("clsc", "clsb"), ("clsb", "clsa")])
        self.assertTrue(L.dominates("clsc", "clsa"))
        self.assertFalse(L.dominates("clsa", "clsc"))
        self.assertEqual(L.down_closure(["clsb"]), frozenset(["clsa", "clsb"]))
        self.assertTrue(L.is_down_closed(["clsa", "clsb"]))
        self.assertFalse(L.is_down_closed(["clsb"]))


if __name__ == "__main__":
    unittest.main()
