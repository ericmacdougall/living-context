# Contributing to living-context

Thanks for helping. This project is experimental and its method is **not
proven**, so the rules below are mostly about keeping the evidence honest.

Code currently lives on two branch lines and reaches `main` by pull request:
`fix/pools-rescue` (product hardening) and `feat/parallel-calls` (evaluation).
Until the integration pull request lands, branch from the line your change
belongs to. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Development setup

- Python 3 (recent; the code uses built-in generic type syntax, so 3.9 or
  newer).
- Create a virtual environment and install from the lockfile, not the `.in`
  file: `pip install -r requirements.lock`. The evaluation line also has
  `requirements-embed.lock` for the real embedding pool. Pin new dependencies
  with a lockfile entry.
- Copy `config.example.toml` **outside the repository**, fill in your own
  values, and point `LC_CONFIG` at it. The repository only ever holds
  placeholders.
- Tests that need Elasticsearch read `LC_TEST_ES_URL`, `LC_TEST_CA` and
  `LC_TEST_PW` from the environment. The pinned image is Elasticsearch 9.5.6;
  `tools/setup_es.py` prepares a cluster.

## Tests

Run the focused tests for the area you changed:

```
python -m unittest tests.unit.test_fusion
python -m unittest discover -s tests/unit
```

- A security test that needs a cluster **fails** when none is configured; it
  never skips. `python tools/run_security.py [DIR ...]` runs the security
  suite and exits non-zero on any failure, error, skip or empty run.
- A test of changed behaviour must fail against the previous implementation (or
  a deliberately broken variant) and pass against yours. A test that cannot fail
  for the stated reason does not count.
- Prefer focused suites over running everything. Long runs and anything that
  spends money on model calls do not belong on a laptop and need agreement in
  the issue or pull request first.
- Unit tests use the deterministic stub model and embedder. Nothing in the test
  suites should make a paid API call.

## The pre-registration rule

The experiment is pre-registered in `docs/EXPERIMENT.md`. The rules:

- **Results never move a threshold.** A threshold, minimum effect, merge rule or
  hypothesis outcome is not edited after seeing data it governs.
- **Amendments are committed alone, before the run they affect**, with the
  reason in the message. Do not bundle an amendment with code or other
  documentation.
- Development choices (prompts, thresholds, split rules) are frozen in a commit
  before the confirmatory run.
- Pilot and exploratory figures are labelled as such wherever they appear.
  Report negative and null results in the same place as positive ones.
- Use the outcome words defined there (HOLDS, HOLDS BELOW MINIMUM, DID NOT HOLD,
  NOT ESTABLISHED, HARM). Do not claim more than the data supports.

To propose a new experiment, open an issue with the experiment-proposal
template. State the hypothesis, the arms, the metric, the pre-registered
threshold and sample size, and the budget before running anything.

## Commit style

Small, coherent commits, each green on its own, in conventional form:

```
<type>(<scope>): <summary>

Why: the problem or motivation, not a restatement of the diff
What: the key changes and their behaviour impact
Testing: the exact command and result, or "not run: <reason>"
Refs: an issue or pull request number
Risk: optional (migrations, rollback)
```

Types: `feat`, `fix`, `docs`, `test`, `refactor`, `perf`, `build`, `ci`,
`chore`, `revert`, `security`. Never force-push a branch that others have
pulled.

## Review

- Open a pull request using the template. Say what was and was not tested.
- Substantive changes get an **adversarial review before merge**: a reviewer
  whose job is to find what breaks (security, failure modes, privacy, abuse
  cases, scope gaps), not to approve. Material findings (security, data loss,
  correctness of the main flow, privacy) block the merge until fixed; the rest
  become issues.
- Changes to authorization, tenancy, erasure or the keystore need two
  independent reviews.
- Keep claims in documentation in step with the evidence. If a change affects
  what is measured, update `docs/RESULTS.md` in the same pull request.

## No secrets

Never commit passwords, API keys, key ids, private keys, session tokens, host
names, absolute home paths or dataset files. `tests/unit/test_repo_hygiene.py`
scans tracked files for these and fails when it finds one. If you commit a secret by
accident, treat it as leaked: rotate it first, then tell the maintainers.

## Licensing of contributions

living-context is under the Business Source License 1.1 (see
[LICENSE](LICENSE)). By submitting a contribution you agree that it is licensed
under the repository's license, including the conversion to the Apache License
2.0 on each version's change date.

## Reporting security issues

Do not open a public issue for a vulnerability. Use GitHub's private
vulnerability reporting on the repository (the "Security" tab, "Report a
vulnerability"). Include what you found, how to reproduce it, and the commit you
tested. Please do not test against systems you do not own, and do not include
real personal data or credentials in a report.
