<div align="center">

# kubemend

**A GitOps-native Kubernetes remediation agent that can only open pull requests.**

It diagnoses incidents from Prometheus metrics and Loki logs, proposes a fix, and verifies that fix itself — helm render → Kyverno policy check → live diff → scope check → live quota headroom — before it ever asks a human to approve anything. It never runs `kubectl apply`. It has no cluster credentials that can write.

[![CI](https://img.shields.io/github/actions/workflow/status/m-stepkowski/kubemend/ci.yml?branch=main&label=CI)](../../actions)
[![License](https://img.shields.io/github/license/m-stepkowski/kubemend)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.12-blue)](pyproject.toml)
[![Status](https://img.shields.io/badge/status-v0.1-blue)](IMPLEMENTATION_PLAN.md)

</div>

> **Status:** v0.1 baseline (M0–M5 complete). The eval table below is a real, committed sweep on the main model — not a placeholder. Not production-ready: no alert-triggered runs, no multi-repo GitOps, no sandboxed tool execution yet. See [`docs/threat-model.md`](docs/threat-model.md) for what's in and out of scope.

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
- **Everything is evaluated:** a hermetic `kind`-based fault-injection lab reproduces real incidents (bad image tags, OOMKills, missing config keys, broken probes...) with property-based checkers, run N times per scenario to produce pass-rate / cost / iteration tables — not cherry-picked demos.

Full design, invariants, and every numeric default with its rationale: **[`ARCHITECTURE.md`](ARCHITECTURE.md)**.

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
what's out of scope for v0.1 (single repo, values-only edits, no persistent
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
Cheap model (`claude-haiku-4-5`) numbers, used for day-to-day regression
sweeps during development, are lower and cheaper — see
[`evals/reports/latest/`](evals/reports/latest/).

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
- [ ] M6 — adversarial scenarios (scope traps, log-based prompt injection)

Details and acceptance criteria per milestone: [`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md).

## Contributing

Not yet open for external contributions — first getting v0.1's harness and eval baseline solid. Issues and design discussion welcome in the meantime.

## License

[Apache 2.0](LICENSE)
