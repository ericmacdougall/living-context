# Mutation check for the H1 evaluation kit: each mutant applies one broken variant to a copy of the checked-out tree
# (expects ~/living-context-h1/code and its venv) and the named test module must go red. Prints one MUTANT line each.
set -u
cd $HOME/living-context-h1
rm -rf mut && mkdir mut
run() {  # name, file, python-snippet performing the edit on the file text s, test module
  rm -rf mut/code && cp -r code mut/code && cd mut/code
  ../../venv/bin/python - "$2" <<EOF
import sys
p = sys.argv[1]
s = open(p).read()
t = s
$3
assert t != s, "mutation did not apply"
open(p, "w").write(t)
EOF
  res=$(../../venv/bin/python -m unittest $4 2>&1 | tail -1)
  echo "MUTANT $1 -> $res"
  cd ../..
}
run harm_after_did_not_hold evalkit/stats.py 't = s.replace("    if harm_defined and hi < 0:\n        return HARM\n", "").replace("    return DID_NOT_HOLD if hi < m else NOT_ESTABLISHED", "    if harm_defined and hi < 0 and lo > 0:\n        return HARM\n    return DID_NOT_HOLD if hi < m else NOT_ESTABLISHED")' tests.unit.test_eval_arms
run bootstrap_per_question evalkit/stats.py 't = s.replace("    sums, cnt = _cluster_sums(diffs, clusters)", "    sums, cnt = _cluster_sums(diffs, {})")' tests.unit.test_eval_arms
run leaf_level_recall evalkit/arms.py 't = s.replace("        elif s not in out:\n            out.append(s)", "        else:\n            out.append(s)")' tests.unit.test_eval_arms
run failed_call_not_zero evalkit/score.py 't = s.replace("            scores[qid] = 0.0\n            continue", "            continue")' tests.unit.test_eval_arms
run abstention_included evalkit/score.py 't = s.replace("if qid in abstention and not broken_include_abstention:", "if False:")' tests.unit.test_eval_arms
run wilson_on_counts evalkit/stats.py 't = s.replace("    if measure.kind != \"binary\":", "    if False:")' tests.unit.test_eval_arms
run identity_permutation_default evalkit/arms.py 't = s.replace("shift if shift is not None else max(1, n // 2)", "0")' tests.unit.test_eval_arms
run a1_fusion_ignores_tree evalkit/arms.py 't = s.replace("[list(tree_ids[:WINDOW]), list(direct_ids[:WINDOW])]", "[list(direct_ids[:WINDOW])]")' tests.unit.test_eval_arms
run charge_after_call evalkit/claude_cli.py 't = s.replace("            n = self._budget.charge()         # BEFORE the process; raises BudgetExceeded at the cap, nothing is spawned\n            self.spawned += 1\n            ro = self._run(prompt)", "            ro = self._run(prompt)\n            self.spawned += 1\n            n = self._budget.charge()")' tests.unit.test_claude_cli
run no_cap_refusal evalkit/claude_cli.py 't = s.replace("            n = self._budget.charge()", "            n = self._budget.count() + 1")' tests.unit.test_claude_cli
run prompt_leak_check_off evalkit/claude_cli.py 't = s.replace("        self._check_prompt(prompt)            # before the counter: a refused prompt costs nothing", "        pass")' tests.unit.test_claude_cli
run argv_has_bare evalkit/claude_cli.py 't = s.replace("\"--no-session-persistence\", \"--tools\"", "\"--bare\", \"--tools\"")' tests.unit.test_claude_cli
run split_no_dev2 evalkit/dataset.py 't = s.replace("    for qid in cand[:DEV2_SIZE]:\n        sp[qid] = \"dev2\"", "    pass")' tests.unit.test_eval_dataset
run test_split_released evalkit/dataset.py 't = s.replace("DEVELOPMENT_SPLITS = (\"pilot\", \"dev2\", \"held_back\")", "DEVELOPMENT_SPLITS = (\"pilot\", \"dev2\", \"held_back\", \"test\")")' tests.unit.test_eval_dataset
run s2_guard_embed_removed evalkit/run_h1.py 't = s.replace("        if embed is embed_stub.embed:", "        if False:")' tests.unit.test_run_h1_guards
