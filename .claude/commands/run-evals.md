Run an eval sweep and summarize.

1. Ensure the lab is up (`task lab:up` is idempotent).
2. Run: `task evals -- --scenarios all -n ${1:-5} --model ${2:-cheap}`.
3. Present the report table (pass, iters avg, cost avg, p95 wall) and compare against the latest committed report in evals/reports/ — call out every regression explicitly.
4. For each scenario below its previous pass rate, open the worst failing trace and give a one-paragraph diagnosis per docs/knowledge/lab-and-evals.md triage rules. Do NOT change prompts or code in this session.
