# Getting started

A single path through the decisions a new adopter actually has to make,
each step pointing at the doc that's authoritative for it rather than
repeating it. If you just want to see it work first, the lab quickstart in
[`README.md`](../README.md#quickstart) is faster — this doc is for pointing
kubemend at a real cluster and a real GitOps repo.

Three GitOps repo shapes are supported, and the last two compose. **Single-repo**
(the default) is one checked-out repo holding both the app charts and their
values. **Split mode** (M11) puts each app's chart in its own repo. **Multi-values
mode** (M12) routes each app's values to one of several values repos, for
per-team or per-environment layouts. Step 3 below covers all three.

## Step 1 — Which observability backend do you have?

kubemend reads metrics and logs to diagnose an incident, and traces if you
turn them on. One provider is registered per run:

| You have | `observability.provider` | metrics | logs | traces |
|---|---|:--:|:--:|:--:|
| Self-hosted Prometheus + Loki (+ Tempo) | `prometheus_loki` (default) | ✅ | ✅ | ✅ (Tempo) |
| Datadog | `datadog` | ✅ | ✅ | ✅ (APM) |
| Grafana Cloud | `grafana_cloud` | ✅ | ✅ | ✅ (Tempo) |

**Traces are opt-in.** `observability.enable.traces` defaults to `false`,
because plenty of clusters run no tracing at all and there is no sensible
endpoint to guess. Turn it on only if you actually have APM/Tempo data:

```yaml
observability:
  provider: grafana_cloud
  enable:
    metrics: true
    logs: true
    traces: true      # then set grafana_cloud_tempo_url + _instance_id
```

The same switch works the other way — a cluster with logs but no Prometheus
can set `metrics: false` and the model simply never sees that tool.

For `prometheus_loki`, traces come from a self-hosted Tempo
(`observability.tempo_url`, default `http://localhost:3200`); the lab runs
one via `task lab:tempo`.

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

**Single-repo** (default, `gitops.chart_repos` unset): the write path
(`kubemend/tools/gitops/*_backend.py`) expects one already-cloned repo at
`/workspace`, holding both charts and values. It doesn't clone anything
itself — see `charts/kubemend/README.md`'s "GitOps repo checkout and
credentials" section for the `job.extraInitContainers` pattern.

**Split mode** (`gitops.chart_repos` set): each app's chart lives in its own
repo, the values repo stays at `/workspace`, and chart checkouts go under
`gitops.chart_repos.checkout_root` (`/workspace-charts` in-cluster) — one
directory per app, `checkout_root/<app>`. Either an explicit
`chart_repos.apps.<app>` entry or a `chart_repos.url_template` resolves
which repo an app's chart comes from. See `charts/kubemend/README.md`'s
"Split mode" section for the full init-container example, and
`docs/design/m11-multi-repo-gitops.md` for the design rationale.

**Multi-values mode** (`gitops.values_repos` set): each app's values are routed
to one of several *named* repos, checked out at
`values_repos.checkout_root/<repo-name>` (`/workspace-values` in-cluster). The
directory is keyed by repo name rather than by app, because one repo usually
holds many apps' values. Per repo you set `url`, the forge coordinates
(`gitea_owner`/`gitea_repo` — required under `backend: gitea`, and never
inferred), and optionally `writable_globs` and `app_dir_template` when that
repo's layout differs. `apps` maps app → repo name; `default` catches the rest.
Full example in `charts/kubemend/README.md`'s "Multi-values mode" section.

This is independent of split mode — set either, both, or neither.

The two things worth deciding now in any shape, since they're config, not
code:

- **`gitops.writable_globs`** (default `apps/**/values*.yaml`) — must match
  where your values files actually live relative to the repo root.
- **`gitops.base_branch`** — the branch kubemend proposes against; it never
  pushes to this branch directly, only opens a PR/branch off it.

Split mode additionally requires the Argo CD diff path (`argocd_bin` +
`argocd_token`) — there is no `kubectl diff` fallback for a multi-source
Application.

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
