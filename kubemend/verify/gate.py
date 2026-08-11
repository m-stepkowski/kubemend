"""Independent verification (ARCHITECTURE.md §5, invariant I1).

Runs the validation pipeline fresh at termination and returns the Verdict. A
result the model produced by calling `validate_change` itself is a hint for the
model and never an input here — the gate re-runs regardless.

Failures re-enter context verbatim and check-by-check, because specificity
("kyverno: disallow-privileged FAILED on Deployment/shop/api: ...") is what
makes the retry loop converge where a generic failure stalls it.

The scope check lives on this side of the boundary and its implementation is
never surfaced to the model beyond pass/fail plus the offending resource — the
model should satisfy scope, not learn to game the checker.
"""
