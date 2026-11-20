"""G7, conversations with planted events (TUNING 2.1; POOLS 6.2): a scripted conversation over a generated corpus, with the turn of
every planted topic shift, return to an earlier topic, new entity and failed lookup recorded, and the memory that should be offered.
Template turns, no model call. A planted shift is easier to separate than a real one (TUNING 2.2), so a trigger score on these
turns measures the generator as much as the trigger."""
from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Dict, List

from . import synth

OPEN = ("Let us go over", "I want to talk about", "Back to", "Quick question on", "Remind me where we are on")
CLOSE = ("and what is new there", "again, please", "before the next meeting", "so I can plan")
CHAT = ("Okay thanks, that helps.", "Right, go on.", "Hmm, I see.", "Fine, noted.")


@dataclass
class Conversation:
    conv_id: int
    turns: List[dict]          # {"i", "text", "topic", "events", "offer": [leaf ids]}
    events: List[dict]         # {"i", "kind"}


def generate(corpus: synth.Corpus, seed: int, *, n_convs: int = 50, turns: int = 30, p_shift: float = 0.10, p_return: float = 0.05,
             p_new_entity: float = 0.08, p_chat: float = 0.30, p_failed: float = 0.03) -> List[Conversation]:
    rng = random.Random(f"{synth.GEN_VERSION}|conv|{seed}")
    names: Dict[int, str] = {L["entity"]: L["ent_name"] for L in corpus.leaves}
    cur = [L for L in corpus.leaves if L["kind"] != "old"]
    by_topic: Dict[int, List[dict]] = {}
    for L in cur:
        by_topic.setdefault(L["topic"], []).append(L)
    topics = sorted(by_topic)
    superseded = [L for L in corpus.leaves if L["kind"] == "old"]
    convs = []
    for ci in range(n_convs):
        topic = rng.choice(topics)
        seen_topics, seen_entities, out, events = [topic], set(), [], []
        for i in range(turns):
            ev, offer = [], []
            u = rng.random()
            if i > 0 and u < p_shift:
                new = rng.choice([t for t in topics if t not in seen_topics[-3:]])
                topic, ev = new, ["shift"]
                seen_topics.append(new)
            elif i > 0 and u < p_shift + p_return and len(set(seen_topics)) > 1:
                topic, ev = rng.choice([t for t in set(seen_topics) if t != topic]), ["return"]
                seen_topics.append(topic)
            elif i > 0 and u < p_shift + p_return + p_new_entity:
                ev = ["new_entity"]
            if u >= p_shift + p_return + p_new_entity and rng.random() < p_chat:
                out.append({"i": i, "text": rng.choice(CHAT), "topic": topic, "events": [], "offer": []})
                continue
            pool = by_topic[topic]
            leaf = rng.choice(pool)
            if "new_entity" in ev:
                fresh = [x for x in pool if x["entity"] not in seen_entities]
                if not fresh:
                    ev = []
                else:
                    leaf = rng.choice(fresh)
            ws = rng.sample(leaf["tws"], 2) + [rng.choice(leaf["tws"])]
            text = f"{rng.choice(OPEN)} {ws[0]} {ws[1]} and {ws[2]} with {names[leaf['entity']]}, {rng.choice(CLOSE)}."
            if ev:
                offer = [x["id"] for x in pool if x["entity"] == leaf["entity"]]
            seen_entities.add(leaf["entity"])
            out.append({"i": i, "text": text, "topic": topic, "events": ev, "offer": offer})
            events += [{"i": i, "kind": e} for e in ev]
        if superseded and rng.random() < p_failed * turns:
            j = rng.randrange(1, turns)
            old = rng.choice(superseded)
            succ = [L["id"] for L in cur if L["key"] == old["key"]]
            if succ:
                out[j]["events"] = out[j]["events"] + ["failed_lookup"]
                out[j]["offer"] = succ
                events.append({"i": j, "kind": "failed_lookup"})
        convs.append(Conversation(ci, out, events))
    return convs
