"""The S1 plan is fixed in code: with a fake runner the probe and the rest make exactly the planned number of calls, stay under
the cap of 25, and cover every task kind. No process is spawned."""
import json
import re
import tempfile
import unittest
from pathlib import Path

from evalkit import canary
from evalkit import claude_cli as cc
from tests.unit.test_claude_cli import Fake, envelope


def canned(prompt):
    task = re.match(r"TASK (\w+)", prompt).group(1)
    if task == "route":
        labels = re.findall(r'"label":"(c\d+)"', prompt)
        l = labels[0] if labels else "c1"
        return envelope({"selected": [l], "reasons": {l: "looks related"}, "confidence": {l: 3}})
    if task == "build_l1":
        n = len(json.loads(re.search(r"Leaves \(JSON\): (.*)\n", prompt).group(1)))
        return envelope({"entries": [{"label": "c%d" % i, "gist": "g", "facts": ["f"], "entities": ["e"]} for i in range(1, n + 1)],
                         "summary": "a summary"})
    if task == "build_upper":
        return envelope({"summary": "an upper summary"})
    if task == "organise":
        ps = json.loads(re.search(r"Pointers \(JSON\): (.*)\n", prompt).group(1))
        return envelope({"outline": [{"label": "group", "pointers": [p["pointer"] for p in ps]}]})
    return {"type_edges": envelope({"edges": []}), "judge_relevance": envelope({"score": 0.5, "relevant": True}),
            "query_variants": envelope({"variants": ["a", "b", "c"]}), "synthetic_queries": envelope({"queries": ["a", "b", "c"]}),
            "ask": envelope({"answer": "x"}), "reader_answer": envelope({"answer": "x"}),
            "answer_judge": envelope({"correct": True})}[task]


class Plan(unittest.TestCase):
    def test_probe_plus_rest_is_22_calls_under_the_cap_and_covers_every_task_kind(self):
        with tempfile.TemporaryDirectory() as d:
            run = Path(d)
            port = cc.CappedPort(Fake(canned), canary.Budget(run / "counter", canary.CAP), run / "calls.jsonl")
            canary.probe(run, port)
            self.assertEqual(port.spawned, 1)
            canary.rest(run, port)
            self.assertEqual(port.spawned, 22)
            self.assertLessEqual(port.spawned, canary.CAP)
            kinds = {r["task"] for r in port.rows}
            self.assertEqual(kinds, {"build_l1", "build_upper", "route", "organise", "type_edges", "judge_relevance",
                                     "query_variants", "synthetic_queries", "ask", "reader_answer", "answer_judge"})
            by = {k: sum(r["task"] == k for r in port.rows) for k in kinds}
            self.assertEqual((by["build_l1"], by["route"], by["organise"]), (4, 8, 2))
            self.assertTrue(all(r["schema_ok"] and r["strict_ok"] for r in port.rows), [r for r in port.rows if not r["strict_ok"]][:1])
            rep = canary.report(run, "c", "m")
            self.assertEqual((rep["calls_total"], rep["counter"]), (22, 22))


if __name__ == "__main__":
    unittest.main()
