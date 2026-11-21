---
name: Experiment proposal
about: Propose a new experiment, arm, benchmark or hypothesis test
title: "experiment: "
labels: experiment
---

The method is not proven. A proposal should say up front what result would
count against it. Nothing is run until the threshold and sample size below are
agreed and committed.

## Question and hypothesis

<!-- One question. State the hypothesis and which existing hypothesis (H1, H1b, H2, H3, H4, S, C) it relates to, if any. -->

## Arms and controls

<!-- What is compared. Include a negative control that should fail for its stated reason, and any control for confounds (for example session length). -->

## Data

<!-- Dataset, split, how questions are drawn, how leakage into prompts or development is prevented. -->

## Metric and pre-registered threshold

- Primary metric:
- Minimum effect that would matter:
- Statistics (interval method, clustering):
- Sample size and the power calculation behind it:
- Outcome words for each result (HOLDS, HOLDS BELOW MINIMUM, DID NOT HOLD, NOT ESTABLISHED, HARM):

## Budget

<!-- Expected model calls and spend, a hard cap, and the model and backend. -->

## Pre-registration plan

- [ ] The threshold and analysis are committed alone, before the run, as an amendment to `docs/EXPERIMENT.md` or a new pre-registration file
- [ ] Results will be reported whichever way they come out, including nulls and harms

## What would change our mind

<!-- What outcome would show the idea does not work? -->
