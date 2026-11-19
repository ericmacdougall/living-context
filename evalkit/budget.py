"""Derived call and token budget for living-context EXPERIMENT.md section 10 (estimates, not measurements).

low = small figure in every factor; high = large figure in every factor. Every arm is its own tenant and nothing
is shared between arms (the evaluation cache of the earlier draft is withdrawn), so k agent-routed arms cost k
times one arm in both figures. Inside one arm, gate replays re-use the product cache of the class's service
principal (DESIGN 5.8), which is already in the gate's per-cycle routing figures.

Plain Python 3, standard library only, deterministic. Run: python3 -I budget.py"""
TOK = {  # API input tokens per call (low, high); CLI basis adds 10,500 fixed overhead per call (LAB measured)
 "route": (2500, 4000), "organise": (2000, 3000), "build_l1": (18000, 37000), "build_upper": (4000, 8000),
 "type_edges": (5000, 7000), "judge": (3000, 7000), "gen": (1000, 2500), "reader": (4000, 10000),
 "answer_judge": (1500, 3000), "mixed": (5000, 15000)}
CLI = 10500; RATE = 3600 / 9.36 * 2  # calls per hour at 9.36 s per call, concurrency 2
def recall(n, per, arms):  # routing calls: 80% route, 20% organise
    lo = n * per[0] * arms; hi = n * per[1] * arms
    return {"route": (0.8 * lo, 0.8 * hi), "organise": (0.2 * lo, 0.2 * hi)}
def build(lo, hi, arms=1):
    lo2 = lo * arms; hi2 = hi * arms
    return {"build_l1": (0.93 * lo2, 0.93 * hi2), "build_upper": (0.07 * lo2, 0.07 * hi2)}
def add(acc, part, label):
    for k, (a, b) in part.items():
        x = acc.setdefault(k, [0, 0]); x[0] += a; x[1] += b
    rows.append((label, sum(v[0] for v in part.values()), sum(v[1] for v in part.values())))
DEV, WHOLE, H4P = (25, 60), (50, 77), (35, 70)
stages = {}
def stage(name, parts):
    global rows; rows = []; acc = {}
    for label, part in parts: add(acc, part, label)
    stages[name] = (acc, rows)
stage("S1", [("canary, every task kind", {"mixed": (20, 25)})])
stage("S2", [("build", build(381, 574)), ("edge typing", {"type_edges": (330, 1100)}),
  ("A1 recalls 30 q", recall(30, DEV, 1)), ("SHUFFLED-SUMMARIES 30 q", recall(30, DEV, 1)),
  ("relevance judge on 3 pools", {"judge": (180, 180)})])
stage("S3", [("build", build(1256, 1932)), ("edge typing", {"type_edges": (1086, 3620)}),
  ("A1 recalls 102 q", recall(102, DEV, 1)), ("SHUFFLED-SUMMARIES 50 q", recall(50, DEV, 1)),
  ("judge validation, leaf and session level, 3 paraphrases", {"judge": (480, 480)}),
  ("AGENTIC: agent calls 50 q", {"reader": (50, 150)}), ("AGENTIC: explore holder calls", {"route": (300, 1800)}),
  ("AGENT-A0: agent calls 50 q", {"reader": (50, 150)})])
stage("S4a", [("stream recalls, 3 agent-routed arms x 222", recall(222, DEV, 3)),
  ("reader per item, LOOP and FROZEN", {"reader": (444, 1332)}), ("reflection, LOOP", {"gen": (222, 222), "judge": (1110, 1776)}),
  ("QUERY-MEMO shadow reflection on FROZEN's searches", {"gen": (222, 222), "judge": (1110, 1776)}),
  ("reverse audit, 9 cycles x 100 leaves", {"judge": (450, 900)}), ("re-summaries, LOOP and RANDOM-FEEDBACK", {"build_upper": (300, 1200)}),
  ("gate, 2 arms x 9 cycles", {"gen": (216, 216), "judge": (540, 1080), "route": (2844, 11304)}),
  ("incremental build", build(1966, 3276, 3)), ("edge typing", {"type_edges": (1417, 4725)}),
  ("recurring-query generation", {"gen": (120, 360)}), ("proxy supporting arms, judge", {"judge": (500, 1000)}),
  ("behavioural-use sample", {"reader": (200, 200)})])
