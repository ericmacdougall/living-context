"""The S1-measured budget for the H1-first path (EXPERIMENT 14.2; 10.2, 10.3). Plain Python, run with `python3 -I`.

It executes `budget.py` UNCHANGED (its default output is the registered 10.3 table and is not altered) with one substitution, the
fixed per-call CLI overhead, and then derives two things from the same per-kind call counts:

  * CLI input tokens with the MEASURED overhead in place of the 10,500 assumption, on the registered per-kind API token ranges
    (those ranges are estimates for real-data sizes and S1 did not measure real-data sizes);
  * a fixture-sized LOWER BOUND: calls x the per-kind total input tokens measured in S1 on synthetic notes;

and wall hours from the measured mean latency per kind at the registered concurrency of 2 (a floor: real prompts are larger than
the fixture's, so real calls are slower for the kinds whose input scales with text).

Calls per stage do not change: S1 measures tokens and time, not how many calls a stage makes."""
import contextlib
import io
import os

CLI_OVERHEAD = 645   # tokens: query_variants (158-char prompt) 682 and answer_judge (178 chars) 689 total input, minus their prompts
# measured in S1, one entry per budget.py kind: (mean total input tokens per call, mean output tokens per call, mean latency s)
MEASURED = {
    "route": (2865, 98, 2.54), "organise": (1255, 404, 3.80), "build_l1": (2415, 1724, 10.21), "build_upper": (2090, 176, 3.00),
    "type_edges": (1842, 115, 2.54), "judge": (1558, 18, 2.03), "gen": (1104, 104, 2.67), "reader": (1550, 93, 2.65),
    "answer_judge": (689, 9, 1.88), "mixed": (1500, 200, 3.0)}
CONCURRENCY = 2

src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "budget.py")).read()
assert "CLI = 10500;" in src
ns = {"__name__": "budget_registered"}
with contextlib.redirect_stdout(io.StringIO()):
    exec(compile(src.replace("CLI = 10500;", "CLI = %d;" % CLI_OVERHEAD), "budget.py", "exec"), ns)
stages, TOK = ns["stages"], ns["TOK"]
fmt = lambda x: "{:,}".format(int(round(x)))


def stage_figures(name):
    acc = stages[name][0]
    calls = [sum(v[0] for v in acc.values()), sum(v[1] for v in acc.values())]
    api = [sum(acc[k][0] * TOK[k][0] for k in acc), sum(acc[k][1] * TOK[k][1] for k in acc)]
    cli = [api[0] + CLI_OVERHEAD * calls[0], api[1] + CLI_OVERHEAD * calls[1]]
    fix_in = [sum(acc[k][i] * MEASURED[k][0] for k in acc) for i in (0, 1)]
    out = [sum(acc[k][i] * MEASURED[k][1] for k in acc) for i in (0, 1)]
    hours = [sum(acc[k][i] * MEASURED[k][2] for k in acc) / 3600 / CONCURRENCY for i in (0, 1)]
    return calls, api, cli, fix_in, out, hours


print("S1-measured budget (fixed CLI overhead %d tokens measured, was 10,500; latencies measured per kind at concurrency %d)" % (CLI_OVERHEAD, CONCURRENCY))
path = ["S1", "S2", "S3", "S6-H1"]
tot = [[0, 0] for _ in range(6)]
for name in path:
    figs = stage_figures(name)
    print("%-6s calls %s to %s | CLI input Mtok (registered API ranges + measured overhead) %.0f to %.0f | fixture-sized lower bound %.0f to %.0f | "
          "output Mtok %.1f to %.1f | hours (measured latency floor) %.1f to %.1f" % (
              name, fmt(figs[0][0]), fmt(figs[0][1]), figs[2][0] / 1e6, figs[2][1] / 1e6, figs[3][0] / 1e6, figs[3][1] / 1e6,
              figs[4][0] / 1e6, figs[4][1] / 1e6, figs[5][0], figs[5][1]))
    if name != "S1":
        for t, f in zip(tot, figs):
            t[0] += f[0]; t[1] += f[1]
print("S2 + S3 + S6-H1 (the stages still to approve): calls %s to %s | CLI input Mtok %.0f to %.0f | fixture-sized lower bound %.0f to %.0f | "
      "output Mtok %.1f to %.1f | hours %.1f to %.1f" % (
          fmt(tot[0][0]), fmt(tot[0][1]), tot[2][0] / 1e6, tot[2][1] / 1e6, tot[3][0] / 1e6, tot[3][1] / 1e6, tot[4][0] / 1e6, tot[4][1] / 1e6,
          tot[5][0], tot[5][1]))
