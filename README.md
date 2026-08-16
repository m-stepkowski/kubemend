<div align="center">

# kubemend

**A GitOps-native Kubernetes remediation agent that can only open pull requests.**

It diagnoses incidents from Prometheus metrics and Loki logs, proposes a fix, and verifies that fix itself — helm render → Kyverno policy check → live diff → scope check → live quota headroom — before it ever asks a human to approve anything. It never runs `kubectl apply`. It has no cluster credentials that can write.

[![CI](https://img.shields.io/github/actions/workflow/status/m-stepkowski/kubemend/ci.yml?branch=main&label=CI)](../../actions)
[![License](https://img.shields.io/github/license/m-stepkowski/kubemend)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.12-blue)](pyproject.toml)
[![Status](https://img.shields.io/badge/status-v0.2-blue)](IMPLEMENTATION_PLAN.md)

</div>

> **Status:** v0.2 (M0–M6 complete). Nine scenarios — six fault-injection, three adversarial/negative — with real, committed eval sweeps on the main model, not placeholders. Not production-ready: no alert-triggered runs, no multi-repo GitOps, no sandboxed tool execution yet. See [`docs/threat-model.md`](docs/threat-model.md) for what's in and out of scope.

---

## Why

Most "AI SRE agent" demos are impressive and unverifiable — a model claims it fixed something, and you take its word for it. kubemend is built the other way around: **the model's claim of success is never trusted.** Every run terminates only after an independent validation pipeline says the proposed fix renders cleanly, satisfies policy, produces a real and scoped diff, and touches nothing outside the declared incident. The agent's only actuator is a Git branch and a draft PR — a human still merges.

It's also a from-scratch agent harness, not a wrapper around LangChain/CrewAI/AutoGen. The loop, context management, tool registry, and verification gate are hand-written and documented, because understanding those trade-offs — not gluing a framework together — is the point of the project.

## How it works

```
task ──▶ Loop ──▶ tool calls ──▶ Prometheus / Loki / K8s (read-only)
          │
          └── model claims "done" ──▶ independent verification gate
                                        helm template → kyverno apply
                                        → argocd/kubectl diff → scope check
                                        → live quota headroom
                                        │
                                pass ──▶ draft PR against the GitOps repo
                                fail ──▶ structured failure fed back into the loop
```

- **Observability:** PromQL against Prometheus/Mimir, LogQL against Loki. Swappable behind a provider interface (Dynatrace/CloudWatch are future drop-ins).
- **Cluster access:** read-only ServiceAccount, allow-listed resource kinds, no Secret values ever fetched.
- **Remediation:** the agent edits Helm `values*.yaml` only — never templates directly — so diffs stay small and reviewable.
- **Verification:** re-run independently by the harness at termination, never taken on the model's word.
- **Everything is evaluated:** a hermetic `kind`-based fault-injection lab reproduces real incidents (bad image tags, OOMKills, missing config keys, broken probes...) with property-based checkers, run N times per scenario to produce pass-rate / cost / iteration tables — not cherry-picked demos. Three more scenarios are adversarial by design: a fix with no values-only solution, an incident whose real cause is out of the declared scope, and a prompt-injection attempt planted in the agent's own log evidence — each expects a handoff or a scope-clean PR, never a plausible-looking wrong answer.

Full design, invariants, and every numeric default with its rationale: **[`ARCHITECTURE.md`](ARCHITECTURE.md)**.

## Model providers

`main` and `cheap` are each configured independently, so mixing providers
across tiers (e.g. Claude on Bedrock for `main`, DeepSeek for `cheap`) is a
normal configuration, not a special case:

| Provider | `model.*.provider` | Covers | Credentials |
|---|---|---|---|
| Anthropic | `anthropic` (default) | Claude, direct API | `ANTHROPIC_API_KEY`, or an `ant auth login` profile |
| OpenAI-compatible | `openai` + `base_url` | OpenAI, DeepSeek, vLLM, Ollama, anything speaking `/v1/chat/completions` | `OPENAI_API_KEY` (local/self-hosted endpoints without auth fall back to a placeholder automatically) |
| AWS Bedrock | `bedrock` | Claude models only, via Bedrock (Converse API / non-Claude models not yet supported) | the standard AWS credential chain (env, profile, or IMDS) |

```yaml
model:
  main:
    provider: bedrock
    name: us.anthropic.claude-sonnet-5-v1:0
    aws_region: us-east-1
  cheap:
    provider: openai
    name: deepseek-v4-flash
    base_url: https://api.deepseek.com
```

See `kubemend.yaml`'s own comments for more examples, and
[`config/pricing.yaml`](config/pricing.yaml) for cost-guardrail pricing —
non-Anthropic entries there are placeholders sourced from public pricing
pages, not verified against an invoice; check before trusting them for a
committed baseline.

## Quickstart

Requires [Docker](https://docs.docker.com/get-docker/) (or Rancher Desktop —
anything `kind` can use), [`uv`](https://docs.astral.sh/uv/), and
[`go-task`](https://taskfile.dev/), plus an `ANTHROPIC_API_KEY`.

The fastest way to see it work end to end — bring up the lab, inject a real
fault, run the agent against it, and print the resulting proposal — is:

```bash
git clone https://github.com/m-stepkowski/kubemend.git && cd kubemend
uv sync

export ANTHROPIC_API_KEY=...
task lab:up      # kind cluster: gitea, Argo CD, kube-prometheus-stack, Loki, Kyverno
task demo        # inject a fault, run kubemend, show the resulting proposal (~90s)
```

`task demo` runs on the cheap model by default; pass `-- --model main` to use
the model the headline sweep below was run on:

```bash
task demo -- --model main
```

To drive it by hand instead of via the demo script:

```bash
task lab:forward   # port-forward Prometheus/Loki/gitea/Argo locally, blocks — run in another terminal

kubemend run --task "shop-api pods in namespace shop are crash-looping since 10 minutes ago" \
              --namespace shop --app shop-api
```

This writes a branch (and, with `gitops.backend: gitea`, a real draft PR in
the lab's gitea instance) plus a full JSONL trace under `traces/`. See
[`docs/threat-model.md`](docs/threat-model.md) for the trust boundaries and
what's still out of scope (single repo, values-only edits, no persistent
memory across runs).

## Evals

Reproducible pass-rate benchmarks, not anecdotes — every scenario is run N times and reported with cost and iteration counts:

```bash
task evals -- --scenarios all -n 5 --model main
```

**v0.1 baseline** (`claude-sonnet-5`, n=5 per scenario, $11.08 total —
[`evals/reports/v0.1-baseline/`](evals/reports/v0.1-baseline/)):

| scenario | pass | avg iterations | avg cost | p95 wall |
|---|---|---|---|---|
| bad-image-tag | 5/5 | 7.6 | $0.29 | 96s |
| oom-limit | 5/5 | 7.8 | $0.26 | 66s |
| missing-configmap-key | 5/5 | 12.0 | $0.35 | 106s |
| bad-probe-path | 4/5 | 8.4 | $0.38 | 348s |
| bad-env-endpoint | 5/5 | 7.4 | $0.38 | 61s |
| quota-conflict | 5/5 | 10.0 | $0.56 | 290s |

29/30 (97%) pass overall. The one failure is a genuine model struggle, not a
harness bug: `bad-probe-path`'s failing run hit `budget_exhausted` after
repeated `propose_git_change`/`validate_change` cycling without converging.

**Adversarial scenarios, M6 baseline** (`claude-sonnet-5`, n=3 per scenario,
$4.01 total, capped at a $5 budget for this sweep —
[`evals/reports/m6-baseline/`](evals/reports/m6-baseline/)):

| scenario | pass | avg iterations | avg cost |
|---|---|---|---|
| fix-needs-template-change | 2/3 | 8.7 | $0.43 |
| scope-trap | 3/3 | 15.0 | $0.71 |
| log-injection | 3/3 | 6.3 | $0.19 |

n=3 here, not n=10 — `scope-trap`'s real per-run cost (15 iterations,
$0.71) made a larger sweep infeasible under the budget for this baseline;
reported as an honest n=3 sample, not rounded up. The one failure
(`fix-needs-template-change`) is a real, specific model gap: it correctly
diagnosed a hardcoded probe scheme as the root cause but hedged on the
handoff instead of committing to "no values-only fix exists." See
[`docs/threat-model.md`](docs/threat-model.md) §9 for the log-injection
scenario's full trace excerpt.

Cheap model (`claude-haiku-4-5`) numbers, used for day-to-day regression
sweeps during development, are lower and cheaper — see
[`evals/reports/latest/`](evals/reports/latest/).

## Deploy in-cluster

A `kubemend run` from a laptop needs a kubeconfig holding the full read-only
RBAC kubemend uses. The [Helm chart](charts/kubemend/) exists to narrow that:
install it once and an on-call engineer only needs permission to *create a
Job* in one namespace, not the reader's own permissions.

```bash
helm install kubemend charts/kubemend -n kubemend-system --create-namespace
```

This installs the reader ServiceAccount and RBAC (namespace-scoped `Role` by
default; `--set rbac.clusterScoped=true` for a `ClusterRole`) and spawns
nothing — `job.enabled` defaults to `false`. To trigger a run:

```bash
helm template kubemend charts/kubemend \
  --namespace kubemend-system \
  --set job.enabled=true \
  --set job.namespace=shop \
  --set job.app=shop-api \
  --set job.task="shop-api pods are crash-looping" \
  -s templates/job.yaml \
  | kubectl create -f -
```

The Job runs with its own tightly-scoped in-cluster ServiceAccount
(`kubernetes.in_cluster: true`, no kubeconfig file involved) via the same
`ghcr.io/m-stepkowski/kubemend` image published on each release. See
[`charts/kubemend/README.md`](charts/kubemend/README.md) for wiring in a
GitOps repo checkout and the full values reference. There is no
alert-triggered automation yet — every run above is a human decision; see
[`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md)'s M8b for that design.

## Project layout

```
kubemend/          harness core, tools, gitops module, verification gate
prompts/           versioned system/compaction/handoff prompts
policies/          Kyverno pack (shared by admission and the validator)
lab/               kind bootstrap, lab GitOps repo, fault-injection scenarios
evals/             sweep runner + committed baseline reports
tests/             unit (FakeLLM, no network) + integration (against the lab)
docs/knowledge/    design contracts — read before modifying core/, tools/, or scenarios
```

Full tree and rationale for each module: [`ARCHITECTURE.md §9`](ARCHITECTURE.md).

## Roadmap

- [x] M0 — scaffold & CI
- [x] M1 — harness core against a FakeLLM (loop, context, budgets, loop detector — zero network)
- [x] M2 — lab up, read-only observability & K8s tools
- [x] M3 — GitOps write path + independent verification gate
- [x] M4 — fault-injection scenarios + eval runner
- [x] M5 — baseline benchmarks, threat model, v0.1 publish
- [x] M6 — adversarial scenarios (scope traps, log-based prompt injection), v0.2 publish
- [x] M7 — multi-LLM-provider support (OpenAI-compatible, AWS Bedrock)
- [x] M8a — packaging (container image, ghcr.io + PyPI publish, Helm chart, in-cluster kubeconfig)

Details and acceptance criteria per milestone: [`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md).

## Contributing

Not yet open for external contributions — still working through the milestones in [`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md). Issues and design discussion welcome in the meantime.

## License

[Apache 2.0](LICENSE)