stage("S4b", [("base build (43 non-target dev blocks)", build(480, 740)), ("base edge typing", {"type_edges": (443, 1475)}),
  ("stream recalls, 4 agent-routed arms x 174", recall(174, DEV, 4)),
  ("reader per item, LOOP FROZEN NO-AUDIT", {"reader": (360, 1080)}),
  ("reflection, LOOP and NO-AUDIT", {"gen": (240, 240), "judge": (720, 1320)}),
  ("reverse audit, 6 cycles x 100 leaves", {"judge": (300, 600)}), ("re-summaries, 3 learning arms", {"build_upper": (300, 1200)}),
  ("gate, 3 arms x 6 cycles", {"gen": (216, 216), "judge": (540, 1080), "route": (2844, 11304)}),
  ("gate positive controls (oracle and random candidates)", {"gen": (24, 24), "judge": (60, 120), "route": (316, 1256)}),
  ("stream build", build(1090, 1680, 4)), ("stream edge typing", {"type_edges": (938, 3125)}),
  ("probe generation with rank filter", {"gen": (94, 282)}), ("LOOP+SYNTH variant (proxy)", {"gen": (40, 120), "judge": (160, 480)}),
  ("proxy supporting arms, judge", {"judge": (500, 1000)}), ("behavioural-use sample", {"reader": (200, 200)})])
stage("S5", [("J1 reader and answer judge (optional)", {"reader": (590, 590), "answer_judge": (590, 590)})])
stage("S6-H1", [("whole-pool build", build(5004, 7700)),
  ("A1 recalls 312 q (A0, A0+LEN: no model call)", recall(312, WHOLE, 1)), ("SHUFFLED-SUMMARIES 50 q", recall(50, WHOLE, 1))])
stage("S6-H2", [("edge typing (A2 and RANDOM-EDGES re-use A1's routing)", {"type_edges": (4329, 14432)}),
  ("AGENTIC: agent calls 100 q", {"reader": (100, 300)}), ("AGENTIC: explore holder calls", {"route": (600, 3600)}),
  ("AGENT-A0: agent calls 100 q", {"reader": (100, 300)})])
stage("S7a", [("stream recalls, 3 agent-routed arms x 522", recall(522, WHOLE, 3)),
  ("reader per item, LOOP and FROZEN", {"reader": (1044, 3132)}), ("reflection, LOOP", {"gen": (522, 522), "judge": (2610, 4176)}),
  ("QUERY-MEMO shadow reflection on FROZEN's searches", {"gen": (522, 522), "judge": (2610, 4176)}),
  ("reverse audit, 10 cycles x 100 leaves", {"judge": (500, 1000)}), ("re-summaries, LOOP and RANDOM-FEEDBACK", {"build_upper": (1200, 4800)}),
  ("gate, 2 arms x 10 cycles", {"gen": (240, 240), "judge": (600, 1200), "route": (3160, 12560)}),
  ("incremental build", build(4000, 6700, 3)), ("edge typing", {"type_edges": (2900, 9700)}),
  ("recurring-query generation", {"gen": (299, 897)}), ("proxy supporting arms, judge", {"judge": (1000, 2000)}),
  ("behavioural-use sample", {"reader": (200, 200)})])
