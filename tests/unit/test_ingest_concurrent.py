"""Concurrent ingest (EvalTenant.ingest_concurrent, OrderedWriter, embed_pool): the leaves, their order, their src_seq, the session map
and the stored documents equal a one-at-a-time `ingest` of the same pool; the up-front embedding equals the per-text embedding whatever
the worker count. The real Writer over an in-memory Elasticsearch (documents, op_type=create, compare-and-set on seq_no), a real
keystore in a temporary directory, the hashed stub embedder; no network, no model."""
import json
import re
import tempfile
import threading
import time
import unittest
from pathlib import Path

from evalkit import embed_pool, eval_tenant as etm, fixture
from evalkit.eval_tenant import CLS, EvalTenant, OrderedWriter
from living_context.config import Constants
from living_context.embed import stub
from living_context.ingest.subjects import Guard, TokenSource
from living_context.ingest.writer import Writer, WriterError
from living_context.keystore import Keystore
from living_context.policy import Principal, Role

T = "ingc"
DOC = re.compile(r"^/([^/]+)/_doc/([^?]+)(?:\?(.*))?$")


class MemEs:
    """Documents per index with seq_no/primary_term; create conflicts and compare-and-set mismatches answer 409. `delay` adds a random
    pause per request so concurrent writers interleave."""
    def __init__(self, delay=0.0, seed=1):
        import random
        self.docs, self.lock, self.seq, self.delay, self.rng, self.requests = {}, threading.Lock(), 0, delay, random.Random(seed), 0

    def request(self, method, path, body=None, **kw):
        if self.delay:
            with self.lock:
                d = self.rng.random() * self.delay
            time.sleep(d)
        with self.lock:
            self.requests += 1
            m = DOC.match(path)
            if m:
                idx, did, q = m.group(1), m.group(2), dict(p.split("=") for p in (m.group(3) or "").split("&") if p)
                key = (idx, did)
                if method == "GET":
                    if key not in self.docs:
                        return 404, {}
                    seq, src = self.docs[key]
                    return 200, {"_seq_no": seq, "_primary_term": 1, "_source": json.loads(json.dumps(src))}
                if method == "PUT":
                    if q.get("op_type") == "create" and key in self.docs:
                        return 409, {}
                    if "if_seq_no" in q and (key not in self.docs or self.docs[key][0] != int(q["if_seq_no"])):
                        return 409, {}
                    self.seq += 1
                    self.docs[key] = (self.seq, json.loads(json.dumps(body)))
                    return 201, {"_seq_no": self.seq}
            if path.endswith("/_refresh"):
                return 200, {}
            raise AssertionError("unexpected request %s %s" % (method, path))

    def index(self, suffix):
        return {did: src for (idx, did), (_, src) in self.docs.items() if idx == "lc-%s-%s" % (T, suffix)}


class Gate:
    def __init__(self, es):
        self.es = es

    def client(self, tenant, role):
        return self.es


def tenant(es, root, embed=stub.embed):
    """An EvalTenant over the in-memory Elasticsearch (the constructor's provisioning is the part left out)."""
    et = EvalTenant.__new__(EvalTenant)
    et.tenant, et.root, et.consts, et._embed, et._query_embed = T, Path(root), Constants(), embed, embed
    et._last_vec, et.a0_fields, et.admin, et.gate = None, (), es, Gate(es)
    et.ks = Keystore(Path(root) / "ks" / f"{T}.db", T)
    et.writer = Writer(lambda: es, et.ks, T, Guard(TokenSource(et.ks)), consts=et.consts, embed=et._emb)
    et.leaves, et.leaf_session, et.session_tokens, et._seen, et.max_seq = [], {}, {}, set(), 0
    et.principal = Principal(T, "p-eval", frozenset([Role.READER]), frozenset([CLS]), frozenset())
    return et


def pool(n=12, mates=6):
    fx = fixture.make("advantage", n, mates=mates)
    from evalkit import dataset as ds
    seen, out = set(), []
    for q in fx.questions:
        for _sid, turns in q.sessions:
            h = ds.turns_hash(turns)
            if h not in seen:
                seen.add(h)
                out.append((h, turns))
    return out


def stub_many(texts):
    return {t: stub.embed(t) for t in texts}


def shape(et, es):
    """What must not depend on the writer count: the leaf sequence without the random leaf ids, and the stored documents keyed by
    src_seq with the random and time fields removed."""
    leaves = [(l.text, l.title, l.embedding, l.size_tokens, l.src_seq, et.leaf_session[l.leaf_id]) for l in et.leaves]
    drop = {"created_at", "valid_from", "ingested_at", "lin_leaf_ids", "leaf_id", "ct", "nonce", "key_ref", "content_hash"}
    docs = {}
    for suffix in ("base-leaf", "src", "base-ctl", "ctl"):
        for did, src in es.index(suffix).items():
            if did == "seq":
                docs[("seq",)] = src["value"]
                continue
            docs[(suffix, src.get("kind"), src.get("src_seq", src.get("current_lv")), did.startswith("lh:"))] = \
                {k: v for k, v in src.items() if k not in drop}
    return leaves, et.session_tokens, et.max_seq, docs


