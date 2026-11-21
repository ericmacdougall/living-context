## What and why

<!-- The problem or motivation, then the key changes. -->

## Testing

<!-- Exact commands and results. If something was not run, say "not run: <reason>". -->

## Checklist

- [ ] Focused tests for the changed area pass; a test of changed behaviour fails against the previous implementation or a broken variant
- [ ] No secrets, host names, absolute home paths or dataset files (`tests/unit/test_repo_hygiene.py` passes)
- [ ] No paid API calls were added to tests
- [ ] Commits are small and conventional (`<type>(<scope>): <summary>` with Why / What / Testing / Refs)
- [ ] Docs updated if behaviour, status or measured results changed (`docs/ARCHITECTURE.md`, `docs/RESULTS.md`)

## Experiment and results impact

- [ ] Not applicable
- [ ] Touches a pre-registered threshold, split, prompt or metric: any change to `docs/EXPERIMENT.md` is an amendment **committed alone, before the run it affects**, with the reason (see CONTRIBUTING)
- [ ] Reports new figures: labelled exploratory or confirmatory, with model, n, commit and run provenance, and nulls reported alongside positives

## Security and privacy

<!-- Does this touch authorization, tenancy, erasure, keys or logs? If yes, it needs two independent reviews. Describe what could go wrong and how it was checked. -->

## Review

- [ ] Adversarial review requested or done; material findings fixed, others filed as issues
- [ ] I agree this contribution is licensed under the repository license (BUSL-1.1)
