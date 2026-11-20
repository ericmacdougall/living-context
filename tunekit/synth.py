"""Synthetic memory corpora for tuning (TUNING 2.1, generators G1 to G5): deterministic from (generator version, knobs, seed),
ground truth by construction, no model call of any kind.

Two variables the owner names, varied independently:

  * SIZE is one number, `n_leaves`. The generated corpus has exactly that many leaves.
  * COMPLEXITY is a vector of intensive knobs (per-leaf or per-topic quantities that do not mention the leaf count):
    `topic_size` (leaves per topic), `topic_overlap` (share of a topic's vocabulary drawn from a small common pool),
    `entity_reuse` (mean number of topics an entity appears in), `near_dup` (share of facts that get a same-wording sibling
    about another entity), `update_rate` (share of facts that have an older, superseded version), `session_len`,
    `paraphrase` (probability that a question names the attribute with a different surface form than the leaf), `alias_rate`
    (probability that a question names the entity by first or last name only, when that is still unambiguous).

`growth = "wide"` keeps the complexity statistics constant while size grows (topics and entities are added with the leaves).
`growth = "deep"` fixes the topic count, so topics and entities get deeper with size; that changes the complexity statistics on
purpose and is the control that the independence test must see move.

What this text is NOT: it is template text over a controlled vocabulary. Topic words are partly real common words (the shared
pool) and partly invented pronounceable strings; entity names are invented; attribute concepts are real English phrases with
three surface forms each. It has no real-language ambiguity, no negation, no long-range reference. Lexical overlap between a
question and its gold leaf is controlled (entity name, two topic words, an attribute form), which usually makes BM25 look
better than on real data; `paraphrase` is the only knob that removes lexical overlap, and only for the attribute phrase.
"""
from __future__ import annotations

import hashlib
import json
import math
import random
from dataclasses import asdict, dataclass, field, replace
from typing import Dict, List, Optional, Sequence, Tuple

GEN_VERSION = "tk-synth-3"

COMMON = ("garden kitchen travel planning pottery journey report design school family health music weather market office repair "
          "delivery contract ferry schedule training survey launch archive server network invoice holiday recipe fitness library "
          "studio workshop trial draft policy session update roadmap backlog garage carving furniture laptop camera phone wallet "
          "ticket hotel flight museum concert bakery clinic pharmacy harbor bridge tunnel station airport village campus dormitory "
          "orchard vineyard lantern compass anchor blanket kettle hammer ladder window fence carpet mirror candle basket ribbon button").split()

ATTRS: List[List[str]] = [
    ["budget", "spending limit", "cost ceiling"], ["deadline", "due date", "cutoff"],
    ["owner", "person in charge", "lead"], ["location", "venue", "meeting place"],
    ["priority", "urgency level", "importance rating"], ["status", "current state", "progress stage"],
    ["headcount", "team size", "number of people"], ["duration", "length", "time span"],
    ["version", "release number", "build tag"], ["vendor", "supplier", "provider"],
    ["contact", "point of contact", "liaison"], ["quantity", "amount", "count"],
    ["colour", "shade", "paint code"], ["rating", "score", "grade"],
    ["route", "itinerary", "path"], ["reference code", "tracking number", "case id"],
]
NA = len(ATTRS)
CONS, VOW = "bdfgklmnprstvz", "aeiou"
N_FIRST, N_LAST = 60, 400
TOPIC_WORDS = 10

TEMPLATES = (
    "{date} {w1} {w2} {w3}: {ent} reported the {attr} as {val}.",
    "In the {w1} {w2} review on {date}, {ent} set the {attr} to {val}, noted under {w3}.",
    "{ent} says the {attr} for {w1} {w2} is {val} ({date}, {w3}).",
)


def _template_words() -> set:
    import re
    words = set()
    for t in TEMPLATES:
        words |= set(re.findall(r"[a-z]+", re.sub(r"\{[a-z0-9]+\}", " ", t.lower())))
    for forms in ATTRS:
        for f in forms:
            words |= set(f.split())
    return words


