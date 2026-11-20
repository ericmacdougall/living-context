"""Interface for an external (downloaded) retrieval set, for Z6. Nothing is downloaded by this kit: this converts files that the
owner has approved and placed on the host into the harness's cell format, so the same baseline and signal code runs on them.

Layout (the BEIR layout; each of its facts is "to verify" for a given set, see docs/tuning/DATASETS.md):
  corpus.jsonl    {"_id": str, "title": str, "text": str}
  queries.jsonl   {"_id": str, "text": str}
  qrels.tsv       query-id <TAB> corpus-id <TAB> score   (a header line is skipped; score > 0 is relevant)

A leaf is the title plus the text; every leaf is its own "session" (such sets have no sessions), so `spread` is the number of
distinct documents and carries no session information there. Queries without a relevant document in the corpus are dropped and
counted."""
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Optional

from .synth import Corpus, Knobs


def load_beir_like(root, *, max_docs: Optional[int] = None, max_queries: Optional[int] = None, name: str = "external") -> Corpus:
    root = Path(root)
    leaves, index = [], {}
    with open(root / "corpus.jsonl") as f:
        for line in f:
            d = json.loads(line)
            if max_docs is not None and len(leaves) >= max_docs:
                break
            text = (d.get("title", "") + ". " + d["text"]).strip(". ") if d.get("title") else d["text"]
            index[d["_id"]] = len(leaves)
            leaves.append({"id": len(leaves), "text": text, "topic": 0, "entity": -1, "attr": -1, "key": [d["_id"]], "kind": "fact",
                           "session": len(leaves), "tws": [], "value": "", "date": "", "form": 0})
    rel = {}
    with open(root / "qrels.tsv") as f:
        for row in csv.reader(f, delimiter="\t"):
            if len(row) < 3 or not row[2].lstrip("-").isdigit() or int(row[2]) <= 0:
                continue
            if row[1] in index:
                rel.setdefault(row[0], []).append(index[row[1]])
    questions, dropped = [], 0
    with open(root / "queries.jsonl") as f:
        for line in f:
            d = json.loads(line)
            gold = rel.get(d["_id"])
            if not gold:
                dropped += 1
                continue
            questions.append({"qid": len(questions), "text": d["text"], "gold": sorted(set(gold)), "gold_value": "", "topic": 0,
                              "session": gold[0], "stale": [], "dups": [], "kind": "plain", "alias": False, "para": False,
                              "ext_id": d["_id"]})
            if max_queries is not None and len(questions) >= max_queries:
                break
    k = Knobs(n_leaves=max(8, len(leaves)), seed=0)
    c = Corpus(k, leaves, questions, [])
    c.manifest = {"generator": "external:" + name, "n_questions": len(questions), "questions_dropped_no_gold": dropped,
                  "knobs": {"n_leaves": len(leaves), "seed": 0}, "questions_dropped_ambiguous": dropped, "content_sha256": "",
                  "stats": {"n_leaves": len(leaves)}}
    return c
