# Getting started

A single path through the decisions a new adopter actually has to make,
each step pointing at the doc that's authoritative for it rather than
repeating it. If you just want to see it work first, the lab quickstart in
[`README.md`](../README.md#quickstart) is faster — this doc is for pointing
kubemend at a real cluster and a real GitOps repo.

Today's GitOps model is **one checked-out repo** holding both the app
charts and their values (see [`IMPLEMENTATION_PLAN.md`](../IMPLEMENTATION_PLAN.md)
M11/M12 for the planned multi-repo shapes — chart-per-app-repo with a
central values repo, and multiple values repos — neither shipped yet). If
your setup already looks like that, this doc doesn't apply cleanly yet.

## Step 1 — Which observability backend do you have?

kubemend reads metrics and logs to diagnose an incident; it never reads
traces (no third pillar yet — see `IMPLEMENTATION_PLAN.md` M13). Three
backends are supported today, one registered per run:

| You have | `observability.provider` |
|---|---|
| Self-hosted Prometheus + Loki | `prometheus_loki` (default) |
| Datadog | `datadog` |
| Grafana Cloud (hosted Mimir/Loki) | `grafana_cloud` |

Exact config fields and credential setup: README's
["Observability providers"](../README.md#observability-providers) section.
Pick this first — everything else assumes you know it.

## Step 2 — Which trigger do you want?

Two ways to start a run, and they can coexist:

- **Manual** — an on-call human runs one `helm template ... | kubectl create`
  invocation (or `kubemend run` directly from a machine with the read-only
  kubeconfig). Nothing happens until a person decides it should.
- **Alert-triggered** — the operator (`operator.enabled=true`) receives
  Alertmanager webhooks and creates the same kind of Job on its own, gated
  by a required bearer token and a per-`(namespace, app)` cooldown.

Start manual. It's the same code path underneath (the operator shells out to
`helm template | kubectl create`, same as a human would), and it lets you
build trust in what the agent actually proposes before anything triggers
without a person in the loop. Read
[`docs/threat-model.md`](threat-model.md) §11 before turning the operator on
— it's the project's first component that acts without a human, and the
threat model is explicit about what that does and doesn't change.

Full wiring for both: [`charts/kubemend/README.md`](../charts/kubemend/README.md).

## Step 3 — Confirm your GitOps repo shape

The write path (`kubemend/tools/gitops/*_backend.py`) expects one
already-cloned repo at `/workspace`, holding both charts and values. It
doesn't clone anything itself — see `charts/kubemend/README.md`'s "GitOps
repo checkout and credentials" section for the `job.extraInitContainers`
pattern. The two things worth deciding now, since they're config, not code:

- **`gitops.writable_globs`** (default `apps/**/values*.yaml`) — must match
  where your values files actually live relative to the repo root.
- **`gitops.base_branch`** — the branch kubemend proposes against; it never
  pushes to this branch directly, only opens a PR/branch off it.

## Step 4 — Install

```bash
helm install kubemend charts/kubemend -n kubemend-system --create-namespace
```

Installs the reader ServiceAccount/RBAC and spawns nothing (`job.enabled`
defaults to `false`). This alone is a safe, reversible step — no LLM calls,
no GitOps repo access yet.

## Step 5 — Do a safe first run

Before wiring up the write path, run once with `--read-only`. This
registers no write path at all: the model can investigate using
`query_metrics`/`search_logs`/`get_k8s_state` and hand off with its
findings, but nothing can open a PR even if it wanted to. This is the
cheapest way to sanity-check credentials, RBAC, and observability wiring
against a real incident before trusting the agent with write access.

```bash
kubemend run --read-only \
  --task "shop-api pods in namespace shop are crash-looping since 10 minutes ago" \
  --namespace shop --app shop-api
```

**Known gap:** `--read-only` is a CLI flag on `kubemend run` — there is
currently no equivalent Helm value on the chart's `job.yaml`/operator path
(it always passes `run` with no `--read-only`, and there's no `job.readOnly`
value to add it). So this safe-trial step only works today by invoking
`kubemend run` directly, from a machine holding the read-only kubeconfig,
before ever installing the chart's write-enabled Job — not through
`helm install` + trigger. Worth a follow-up if the chart is your primary
deployment path.

## Step 6 — Enable the write path and verify a real run

Drop `--read-only` (or wire the GitOps checkout into the Job per Step 3) and
run again. The terminal output at the end of a run:

```
model:      <model name>
reason:     verified | handoff | budget_exhausted | loop_detected | fatal_error
iterations: <n>
cost:       $<usd>
wall:       <seconds>s
trace:      traces/<run_id>.jsonl
```

**Known gap:** this summary does not currently print the PR URL even on a
successful (`reason: verified`) run with a proposal opened — check your
GitOps backend directly (the gitea/GitHub PR list) or inspect the trace:

```bash
kubemend trace replay traces/<run_id>.jsonl
```

`reason: verified` means the harness's own `verify/gate.py` independently
re-ran validation and it passed — never a claim the model made about its
own work (CLAUDE.md invariant I1). Any other `reason` means no PR was
opened; that's not a bug report, that's the harness refusing to trust an
unverified result, which is the entire point.

## Troubleshooting

- **`could not construct an LLM client`** — the error names which env var or
  credential chain applies to your configured provider(s).
- **`observability.provider is '...' but ...`** (`ObservabilityConfigError`)
  — a required credential file or config field for your chosen provider is
  missing; the message names the exact field.
- **"no GitOps workspace at `<path>` — running read-only"** — Step 3/4 isn't
  wired up yet; the CLI degrades to read-only rather than guessing, so a run
  that genuinely can't propose doesn't look identical in the report to one
  that chose not to.