# a topic word must never also be a literal word of the templates or of an attribute phrase, or "the question's words occur only
# in its topic" would not hold at the text level
assert not (set(COMMON) & _template_words()), sorted(set(COMMON) & _template_words())


@dataclass(frozen=True)
class Knobs:
    n_leaves: int
    seed: int
    topic_size: int = 25
    topic_overlap: float = 0.4
    entity_reuse: float = 2.0
    near_dup: float = 0.15
    update_rate: float = 0.15
    session_len: float = 6.0
    paraphrase: float = 0.3
    alias_rate: float = 0.3
    one_word: float = 0.0         # probability that a question gives ONE topic word instead of two (a fixed spec, not "as few as needed")
    topic_skew: float = 0.0
    growth: str = "wide"          # "wide" or "deep"
    fixed_topics: int = 40        # used when growth == "deep"
    n_questions: int = 400
    domain: int = 0               # separates the invented vocabulary of different pools (G6); 0 for a single corpus

    def validate(self) -> None:
        if self.n_leaves < 8:
            raise ValueError("n_leaves must be at least 8")
        for name in ("topic_overlap", "near_dup", "update_rate", "paraphrase", "alias_rate", "one_word"):
            v = getattr(self, name)
            if not 0.0 <= v <= 1.0:
                raise ValueError(f"{name} must be in [0, 1]")
        if self.entity_reuse < 1.0 or self.session_len < 1.0 or self.topic_size < 2:
            raise ValueError("entity_reuse >= 1, session_len >= 1, topic_size >= 2")
        if not 0 <= self.domain < 40:
            raise ValueError("domain in [0, 40)")
        if self.growth not in ("wide", "deep"):
            raise ValueError("growth is wide or deep")


# complexity levels: linear between the two endpoints; 0.0 is easy, 1.0 is hard (calibrated on pilot seeds, see docs/tuning)
LOW = dict(topic_overlap=0.10, entity_reuse=1.0, near_dup=0.0, update_rate=0.0, paraphrase=0.0, alias_rate=0.0, one_word=0.0)
HIGH = dict(topic_overlap=0.80, entity_reuse=5.0, near_dup=0.40, update_rate=0.40, paraphrase=0.70, alias_rate=0.60, one_word=1.0)


def complexity(level: float) -> dict:
    """The complexity knob vector at a scalar level (0 = LOW endpoint, 1 = HIGH endpoint; above 1 extrapolates, capped at the
    valid range)."""
    out = {}
    for k in LOW:
        v = LOW[k] + (HIGH[k] - LOW[k]) * level
        out[k] = round(min(1.0, v) if k != "entity_reuse" else max(1.0, v), 4)
    return out


def knobs_at(n_leaves: int, seed: int, level: float, **over) -> Knobs:
    d = complexity(level)
    d.update(over)
    return Knobs(n_leaves=n_leaves, seed=seed, **d)


@dataclass
class Corpus:
    knobs: Knobs
    leaves: List[dict]
    questions: List[dict]
    topics: List[List[str]]            # vocabulary of each topic
    manifest: dict = field(default_factory=dict)


def _rng(k: Knobs, tag: str) -> random.Random:
    return random.Random(f"{GEN_VERSION}|{k.seed}|{tag}")