class SameAsSerial(unittest.TestCase):
    def serial(self, p):
        es = MemEs()
        et = tenant(es, tempfile.mkdtemp())
        n = et.ingest(p)
        return n, et, es

    def test_concurrent_ingest_equals_one_at_a_time(self):
        p = pool()
        n1, et1, es1 = self.serial(p)
        self.assertGreater(n1, 20)
        for w in (1, 4, 9):
            es = MemEs(delay=0.002, seed=w)
            et = tenant(es, tempfile.mkdtemp())
            self.assertEqual(et.ingest_concurrent(p, writers=w, embed_many=stub_many), n1)
            self.assertEqual(shape(et, es), shape(et1, es1), w)
            self.assertEqual([l.src_seq for l in et.leaves], list(range(1, n1 + 1)))

    def test_a_repeated_content_is_skipped_and_a_second_ingest_continues_the_sequence(self):
        p = pool(6, 4)
        es = MemEs()
        et = tenant(es, tempfile.mkdtemp())
        a = et.ingest_concurrent(p[:5] + p[:2], writers=3, embed_many=stub_many)
        b = et.ingest_concurrent(p, writers=3, embed_many=stub_many)
        es1 = MemEs()
        et1 = tenant(es1, tempfile.mkdtemp())
        self.assertEqual((a + b, len(et.leaves)), (et1.ingest(p), len(et1.leaves)))
        self.assertEqual(shape(et, es), shape(et1, es1))

    def test_writes_really_overlap(self):
        p = pool()
        es = MemEs(delay=0.003)
        inflight, peak, lock = [0], [0], threading.Lock()
        orig = OrderedWriter.remember

        def counting(self, *a, **kw):
            with lock:
                inflight[0] += 1
                peak[0] = max(peak[0], inflight[0])
            try:
                return orig(self, *a, **kw)
            finally:
                with lock:
                    inflight[0] -= 1
        OrderedWriter.remember = counting
        try:
            tenant(es, tempfile.mkdtemp()).ingest_concurrent(p, writers=6, embed_many=stub_many)
        finally:
            OrderedWriter.remember = orig
        self.assertGreater(peak[0], 1)

    def test_a_refused_leaf_raises_and_nothing_after_it_is_recorded(self):
        p = pool(6, 4)
        es = MemEs()
        et = tenant(es, tempfile.mkdtemp())
        texts = [t for h, turns in p for t in etm.session_leaf_texts(turns, et.consts.leaf_cap)]
        bad = texts[7]
        orig = OrderedWriter.remember

        def refuse(self, principal, cls, text, **kw):
            if text == bad:
                from living_context.ingest.writer import REFUSED, WriteResult
                return WriteResult(REFUSED)
            return orig(self, principal, cls, text, **kw)
        OrderedWriter.remember = refuse
        try:
            with self.assertRaises(RuntimeError):
                et.ingest_concurrent(p, writers=4, embed_many=stub_many)
        finally:
            OrderedWriter.remember = orig
        self.assertEqual([l.text for l in et.leaves], texts[:7])

    def test_a_missing_vector_is_refused_before_any_write(self):
        es = MemEs()
        et = tenant(es, tempfile.mkdtemp())
        with self.assertRaises(RuntimeError):
            et.ingest_concurrent(pool(4, 3), writers=2, embed_many=lambda texts: {})
        self.assertEqual(es.requests, 0)

    def test_a_write_without_its_reserved_number_is_refused(self):
        es = MemEs()
        et = tenant(es, tempfile.mkdtemp())
        w = OrderedWriter(lambda: es, et.ks, T, Guard(TokenSource(et.ks)), consts=et.consts)
        with self.assertRaises(WriterError):
            w.remember("eval", CLS, "some text that is a leaf", refresh=False)
        self.assertEqual(w.reserve(5), 0)
        self.assertEqual(w.reserve(3), 5)                       # the counter the plain writer uses, advanced by the range


class Embed:
    """A picklable stand-in for the pinned embedder (per-text vectors from the hashed stub); counts its calls per process."""
    is_real = False
    calls = 0

    def passage(self, text):
        Embed.calls += 1
        return stub.embed("passage " + text)


def make_embed():
    return Embed()


class SlowEmbed(Embed):
    """Uneven per-text time, so worker processes finish out of input order."""
    def passage(self, text):
        time.sleep(0.03 if len(text) % 3 == 0 else 0.0)
        return super().passage(text)


def make_slow_embed():
    return SlowEmbed()


class UpFrontEmbedding(unittest.TestCase):
    texts = ["leaf %d about %s" % (i, w) for i, w in enumerate(["tea", "rain", "maps", "tea", "jazz"] * 7)] + ["leaf 0 about tea"]

    def test_vectors_equal_per_text_embedding_at_any_worker_count(self):
        want = {t: Embed().passage(t) for t in self.texts}
        for w in (1, 3):
            got = embed_pool.passages(self.texts, make_slow_embed, workers=w, chunksize=1)
            self.assertEqual(got, want, w)

    def test_duplicates_are_embedded_once_and_reused_vectors_are_not_embedded(self):
        Embed.calls = 0
        reuse = {embed_pool.text_key(self.texts[1]): [0.5] * 3}
        got = embed_pool.passages(self.texts, make_embed, workers=1, reuse=reuse)
        self.assertEqual(got[self.texts[1]], [0.5] * 3)
        self.assertEqual(Embed.calls, len(set(self.texts)) - 1)

    def test_progress_and_worker_guard(self):
        seen = []
        embed_pool.passages(self.texts[:5], make_embed, workers=2, progress=lambda i, n: seen.append((i, n)))
        self.assertEqual(seen[-1], (5, 5))
        with self.assertRaises(ValueError):
            embed_pool.passages(self.texts, make_embed, workers=0)
        self.assertTrue(embed_pool.exact_reuse(1))
        self.assertFalse(embed_pool.exact_reuse(2))


if __name__ == "__main__":
    unittest.main()
