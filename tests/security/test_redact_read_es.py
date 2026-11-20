"""BUILD-PLAN Step 14: the redact set is consulted by the read path between the boundary and the physical purge (SEC-E1 window).
Needs LC_TEST_ES_URL, LC_TEST_CA, LC_TEST_PW. At the pause right after the boundary nothing has been deleted yet, so direct search
and the tree view would still return the erased subject's documents. Broken control (set not consulted) is red."""
import unittest

from living_context.policy import Principal, Role
from living_context.recall.direct import DirectSearch
from living_context.recall.redact import RedactedView
from living_context.recall.view import EsView
from tests.security import test_erasure_es as E


class RedactedRead(E.ErasureOnElasticsearch):
    def window(self, consult, only=None, **kw):
        self.scenario()
        seen = {}
        p = Principal(E.T, "p-base", frozenset([Role.READER]), frozenset(["base"]), frozenset())

        def pause(step):
            if step != "boundary":
                return
            red = self.state.is_redacted if consult else (lambda i: False)
            ds = DirectSearch(lambda c: self.gate.client(E.T, f"read-{c}"), E.T, redacted=red)
            seen["direct"] = [h.doc_id for h in ds.search(p, E.LAT, E.MARK, size=30)]
            view = RedactedView(EsView(self.gate.client(E.T, "read-base"), E.T, "base", "topic"), red)
            texts = []
            for ref in view.top():
                for e in view.entries(ref):
                    texts.append(str(e))
                    if not e.get("is_leaf"):
                        texts += [str(x) for x in view.entries(f"{e['child_id']}:{e['child_version']}")]
            seen["tree"] = texts
            # an upper node reached only through transitive lineage (its subjects are stripped by `scenario`) must be in the redact set
            # the boundary published: until the purge reaches it, a parent's entry would show its gist
            docs = self.node_docs()
            erased_l1 = {i for i, d in docs.items() if d["level"] == 1 and set(d.get("lin_leaf_ids", [])) & set(self.erased_ids)}
            upper = [i for i, d in docs.items() if d["level"] == 2 and
                     any(f"{c['child_id']}:{c['child_version']}" in erased_l1 for c in d["children"])]
            seen["upper"] = (upper, [i for i in upper if not red(i)])

        r = self.executor(pause=pause, purge_deadline_s=2, **kw).erase_subject(self.subject, apply_guard=self.guard_fn())
        self.assertTrue(r.seq)
        if only in (None, "direct"):
            self.assertEqual([i for i in seen["direct"] if i.split(":")[0] in self.erased_ids], [],
                             "direct search returned an erased leaf after the boundary")
        if only in (None, "upper"):
            self.assertTrue(seen["upper"][0], "premise: an upper node sits above an erased node")
            self.assertEqual(seen["upper"][1], [], "an upper node reached only through transitive lineage was not redacted at the boundary")
        if only in (None, "tree"):
            self.assertFalse([t for t in seen["tree"] if E.MARK in t], "the tree view showed erased content after the boundary")

    def test_read_path_consults_the_redact_set(self):
        self.window(True)

    def test_broken_redact_set_not_consulted_is_red_for_direct_search(self):
        with self.assertRaises(AssertionError) as cm:
            self.window(False, only="direct")
        self.assertIn("direct search returned", str(cm.exception))

    def test_broken_redact_set_not_consulted_is_red_for_the_tree_view(self):
        with self.assertRaises(AssertionError) as cm:
            self.window(False, only="tree")
        self.assertIn("tree view showed", str(cm.exception))

    def test_broken_tombstone_published_before_the_closure_is_complete_leaks_the_upper_node_after_the_boundary(self):
        with self.assertRaises(AssertionError) as cm:
            self.window(True, only="upper", broken_publish_before_closure=True)
        self.assertIn("was not redacted at the boundary", str(cm.exception))


for _n in [n for n in dir(E.ErasureOnElasticsearch) if n.startswith("test_") and n not in RedactedRead.__dict__]:
    setattr(RedactedRead, _n, None)  # only this module's tests run; the inherited scenario helpers are reused


if __name__ == "__main__":
    unittest.main()