def _pseudo(i: int, syl: int) -> str:
    out = []
    for _ in range(syl):
        i, r = divmod(i, len(CONS) * len(VOW))
        out.append(CONS[r // len(VOW)] + VOW[r % len(VOW)])
    return "".join(out)


def _value(rng: random.Random) -> str:
    return "v" + "".join(rng.choice("0123456789abcdefghjkmnpqrstuvwxyz") for _ in range(5))


def _quotas(n: int, n_topics: int, skew: float) -> List[int]:
    w = [1.0 / (i + 1) ** skew for i in range(n_topics)]
    s = sum(w)
    q = [max(1, int(n * x / s)) for x in w]
    i = 0
    while sum(q) < n:
        q[i % n_topics] += 1
        i += 1
    while sum(q) > n:
        j = max(range(n_topics), key=lambda t: q[t])
        q[j] -= 1
    return q


def generate(k: Knobs) -> Corpus:
    k.validate()
    n = k.n_leaves
    n_topics = max(2, round(n / k.topic_size)) if k.growth == "wide" else max(2, min(k.fixed_topics, n // 2))
    quota = _quotas(n, n_topics, k.topic_skew)
    epn = max(8, math.ceil(max(quota) * 1.5 / NA))
    pool = max(epn, round(n_topics * epn / k.entity_reuse))
    if pool > N_FIRST * N_LAST:
        raise ValueError("entity pool too large for the name space")
    if k.domain and n_topics * TOPIC_WORDS >= 1000:
        raise ValueError("a pool with a domain offset holds at most 99 topics")
    r_ent, r_top, r_fact, r_sess, r_q = (_rng(k, t) for t in ("ent", "topic", "fact", "sess", "q"))

    firsts = [_pseudo(i, 2).capitalize() for i in r_ent.sample(range(70 * 70), N_FIRST)]
    lasts = [_pseudo(200_000 + i, 3).capitalize() for i in r_ent.sample(range(70 ** 3 - 200_000), N_LAST)]
    combos = r_ent.sample(range(N_FIRST * N_LAST), pool)
    names = [f"{firsts[c // N_LAST]} {lasts[c % N_LAST]}" for c in combos]

    topics: List[List[str]] = []
    for t in range(n_topics):
        vocab: List[str] = []
        for s in range(TOPIC_WORDS):
            w = r_top.choice(COMMON) if r_top.random() < k.topic_overlap else _pseudo(k.domain * 1000 + t * TOPIC_WORDS + s, 3)
            while w in vocab:
                w = r_top.choice(COMMON) if r_top.random() < k.topic_overlap else _pseudo(k.domain * 1000 + t * TOPIC_WORDS + s + 100_000 * (len(vocab) + 1), 3)
            vocab.append(w)
        topics.append(vocab)
    topic_ents = [r_ent.sample(range(pool), epn) for _ in range(n_topics)]

    # ---- facts per topic: base fact, optional older version of the same key, optional near-duplicate sibling ----
    raw: List[dict] = []
    for t in range(n_topics):
        keys = [(e, a) for e in topic_ents[t] for a in range(NA)]
        r_fact.shuffle(keys)
        left, made = quota[t], 0
        used = set()
        ki = 0
        while made < quota[t] and ki < len(keys):
            e, a = keys[ki]
            ki += 1
            if (e, a) in used:
                continue
            used.add((e, a))
            tpl = r_fact.randrange(len(TEMPLATES))
            form = r_fact.randrange(3)
            tws = r_fact.sample(topics[t], 3)
            base = dict(topic=t, ent=e, attr=a, form=form, tws=tws, tpl=tpl, val=_value(r_fact), role="base", key=(t, e, a))
            bundle = [base]
            if r_fact.random() < k.update_rate:
                old = dict(base, val=_value(r_fact), role="old", tws=r_fact.sample(topics[t], 3),
                           tpl=r_fact.randrange(len(TEMPLATES)), form=r_fact.randrange(3))
                bundle.append(old)
            if r_fact.random() < k.near_dup:
                others = [x for x in topic_ents[t] if x != e and (x, a) not in used]
                if others:
                    e2 = r_fact.choice(others)
                    used.add((e2, a))
                    bundle.append(dict(base, ent=e2, val=_value(r_fact), role="dup", key=(t, e2, a)))
            bundle = bundle[:quota[t] - made]
            raw.extend(bundle)
            made += len(bundle)
        if made < quota[t]:
            raise ValueError("topic key space exhausted; internal sizing error")

    # ---- sessions: shuffle a topic's leaves, chunk by geometric length, order sessions globally ----
    sessions: List[List[dict]] = []
    by_topic: Dict[int, List[dict]] = {}
    for r in raw:
        by_topic.setdefault(r["topic"], []).append(r)
    for t in sorted(by_topic):
        seq = by_topic[t][:]
        r_sess.shuffle(seq)
        i = 0
        while i < len(seq):
            ln = 1
            while r_sess.random() > 1.0 / k.session_len and i + ln < len(seq):
                ln += 1
            sessions.append(seq[i:i + ln])
            i += ln
    order = list(range(len(sessions)))
    r_sess.shuffle(order)
    for pos_s, si in enumerate(order):
        for p, r in enumerate(sessions[si]):
            r["sess_order"], r["pos"], r["sess_id"] = pos_s, p, si
    raw.sort(key=lambda r: (r["sess_order"], r["pos"]))

    # the later of two versions of one key is the current one; the earlier is superseded
    by_key: Dict[tuple, List[int]] = {}
    for i, r in enumerate(raw):
        by_key.setdefault(r["key"], []).append(i)
    for key, idxs in by_key.items():
        if len(idxs) == 2:
            a_, b_ = idxs  # raw is chronological: the second is the later one
            raw[a_]["kind"], raw[b_]["kind"] = "old", ("dup" if raw[b_]["role"] == "dup" else "fact")
        else:
            raw[idxs[0]]["kind"] = "dup" if raw[idxs[0]]["role"] == "dup" else "fact"

    leaves: List[dict] = []
    for i, r in enumerate(raw):
        day = r["sess_order"]
        date = "%04d-%02d-%02d" % (2020 + day // 360, (day // 30) % 12 + 1, day % 30 + 1)
        text = TEMPLATES[r["tpl"]].format(date=date, w1=r["tws"][0], w2=r["tws"][1], w3=r["tws"][2], ent=names[r["ent"]],
                                          attr=ATTRS[r["attr"]][r["form"]], val=r["val"])
        leaves.append({"id": i, "text": text, "topic": r["topic"], "entity": r["ent"], "attr": r["attr"], "key": list(r["key"]),
                       "kind": r["kind"], "session": r["sess_id"], "tws": r["tws"], "value": r["val"], "date": date,
                       "form": r["form"], "ent_name": names[r["ent"]]})

    questions, dropped = _questions(k, leaves, topics, names, r_q)
    corpus = Corpus(k, leaves, questions, topics)
    corpus.manifest = _manifest(corpus, n_topics, epn, pool, len(sessions), dropped)
    return corpus


def _questions(k: Knobs, leaves: List[dict], topics: List[List[str]], names: List[str], rng: random.Random):
    by_attr: Dict[int, List[dict]] = {}
    for L in leaves:
        if L["kind"] != "old":
            by_attr.setdefault(L["attr"], []).append(L)
    old_of: Dict[tuple, List[int]] = {}
    dup_of: Dict[tuple, List[int]] = {}
    by_sig: Dict[tuple, List[int]] = {}
    for L in leaves:
        if L["kind"] == "old":
            old_of.setdefault(tuple(L["key"]), []).append(L["id"])
        by_sig.setdefault((L["topic"], L["attr"]), []).append(L["id"])
    vocab_sets = [set(v) for v in topics]

    def unique(L: dict, alias: str, words: Sequence[str]) -> bool:
        ws = set(words)
        hits = 0
        for x in by_attr[L["attr"]]:
            if _alias_match(alias, names[x["entity"]]) and ws <= vocab_sets[x["topic"]]:
                hits += 1
                if hits > 1:
                    return False
        return hits == 1

    cands = [L for L in leaves if L["kind"] != "old"]
    rng.shuffle(cands)
    out, dropped = [], 0
    for L in cands:
        if len(out) >= k.n_questions:
            break
        full = names[L["entity"]]
        alias_used = False
        alias = full
        words = None
        k_words = 1 if rng.random() < k.one_word else 2
        if rng.random() < k.alias_rate:
            part = rng.choice(full.split())
            for w in _combos(L["tws"], k_words, rng):
                if unique(L, part, w):
                    alias, words, alias_used = part, w, True
                    break
        if words is None:
            for w in _combos(L["tws"], k_words, rng):
                if unique(L, full, w):
                    words = w
                    break
        if words is None:
            dropped += 1
            continue
        forms = ATTRS[L["attr"]]
        para = rng.random() < k.paraphrase
        form_q = rng.choice([f for i, f in enumerate(forms) if i != L["form"]]) if para else forms[L["form"]]
        text = f"What is the {form_q} for {alias}" + (f" regarding {' '.join(words)}?" if words else "?")
        key = tuple(L["key"])
        sibs = [i for i in by_sig[(L["topic"], L["attr"])] if i != L["id"] and leaves[i]["kind"] != "old"
                and leaves[i]["tws"] == L["tws"] and leaves[i]["entity"] != L["entity"]]
        out.append({"qid": len(out), "text": text, "gold": [L["id"]], "gold_value": L["value"], "topic": L["topic"],
                    "session": L["session"], "stale": old_of.get(key, []), "dups": sibs[:5],
                    "kind": "updated" if key in old_of else "plain", "alias": alias_used, "para": para})
    return out, dropped


def _combos(tws: Sequence[str], size: int, rng: random.Random):
    from itertools import combinations
    cs = [tuple(c) for c in combinations(tws, size)]
    rng.shuffle(cs)
    return cs


def _alias_match(alias: str, name: str) -> bool:
    return alias == name if " " in alias else alias in name.split()


def complexity_stats(c: Corpus, *, pairs: int = 3000) -> dict:
    """Corpus statistics used by the independence tests (and reported in the manifest)."""
    n = len(c.leaves)
    nt = len(c.topics)
    rng = random.Random(f"{GEN_VERSION}|{c.knobs.seed}|stats")
    sets = [set(v) for v in c.topics]
    js = []
    allp = [(i, j) for i in range(nt) for j in range(i + 1, nt)]
    chosen = allp if len(allp) <= pairs else rng.sample(allp, pairs)
    for i, j in chosen:
        js.append(len(sets[i] & sets[j]) / len(sets[i] | sets[j]))
    ent_topics: Dict[int, set] = {}
    for L in c.leaves:
        ent_topics.setdefault(L["entity"], set()).add(L["topic"])
    sess_len: Dict[int, int] = {}
    for L in c.leaves:
        sess_len[L["session"]] = sess_len.get(L["session"], 0) + 1
    qs = c.questions
    return {
        "n_leaves": n,
        "n_topics": nt,
        "topic_jaccard": sum(js) / len(js) if js else 0.0,
        "entity_reuse": sum(len(v) for v in ent_topics.values()) / len(ent_topics),
        "dup_frac": sum(1 for L in c.leaves if L["kind"] == "dup") / n,
        "old_frac": sum(1 for L in c.leaves if L["kind"] == "old") / n,
        "mean_session_len": sum(sess_len.values()) / len(sess_len),
        "mean_words": sum(len(L["text"].split()) for L in c.leaves) / n,
        "leaves_per_topic": n / nt,
        "q_alias_frac": sum(q["alias"] for q in qs) / len(qs) if qs else 0.0,
        "q_mean_topic_words": sum(len(q["text"].split(" regarding ")[1].rstrip("?").split()) if " regarding " in q["text"] else 0
                                  for q in qs) / len(qs) if qs else 0.0,
        "q_para_frac": sum(q["para"] for q in qs) / len(qs) if qs else 0.0,
        "q_updated_frac": sum(q["kind"] == "updated" for q in qs) / len(qs) if qs else 0.0,
    }


def _manifest(c: Corpus, n_topics: int, epn: int, pool: int, n_sessions: int, dropped: int) -> dict:
    h = hashlib.sha256()
    for L in c.leaves:
        h.update(L["text"].encode() + b"\n")
    for q in c.questions:
        h.update(json.dumps([q["text"], q["gold"]]).encode() + b"\n")
    return {"generator": GEN_VERSION, "knobs": asdict(c.knobs), "n_topics": n_topics, "entities_per_topic": epn,
            "entity_pool": pool, "n_sessions": n_sessions, "n_questions": len(c.questions), "questions_dropped_ambiguous": dropped,
            "question_drop_rate": round(dropped / max(1, dropped + len(c.questions)), 4),
            "content_sha256": h.hexdigest(), "stats": complexity_stats(c),
            "text_caveat": "template text over a controlled vocabulary; not natural language"}


def dumps_manifest(c: Corpus) -> str:
    return json.dumps(c.manifest, sort_keys=True, indent=1)
