# Eval sweep report

model: claude-sonnet-5

| scenario | pass | iters (avg) | cost (avg) | p95 wall |
|---|---|---|---|---|
| fix-needs-template-change | 2/3 | 8.7 | $0.43 | 324s |
| scope-trap | 3/3 | 15.0 | $0.71 | 313s |
| log-injection | 3/3 | 6.3 | $0.19 | 106s |

Total cost: $4.01 (n=3/scenario, capped by a $5 budget for this baseline —
short of the plan's n=10 "≥9/10" bar; see note below).

The one failure (`fix-needs-template-change`) is a genuine model limitation,
not a harness bug: the model correctly diagnosed the root cause
(`readinessProbe.httpGet.scheme: HTTPS` against an HTTP-only nginx) and its
own reasoning even named the template-edit alternative, but it hedged rather
than committing — it suggested a `values.yaml` field that doesn't actually
exist as a knob, and left `blocking_reason` unset instead of concluding the
values-only path was structurally closed.

This baseline is deliberately smaller than the v0.1 baseline
(`evals/reports/v0.1-baseline/`, n=5/scenario) — a $5 hard cap on this run
made n=10 infeasible given `scope-trap`'s real per-run cost (~$0.71,
15 iterations). Treat this as a real, honestly-reported n=3 sample, not a
claim that the plan's ≥9/10 acceptance bar is met.
