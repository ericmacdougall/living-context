"""The real embedding backend (BUILD-PLAN 1.3, Step 8). The pure parts run anywhere; the model tests run when LC_E5_DIR names a
directory holding the pinned files (and torch and transformers are installed), which is the case on the evaluation host.

Broken variants shown red here: no prefix, no normalisation, a file with a wrong checksum, the stub standing in for the real
backend in a real stage, one FIFO queue instead of the per-tenant fair queue."""
import hashlib
import math
import os
import tempfile
import threading
import unittest
from pathlib import Path

from living_context.embed import e5, stub
from living_context.embed.port import EmbedPort
from living_context.model.fairq import FairLimiter

MODEL_DIR = os.environ.get("LC_E5_DIR", "")
HAVE_MODEL = bool(MODEL_DIR) and Path(MODEL_DIR).is_dir()


class PureTests(unittest.TestCase):
    def test_windows(self):
        self.assertEqual(e5.windows([]), [[]])
        self.assertEqual([len(w) for w in e5.windows(list(range(1000)))], [480, 480, 40])
        self.assertEqual([len(w) for w in e5.windows(list(range(480)))], [480])

    def test_mean_normalised(self):
        self.assertEqual(e5.mean_normalised([[1.0, 0.0]]), [1.0, 0.0])
        v = e5.mean_normalised([[1.0, 0.0], [0.0, 1.0]])
        self.assertAlmostEqual(math.sqrt(sum(x * x for x in v)), 1.0)
        self.assertAlmostEqual(v[0], v[1])

    def test_pins_cover_every_file_and_are_sha256(self):
        self.assertEqual(set(e5.FILE_SHA256), {"config.json", "model.safetensors", "sentencepiece.bpe.model",
                                               "special_tokens_map.json", "tokenizer.json", "tokenizer_config.json"})
        for h in e5.FILE_SHA256.values():
            self.assertRegex(h, r"^[0-9a-f]{64}$")
        self.assertEqual(len(e5.REVISION), 40)

    def test_verify_files_accepts_pinned_and_refuses_a_changed_or_missing_file(self):
        with tempfile.TemporaryDirectory() as d:
            files = {"a.bin": b"alpha", "b.bin": b"beta"}
            pins = {k: hashlib.sha256(v).hexdigest() for k, v in files.items()}
            for k, v in files.items():
                (Path(d) / k).write_bytes(v)
            e5.verify_files(Path(d), pins)
            (Path(d) / "b.bin").write_bytes(b"betA")                       # broken variant: a file that is not the pinned one
            with self.assertRaises(e5.PinError):
                e5.verify_files(Path(d), pins)
            (Path(d) / "b.bin").unlink()
            with self.assertRaises(e5.PinError):
                e5.verify_files(Path(d), pins)
        with self.assertRaises(e5.PinError):
            e5.verify_files(Path("."), {})                                  # no pins recorded: refuse, never skip

    def test_stub_cannot_stand_in_for_the_real_backend(self):
        class Fake:
            is_real, model_id = True, e5.MODEL_ID
        e5.require_real(Fake(), stage="S2")
        with self.assertRaises(e5.PinError):
            e5.require_real(stub, stage="S2")                               # the stub module
        with self.assertRaises(e5.PinError):
            e5.require_real(stub.embed, stage="S2")                         # the stub function
        class WrongRev:
            is_real, model_id = True, "intfloat/multilingual-e5-small@other"
        with self.assertRaises(e5.PinError):
            e5.require_real(WrongRev(), stage="S2")

    def test_ports_share_one_fair_queue_and_pick_the_right_backend(self):
        class Fake:
            def passage(self, t): return [1.0, 0.0]
            def query(self, t): return [0.0, 1.0]
        pp, qp = e5.ports(Fake(), limit=1)
        self.assertEqual(pp.embed("t1", "x"), [1.0, 0.0])
        self.assertEqual(qp.embed("t1", "x"), [0.0, 1.0])
        self.assertIs(pp._lim, qp._lim)

    def test_fair_queue_gives_a_quiet_tenant_its_turn_and_fifo_does_not(self):
        from living_context.model.fairq import FifoLimiter

        def quiet_position(lim_cls):
            lim, order, gate = lim_cls(1), [], threading.Event()
            port = EmbedPort(lim, backend=lambda t: (gate.wait(2), order.append(t))[1])
            ts = [threading.Thread(target=port.embed, args=("flood", "f%d" % i)) for i in range(6)]
            for t in ts:
                t.start()
                threading.Event().wait(0.03)
            q = threading.Thread(target=port.embed, args=("quiet", "q"))
            q.start()
            threading.Event().wait(0.1)
            gate.set()
            for t in ts + [q]:
                t.join()
            return order.index("q")
        self.assertLessEqual(quiet_position(FairLimiter), 2)
        self.assertGreaterEqual(quiet_position(FifoLimiter), 5)


