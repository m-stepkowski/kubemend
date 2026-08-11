<div align="center">

# kubemend

**A GitOps-native Kubernetes remediation agent that can only open pull requests.**

It diagnoses incidents from Prometheus metrics and Loki logs, proposes a fix, and verifies that fix itself — helm render → Kyverno policy check → live diff → scope check — before it ever asks a human to approve anything. It never runs `kubectl apply`. It has no cluster credentials that can write.

[![CI](https://img.shields.io/github/actions/workflow/status/OWNER/kubemend/ci.yml?branch=main&label=CI)](../../actions)
[![License](https://img.shields.io/github/license/OWNER/kubemend)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.12-blue)](pyproject.toml)
[![Status](https://img.shields.io/badge/status-early--development-orange)](IMPLEMENTATION_PLAN.md)

</div>

> **Status:** early development, following the milestones in [`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md). Not production-ready. Eval numbers below are placeholders until [M5](IMPLEMENTATION_PLAN.md#m5--baseline-hardening-publish-12-sessions--writing-time) lands.

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

```bash
git clone https://github.com/OWNER/kubemend.git && cd kubemend
uv sync

task lab:up          # kind cluster: gitea, Argo CD, kube-prometheus-stack, Loki, Kyverno
task lab:forward      # port-forward Prometheus/Loki/gitea/Argo locally

export ANTHROPIC_API_KEY=...
kubemend run --task "shop-api pods in namespace shop are crash-looping since 10 minutes ago" \
              --scope namespace=shop,app=shop-api
```

This produces a draft PR in the lab's gitea instance, plus a full JSONL trace under `traces/`. See [`docs/threat-model.md`](docs/threat-model.md) for the trust boundaries and what's out of scope for v0.1 (single repo, values-only edits, no persistent memory across runs).

## Evals

Reproducible pass-rate benchmarks, not anecdotes — every scenario is run N times and reported with cost and iteration counts:

```bash
task evals -- --scenarios all -n 10 --model main
```

| scenario | pass | avg iterations | avg cost | p95 wall |
|---|---|---|---|---|
| _pending first baseline — see [`evals/reports/`](evals/reports/) once M5 lands_ | | | | |

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

- [ ] M0 — scaffold & CI
- [ ] M1 — harness core against a FakeLLM (loop, context, budgets, loop detector — zero network)
- [ ] M2 — lab up, read-only observability & K8s tools
- [ ] M3 — GitOps write path + independent verification gate
- [ ] M4 — fault-injection scenarios + eval runner
- [ ] M5 — baseline benchmarks, threat model, v0.1 publish
- [ ] M6 — adversarial scenarios (scope traps, log-based prompt injection)

Details and acceptance criteria per milestone: [`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md).

## Contributing

Not yet open for external contributions — first getting v0.1's harness and eval baseline solid. Issues and design discussion welcome in the meantime.

## License

[Apache 2.0](LICENSE) (or your preference — update before publishing).
