# I accidentally ran two copies of my own verification harness against the same cluster — here's how I found out, and what it cost

The premise of kubemend, and of the [first post](01-i-built-a-k8s-agent-that-can-only-open-prs.md),
is that you don't trust a claim — you verify it independently, against
evidence, every time. While collecting the eval numbers for that post, I broke
that rule against myself, noticed, and had to apply the exact same discipline
to my own mess that the harness applies to the model. It's a better story
about the project than the numbers are.

## What happened

I kicked off a 30-run sweep (six scenarios, five repeats each, on the full
model) as a background process. It ran long enough that my tooling's
notification channel to it dropped, and I was told — incorrectly — that the
process had stopped. I didn't check. I reset the shared git workspace back to
a clean state and launched four more sweep commands to fill in what I thought
were gaps.

The original process was still running. It had just gone quiet on the channel
that was supposed to tell me about it. For roughly twenty minutes, two
independent copies of the same harness were resetting, injecting faults into,
and reading state from the *same* git workspace and the *same* cluster,
neither aware the other existed.

I found out because a run for a scenario that has nothing to do with resource
quotas — one that just fixes an unreachable service URL — failed its
self-check with:

> `shop-api: 2 replicas would bring shop to 5 pods, exceeding quota
> shop-api-pods (hard.pods=4; other workloads in this namespace already use 3)`

"Other workloads already use 3" should never happen outside the one scenario
that deliberately breaks the replica count. Something else was live-mutating
the namespace's pod count while this run was checking its own work. That's
not a subtle bug — it's the harness accurately reporting a cluster state that
two processes were actively fighting over.

## Stopping the bleeding, then figuring out the damage

First move: `ps aux`, confirm what's actually running (not what a
notification claims is running), kill both processes. That's the boring part.

The harder question was what to do with the roughly 24 runs that had already
completed under some degree of uncertainty about which process produced them,
in what order, against what state. The instinct — mine, initially — was to
throw all of it out and start clean. That's expensive (real API spend, real
wall-clock time) and it's also lazy: "I'm not sure, so nothing counts" is not
a verification method, it's an excuse not to do one.

So I built one. Every run's full trace was still on disk — every tool call,
every argument, every check result, timestamped. I wrote a scanner that
walked all of them looking for the one signature I already knew to look for:
a quota check reporting "other workloads" using more than the true baseline
of 1 (just the other demo app). Two traces matched. Both were also the only
two traces missing a final result — meaning they'd already be excluded by the
ordinary "only count completed runs" rule, independent of any judgment call
about contamination.

That felt too clean. So before trusting it, I went back to one of the *other*
28 traces and actually read it end to end, rather than just checking it
against the one pattern I already had a name for. It had failed too — not
with a bad number, but with the model correctly proposing a fix, getting a
fully-passing self-check, and then the harness's own independent
re-verification failing anyway with:

> `kyverno: Error: failed to load resources (stat
> .../gitops-workspace/.kubemend-rendered.yaml: no such file or directory)`

A file that existed a second earlier didn't exist when the verifier went to
read it — the other process's concurrent git operations had moved the ground
out from under it, mid-check. That's a *second*, structurally different
failure signature my first scanner had no way to catch, because I'd only
taught it to look for the shape of corruption I'd already seen once.

Broadening the scan to catch filesystem- and git-level errors, not just the
one number I already knew about, found exactly one more contaminated trace —
this specific run, no others. Twenty-seven of the twenty-nine remaining
traces held up against both checks. Three replacement runs, each launched
only after confirming via `ps` that nothing else was touching the lab, closed
the gap.

## The part worth sitting with

Discarding everything would have been the "safe" choice, and it would have
been wrong in the opposite direction from trusting all of it blindly — not
dangerous, just dishonestly expensive, and it would have thrown away real,
good data along with the two bad traces. My first attempt at
being rigorous — scan for the one contamination pattern I already knew —
was *itself* a smaller version of the same mistake: checking for what I
expected to find instead of actually looking. It took a second pass,
prompted by someone asking "why aren't you using data you already have,"
to catch that.

The tool that verifies a Kubernetes agent's work independently, on every run,
because a model's self-report is never sufficient — needed the same standard
applied to me, twice, before either of us got it right. That's not really a
coincidence. It's the same failure mode at two different layers: trusting a
status report (mine from a dropped notification channel, the model's from
whatever it believes about its own fix) instead of checking the artifact.

**Cost of the incident:** roughly $5–9 of API spend on runs that turned out
to be unrecoverable as evidence (not wasted *money*, exactly — spent
confirming a real methodology worked — but not a result I'd publish either).
**Cost of doing it properly instead of discarding everything:** 3 replacement
runs, about $1. The corrected baseline — 29 of 30 runs verified clean, by
inspection, not assumption — is in
[`evals/reports/v0.1-baseline/`](../../evals/reports/v0.1-baseline/) and in
the [first post](01-i-built-a-k8s-agent-that-can-only-open-prs.md)'s numbers.
