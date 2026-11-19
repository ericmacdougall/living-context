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
    def window(self, consult):
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

        r = self.executor(pause=pause, purge_deadline_s=2).erase_subject(self.subject, apply_guard=self.guard_fn())
        self.assertTrue(r.seq)
        self.assertEqual([i for i in seen["direct"] if i.split(":")[0] in self.erased_ids], [],
                         "direct search returned an erased leaf after the boundary")
        self.assertFalse([t for t in seen["tree"] if E.MARK in t], "the tree view showed erased content after the boundary")

    def test_read_path_consults_the_redact_set(self):
        self.window(True)

    def test_broken_redact_set_not_consulted_is_red(self):
        with self.assertRaises(AssertionError) as cm:
            self.window(False)
        self.assertIn("after the boundary", str(cm.exception))


for _n in [n for n in dir(E.ErasureOnElasticsearch) if n.startswith("test_") and n not in RedactedRead.__dict__]:
    setattr(RedactedRead, _n, None)  # only this module's tests run; the inherited scenario helpers are reused


if __name__ == "__main__":
    unittest.main()
