# I built a Kubernetes remediation agent that can only open pull requests — here's what the eval runs cost and taught me

There's a genre of "AI SRE" demo where a model diagnoses a Kubernetes
incident, and the demo ends there. You're supposed to be impressed that it
found the right answer. Nobody checks whether it would have found the wrong
one just as confidently, on a different run, with a slightly different log
line.

I wanted to build the version of this that doesn't ask you to trust the demo.
kubemend diagnoses an incident from Prometheus metrics and Loki logs the same
way those demos do — but it can't act on its own conclusion. Its only
actuator is opening a draft pull request against a GitOps repo. And the run
doesn't succeed because the model says "fixed" — it succeeds because a
verification pipeline the model has no control over re-renders the proposed
change, checks it against the same admission policy the cluster would
enforce, diffs it against live state, and checks the diff stays inside the
declared blast radius. If any of that fails, the model gets the specific
failure back and tries again. If it can't converge, the run ends in a written
handoff, not a plausible-looking PR.

The interesting part of building this wasn't the agent loop. It was how many
ways "the harness verified it" turned out to not mean "it actually works" —
and how an eval suite that runs the same scenario N times, against a real
cluster, is the only thing that reliably surfaces the gap.

## The rule the whole project rests on

> **Success is decided only by an independent re-run of the validation
> pipeline. A model's self-report never ends a run.**

Concretely: the agent has a `validate_change` tool it can call mid-investigation
as a cheap self-check. When it stops calling tools and declares the incident
fixed, the harness throws that self-check result away and re-runs the
*entire* pipeline itself — helm render, Kyverno policy check, live diff
against the cluster, scope check, and (added later, see below) a live quota
check. There is no code path where a value the model returned can satisfy
that re-run. I have a test that constructs a tool which lies to the model —
"everything passed!" — and asserts the harness still returns the true,
failing verdict. If that test ever passes for the wrong reason, the whole
project's claim is gone.

This sounds obvious in the abstract. It stopped being obvious the moment I
started running the same scenario five times in a row against a real
cluster, because that's when the pipeline's own bugs started showing up —
and every one of them was a case where the *harness* would have told the
model (and a human reviewer) "verified" when the fix didn't actually work.

## The bug that would have shipped a policy violation

Kyverno — the admission-policy engine the validator runs against every
proposed change — reported this on an early run:

```
pass: 0, fail: 0, warn: 0, error: 0, skip: 0
```

Zero rules evaluated. My check only looked at whether the command *exited
successfully*, and an exit code of 0 with nothing evaluated looks identical,
on the surface, to an exit code of 0 with six policies cleanly passing. The
actual cause was a namespace-selector mismatch in how the manifests were
rendered — the policies were scoped to a namespace the rendered output didn't
carry, so Kyverno had nothing to check against and shrugged.

A policy check that silently checks nothing is worse than no policy check,
because it looks exactly like a passing one. The fix was to fail *closed*:
if the rule-evaluation count is zero, that's a harness fault, and the
verdict is `False`, not `True`. I only found this because I was running the
scenario against a real cluster repeatedly, not because I reasoned my way to
it — the bug was invisible in a code review of the Kyverno-invocation code,
which looked correct. It was only visible as a pattern across live runs.

## The bug that made the gate lie about a resource quota

Later, building a scenario where an agent has to fix a Deployment that
exceeds a namespace's pod quota, I added a new validator stage: render the
proposed replica count, compare it against the namespace's live
`ResourceQuota`, and reject anything that wouldn't actually be schedulable.
Render, policy, diff, and scope could all pass — and the fix could still be
one that leaves half the fleet stuck `Pending` forever, because none of those
four checks know what a quota is.

I shipped this, ran a regression sweep, and the model's pass rate on that
scenario went from bad to zero. Every failure was the same number:
`replicaCount=4`, against a quota of 4 — an obviously wrong fix. Except when
I pulled the actual trace, the *gate itself* had verified that proposal as
fitting. My own new check was wrong.

The bug: I read the live Deployment's `spec.replicas` to figure out how many
pods it currently contributed to the quota's usage — but `spec.replicas` is
the *desired* count, and the whole scenario is that the desired count is
already broken (it's set to something the cluster refuses to fully honor).
Reading the aspirational number instead of `status.replicas` — what the
Deployment actually, currently owns — inverted the arithmetic into a negative
"other usage" and made an over-quota proposal look like it fit.

Fixed, the pass rate on that scenario didn't just correct itself — it went
*up*, past where it started. Not because the model got smarter, but because
a validator that gives *correct* rejections gives the model something real to
react to. `validate_change` is available mid-run as a self-check; once its
answer stopped being wrong, the agent could use the actual failure message
("other workloads in this namespace already use 1 pod") to converge on a
correct fix by itself. The fix that made the harness stricter is the same fix
that made the agent better at the task — because being lied to by your own
verification tool is exactly as bad for a model as it is for a person holding
a runbook.

## What the eval numbers actually cost

Six scenarios, five repeats each, on `claude-sonnet-5`: **29 of 30 pass,
$11.08 total.** The one failure — one `bad-probe-path` run hitting
`budget_exhausted` after cycling `propose_git_change`/`validate_change`
without converging — is a genuine model limitation, not a harness bug. Full
table in [`evals/reports/v0.1-baseline/report.md`](../../evals/reports/v0.1-baseline/report.md).

Running N repeats per scenario isn't a nicety — it's the only way either of
the bugs above would have shown up before a human trusted a PR body that said
"the harness re-ran the full validation pipeline independently." A single
green run tells you a scenario *can* pass. It tells you nothing about a
validator stage that's wrong in a way that happens to look right on that one
random model output.

## What this doesn't claim

It doesn't diagnose incidents outside a single declared `(namespace, app)`
scope — a fix that genuinely needs to touch something else produces a written
handoff, never a PR. It only edits Helm values files, never chart templates —
a fix that needs a template change is also a handoff, by design, not a gap
quietly worked around. It has no memory between runs. It isn't triggered by
alerts; someone runs it. And the adversarial half of the eval suite — an
attacker planting "ignore previous instructions" in a log line the agent
reads — isn't built yet; the system prompt states the stance, but nothing has
tested it against a real attack under load. That's the next milestone, not
this one.

The honest version of "I built an AI agent for Kubernetes" is mostly a story
about the verification code around the agent, not the agent itself. That's
the part I'd want you to read the source of, if you read any of it:
[`kubemend/verify/gate.py`](../../kubemend/verify/gate.py) and
[`kubemend/tools/gitops/validator.py`](../../kubemend/tools/gitops/validator.py).