stage("S7b", [("stream recalls, 4 agent-routed arms x 280", recall(280, H4P, 4)),
  ("reader per item, LOOP FROZEN NO-AUDIT", {"reader": (600, 1800)}),
  ("reflection, LOOP and NO-AUDIT", {"gen": (400, 400), "judge": (1200, 2200)}),
  ("reverse audit, 10 cycles x 100 leaves", {"judge": (500, 1000)}), ("re-summaries, 3 learning arms", {"build_upper": (600, 2400)}),
  ("gate, 3 arms x 10 cycles", {"gen": (360, 360), "judge": (900, 1800), "route": (4740, 18840)}),
  ("stream build", build(1650, 2550, 4)), ("stream edge typing", {"type_edges": (1174, 3913)}),
  ("probe generation with rank filter", {"gen": (140, 420)}), ("LOOP+SYNTH variant (proxy)", {"gen": (40, 120), "judge": (160, 480)}),
  ("proxy supporting arms, judge", {"judge": (500, 1000)}), ("behavioural-use sample", {"reader": (200, 200)})])
stage("DEVBASE", [("DEV-BASE build when S4a has not run", build(1966, 3276)), ("DEV-BASE edge typing", {"type_edges": (1417, 4725)})])
def summ(name):
    acc, rows = stages[name]
    calls = [sum(v[0] for v in acc.values()), sum(v[1] for v in acc.values())]
    api = [sum(acc[k][0] * TOK[k][0] for k in acc), sum(acc[k][1] * TOK[k][1] for k in acc)]
    cli = [api[0] + CLI * calls[0], api[1] + CLI * calls[1]]
    return calls, api, cli
tot = {"calls": [0, 0], "api": [0, 0], "cli": [0, 0]}
fmt = lambda x: "{:,}".format(int(round(x)))
ORDER = ["S1", "S2", "S3", "S6-H1", "S6-H2", "S4b", "S4a", "S5", "S7a", "S7b", "DEVBASE"]
for name in ORDER:
    c, a, l = summ(name)
    print("%-8s calls %s to %s | API Mtok %.0f to %.0f | CLI Mtok %.0f to %.0f | hours %.0f to %.0f" % (
        name, fmt(c[0]), fmt(c[1]), a[0] / 1e6, a[1] / 1e6, l[0] / 1e6, l[1] / 1e6, c[0] / RATE, c[1] / RATE))
    for label, lo, hi in stages[name][1]: print("    %-55s %s to %s" % (label, fmt(lo), fmt(hi)))
    if name != "DEVBASE":
        for k, v in (("calls", c), ("api", a), ("cli", l)): tot[k][0] += v[0]; tot[k][1] += v[1]
print("TOTAL full plan (S1 to S7b, no DEVBASE): calls %s to %s; API Mtok %.0f to %.0f; CLI Mtok %.0f to %.0f; hours %.0f to %.0f" % (
    fmt(tot["calls"][0]), fmt(tot["calls"][1]), tot["api"][0] / 1e6, tot["api"][1] / 1e6, tot["cli"][0] / 1e6, tot["cli"][1] / 1e6,
    tot["calls"][0] / RATE, tot["calls"][1] / RATE))
def path(names):
    c = [0, 0]; a = [0, 0]; l = [0, 0]
    for n in names:
        cc, aa, ll = summ(n)
        for acc, v in ((c, cc), (a, aa), (l, ll)): acc[0] += v[0]; acc[1] += v[1]
    return "%s to %s calls, API Mtok %.0f to %.0f, CLI Mtok %.0f to %.0f, hours %.0f to %.0f" % (
        fmt(c[0]), fmt(c[1]), a[0] / 1e6, a[1] / 1e6, l[0] / 1e6, l[1] / 1e6, c[0] / RATE, c[1] / RATE)
print("S6 whole (S6-H1 + S6-H2):", path(["S6-H1", "S6-H2"]))
print("H1-first path (S1, S2, S3, S6-H1):", path(["S1", "S2", "S3", "S6-H1"]))
print("then H2, H1b, C (S6-H2):", path(["S6-H2"]))
print("then H4 (S4b, DEVBASE, S7b):", path(["S4b", "DEVBASE", "S7b"]))
print("then H3 (S4a, S7a):", path(["S4a", "S7a"]))
print("per kind API tokens/call:", TOK)
