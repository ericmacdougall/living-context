"""The deterministic, content-dependent stub model (DESIGN 18.2; BUILD-PLAN Step 8). No network, no model.

It must depend on content so the shuffled-summary and random-edge controls can fail at the stub gate.
All outputs are deterministic given inputs. Titles are never written here (INV-7: titles are code-built).
Children are presented as local labels c1..cN mapped back to ids by code, so prompts carry no ids.
"""
from __future__ import annotations

import math
import re
import threading
from collections import Counter
from typing import Any, Mapping

from ..embed import stub as emb

_SENT = re.compile(r"(?<=[.!?])\s+")
_STOP = frozenset("a an and are as at be but by for from has have he her his i in is it its of on or she that the their they this to was we were will with you".split())


def _terms(text: str) -> list[str]:
    return [t for t in emb.tokens(text) if t not in _STOP and len(t) > 2]


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENT.split(text) if s.strip()]


def _bm25(query: list[str], docs: list[list[str]], k1: float = 1.2, b: float = 0.75) -> list[float]:
    n = len(docs)
    avg = (sum(len(d) for d in docs) / n) or 1.0
    df = Counter(t for d in docs for t in set(d))
    out = []
    for d in docs:
        tf, s = Counter(d), 0.0
        for t in set(query):
            if tf[t]:
                idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
                s += idf * tf[t] * (k1 + 1) / (tf[t] + k1 * (1 - b + b * len(d) / avg))
        out.append(s)
    return out


def _grams(text: str, n: int = 4) -> set[str]:
    s = re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()
    return {s[i:i + n] for i in range(max(0, len(s) - n + 1))}


class StubBackend:
    name = "stub"

    def __init__(self):
        self.invocations = 0
        self._lock = threading.Lock()

    def run(self, task: str, inputs: Mapping[str, Any]) -> dict:
        with self._lock:
            self.invocations += 1
        return getattr(self, "_" + task)(inputs)

    # route: BM25 of the prompt over each child's title, gist and entities plus cosine of hashed vectors
    def _route(self, i):
        q = i["query"]
        kids = i["children"]
        docs = [_terms(" ".join([k.get("title", ""), k.get("gist", ""), " ".join(k.get("entities", []))])) for k in kids]
        qt = _terms(q)
        bm = _bm25(qt, docs)
        top = max(bm) or 1.0
        qv = emb.embed(q)
        scored = []
        for k, d, s in zip(kids, docs, bm):
            cos = emb.cosine(qv, emb.embed(" ".join(d)))
            scored.append((s / top * 0.7 + cos * 0.3, s, k["label"], d))
        keep = [t for t in sorted(scored, key=lambda t: (-t[0], t[2])) if t[1] > 0 and t[0] >= i.get("threshold", 0.2)]
        keep = keep[: i.get("b_fanout", 6)]
        reasons = {lab: "matched " + " ".join(sorted(set(qt) & set(d))[:10]) for _, _, lab, d in keep}
        conf = {lab: 3 if sc >= 0.7 else 2 if sc >= 0.45 else 1 for sc, _, lab, _ in keep}  # 0 to 3, by score
        return {"selected": [lab for _, _, lab, _ in keep], "reasons": reasons, "confidence": conf}

    # extractive builds: no title; gist = best sentence; facts = top 3 sentences
    def _build_l1(self, i):
        entries, tops = [], Counter()
        for n, leaf in enumerate(i["leaves"], start=1):
            sents = _sentences(leaf["text"]) or [leaf["text"]]
            tf = Counter(_terms(leaf["text"]))
            tops.update(tf)
            ranked = sorted(sents, key=lambda s: (-sum(tf[t] for t in _terms(s)) / (1 + len(_terms(s))), s))
            entries.append({"label": f"c{n}", "gist": ranked[0], "facts": ranked[:3], "entities": sorted(tf, key=lambda t: (-tf[t], t))[:4]})
        titles = " ".join(l.get("title", "") for l in i["leaves"])
        return {"entries": entries, "summary": (titles + " " + " ".join(t for t, _ in sorted(tops.items(), key=lambda kv: (-kv[1], kv[0]))[:8])).strip()}

    def _build_upper(self, i):
        tops = Counter()
        for c in i["children"]:
            tops.update(_terms(c.get("summary", "") + " " + " ".join(c.get("facts", []))))
        titles = " ".join(c.get("title", "") for c in i["children"])
        return {"summary": (titles + " " + " ".join(t for t, _ in sorted(tops.items(), key=lambda kv: (-kv[1], kv[0]))[:8])).strip()}

    def _organise(self, i):
        groups: dict[str, list] = {}
        for p in i["pointers"]:
            groups.setdefault(p["path"], []).append(p)
        return {"outline": [{"label": path, "pointers": [p["pointer"] for p in sorted(ps, key=lambda p: (-p.get("score", 0), p["pointer"]))]}
                            for path, ps in sorted(groups.items())]}

    def _type_edges(self, i):
        edges = []
        items = i["items"]
        for a in range(len(items)):
            for b in range(a + 1, len(items)):
                ea, eb = set(items[a].get("entities", [])), set(items[b].get("entities", []))
                if ea & eb:
                    kind = "contradicts" if re.search(r"\bnot\b|\bno\b|never", items[a]["text"] + items[b]["text"]) and \
                        re.findall(r"\d+", items[a]["text"]) != re.findall(r"\d+", items[b]["text"]) else "same_entity"
                    edges.append({"src": items[a]["label"], "dst": items[b]["label"], "type": kind})
        return {"edges": edges}

    # judge: character 4-gram Jaccard with its own tokenizer, a different function from route
    def _judge_relevance(self, i):
        a, b = _grams(i["query"]), _grams(i["text"])
        s = len(a & b) / (len(a | b) or 1)
        return {"score": float(s), "relevant": s >= i.get("threshold", 0.08)}

    def _query_variants(self, i):
        t = _terms(i["query"])
        return {"variants": [" ".join(t), " ".join(reversed(t)), "about " + " ".join(t[:3])]}

    def _synthetic_queries(self, i):
        t = Counter(_terms(i["text"]))
        top = [w for w, _ in sorted(t.items(), key=lambda kv: (-kv[1], kv[0]))[:6]]
        return {"queries": [" ".join(top[:2]), " ".join(top[2:4]), " ".join(top[:1] + top[4:5])]}

    def _best_sentence(self, i):
        q = set(_terms(i["question"]))
        sents = [s for ctx in i.get("context", []) for s in _sentences(ctx)] or [""]
        return max(sents, key=lambda s: (len(q & set(_terms(s))), -len(s), s))

    def _ask(self, i):
        return {"answer": self._best_sentence(i)}

    def _reader_answer(self, i):
        return {"answer": self._best_sentence(i)}

    def _answer_judge(self, i):
        gold = set(_terms(i["gold"]))
        got = set(_terms(i["answer"]))
        return {"correct": bool(gold) and len(gold & got) / len(gold) >= 0.6}
