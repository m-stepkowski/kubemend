Replay and analyze trace $ARGUMENTS.

1. Run `kubemend trace replay $ARGUMENTS` and reconstruct the run timeline: iterations, tool calls with durations/raw_bytes, truncations, verdicts, cost.
2. Identify the decisive moment (wrong hypothesis, missed evidence due to truncation, loop, gate failure) with event references.
3. Recommend exactly one of: new unit fixture, new scenario, tool contract change, prompt change — with a one-paragraph justification referencing harness-design.md trade-offs.
