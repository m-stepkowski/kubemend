# Contributing

Project conventions, commands, and hard rules live in `CLAUDE.md` — read that
first. This file covers the one process rule specific to changes that touch
the harness or its prompts.

## The eval regression rule

Any PR that changes `kubemend/core/`, `kubemend/tools/`, `kubemend/verify/`,
or a file under `kubemend/prompts/` must attach a sweep delta:

1. Run the sweep on the cheap model before your change and after it:
   ```
   task evals -- --scenarios all -n 10 --model cheap
   ```
2. Include both `report.md` tables (or a before/after diff of the pass-rate
   column) in the PR description.
3. **A pass-rate regression on any scenario blocks merge**, even if the change
   was meant to fix something unrelated. The sweep is the only thing standing
   between "I think this is safe" and "this is safe" — see
   `docs/knowledge/lab-and-evals.md` for why n=1 proves nothing.

Committed baselines under `evals/reports/` are on the *main* model
(`--model main`) and only land at M5/M6 milestones — day-to-day regression
checks during development stay on the cheap model to keep iteration cheap.

## Triage before tuning

If a scenario sits below 50% pass rate, the fix is a written diagnosis, not a
prompt edit:

- Is the injected symptom too subtle for the model to key on?
- Is the task prompt ambiguous about what's being asked?
- Is a tool missing a capability the fix actually needs?
- Is context truncation eating the evidence before the model sees it?

Write the diagnosis next to the scenario (`lab/scenarios/<name>/`) before
touching a prompt. A prompt change without a diagnosis attached is not
accepted — it is exactly how a harness quietly overfits to one scenario at the
expense of the other five.

## Turning a failure into a fixture

Every interesting failed run is a candidate for a permanent regression test:

```
kubemend trace replay traces/<run_id>.jsonl --json > tests/fixtures/<name>.jsonl
```

Reproduce it as a unit fixture (`tests/unit/`) if the failure is about the
harness itself (loop, context, redaction), or as a new scenario
(`lab/scenarios/`) if it's about a fault pattern the agent should be able to
diagnose. This is the project's "every failure becomes a permanent fix" loop —
see `docs/knowledge/lab-and-evals.md`.

## Everything else

`task lint` and `task test` must be green. Conventional commits. See
`CLAUDE.md` for the full list of things this repo does not let an agent do
(no `kubectl apply/delete/edit` against a cluster, no committing to `main`, no
retry loops papering over flaky tests, no credentials in the repo).