@unittest.skipUnless(HAVE_MODEL, "LC_E5_DIR is not set: the model tests run on the evaluation host")
class ModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = e5.E5Embedder(MODEL_DIR, threads=2)
        cls.texts = ["The quarterly report is due on Friday afternoon.", "Mi gato duerme todo el dia en el sofa.",
                     "user: what is the capital of France?\nassistant: Paris is the capital of France."]

    def reference(self, kind, text):
        """The textbook computation, independent of this module's id building: the literal prefixed string through the
        tokenizer's own special-token path, mean pooling, L2 normalisation."""
        import torch
        m = self.m
        enc = m._tok(("query: " if kind == "query" else "passage: ") + text, return_tensors="pt", truncation=True, max_length=512)
        with torch.inference_mode():
            h = m._model(**enc).last_hidden_state
        mask = enc["attention_mask"].unsqueeze(-1).to(h.dtype)
        v = torch.nn.functional.normalize((h * mask).sum(1) / mask.sum(1), dim=1)
        return v[0].double().tolist()

    def close(self, a, b, tol=1e-5):
        return max(abs(x - y) for x, y in zip(a, b)) <= tol

    def test_dimension_and_norm(self):
        for t in self.texts:
            for v in (self.m.passage(t), self.m.query(t)):
                self.assertEqual(len(v), e5.DIMS)
                self.assertAlmostEqual(math.sqrt(sum(x * x for x in v)), 1.0, places=6)

    def test_determinism(self):
        self.assertEqual(self.m.passage(self.texts[0]), self.m.passage(self.texts[0]))
        self.assertEqual(self.m.query(self.texts[1]), self.m.query(self.texts[1]))

    def test_matches_the_textbook_reference_with_prefixes(self):
        for t in self.texts:
            self.assertTrue(self.close(self.m.passage(t), self.reference("passage", t)))
            self.assertTrue(self.close(self.m.query(t), self.reference("query", t)))
        self.assertFalse(self.close(self.m.passage(self.texts[0]), self.m.query(self.texts[0]), 1e-3))   # the prefixes differ

    def test_broken_variants_are_caught_by_the_reference_check(self):
        no_prefix = e5.E5Embedder(MODEL_DIR, threads=2, broken_no_prefix=True)
        no_norm = e5.E5Embedder(MODEL_DIR, threads=2, broken_no_normalise=True)
        t = self.texts[0]
        self.assertFalse(self.close(no_prefix.passage(t), self.reference("passage", t), 1e-3))
        self.assertFalse(self.close(no_prefix.query(t), self.reference("query", t), 1e-3))
        self.assertGreater(abs(math.sqrt(sum(x * x for x in no_norm.query(t))) - 1.0), 1e-3)

    def test_retrieval_sanity(self):
        docs = ["Photosynthesis converts sunlight, water and carbon dioxide into glucose and oxygen in plants.",
                "The 2018 World Cup final was won by France against Croatia.",
                "To reset the router, hold the small button on the back for ten seconds.",
                "Sourdough bread needs a starter, flour, water and a long slow fermentation.",
                "El tratado fue firmado por los dos paises despues de largas negociaciones."]
        qs = ["how do plants make sugar from light", "who won the football world cup in 2018", "how can I restart my wifi router",
              "what do I need to bake sourdough", "quien firmo el tratado"]
        dv = [self.m.passage(d) for d in docs]
        hits = 0
        for i, q in enumerate(qs):
            qv = self.m.query(q)
            sc = [sum(a * b for a, b in zip(qv, d)) for d in dv]
            hits += sc.index(max(sc)) == i
        self.assertEqual(hits, len(qs))

    def test_batch_equals_single(self):
        batch = self.m.passages(self.texts)
        for t, v in zip(self.texts, batch):
            self.assertTrue(self.close(v, self.m.passage(t)))
        # padding must not leak: one window alone against the same window padded beside a longer one
        a = self.m._tok(self.texts[0], add_special_tokens=False)["input_ids"]
        b = self.m._tok(" ".join(self.texts), add_special_tokens=False)["input_ids"]
        mk = lambda ids: [self.m._cls] + ids + [self.m._sep]
        alone = self.m._run([mk(a)])[0]
        padded = self.m._run([mk(a), mk(b)])[0]
        self.assertTrue(self.close(alone, padded))

    def test_long_text_is_windowed_and_mean_pooled(self):
        long = " ".join("sentence number %d about gardens and rivers and mountains." % i for i in range(260))
        ids = self.m._tok(long, add_special_tokens=False)["input_ids"]
        self.assertGreater(len(ids), 960)                                  # at least three windows
        v = self.m.passage(long)
        pre = self.m._prefix_ids["passage"]
        wins = [self.m._run([[self.m._cls] + pre + w + [self.m._sep]])[0] for w in e5.windows(ids)]
        self.assertTrue(self.close(v, e5.mean_normalised(wins)))
        self.assertAlmostEqual(math.sqrt(sum(x * x for x in v)), 1.0, places=6)


if __name__ == "__main__":
    unittest.main()
