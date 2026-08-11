Create a new fault-injection scenario named $ARGUMENTS.

1. Read docs/knowledge/lab-and-evals.md (scenario format + checker rules) first.
2. Propose scenario.yaml (scope, task_prompt written as an on-call human would phrase it, expected_outcome, symptom_probe) and the break.patch against lab/gitops — show me both before writing files.
3. Write checker.py asserting PROPERTIES only (never diff equality); include at least: gate passed (or handoff-without-PR for negative scenarios), scope compliance, and one scenario-specific rendered-value predicate.
4. Run it once end-to-end with the cheap model: `task evals -- -s $ARGUMENTS -n 1 --model cheap`, and paste the checker report.
5. If it fails, write the triage note per the eval rules before changing anything.
