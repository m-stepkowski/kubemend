# Kubemend — Implementation Plan (Claude Code–driven)

This plan assumes you build **with** Claude Code, session by session, at roughly 6–8 h/week. Each milestone lists: goal, scope, acceptance criteria (machine-checkable where possible), and a **session prompt** — the opening instruction you give Claude Code for that work block. The companion `CLAUDE.md` and `docs/knowledge/*` files are what make those short prompts sufficient; keep them in the repo from M0.

Working method, applied every session:

1. Start Claude Code in the repo root; it loads `CLAUDE.md` automatically.
2. Point it at the relevant knowledge file(s) for the milestone ("read docs/knowledge/harness-design.md first").
3. Ask for a **plan before code** on anything non-trivial; review the plan, then let it implement.
4. Definition of done for every session: `task test` green, `ruff` + `mypy --strict` clean, and the milestone's acceptance criteria met. No exceptions — this is your own verification gate, applied to yourself.
5. Commit per logical change; you review every diff. You are the human in your own human-in-the-loop design.

---

## M0 — Scaffold & CI (½ session)

**Goal:** empty but disciplined repo.

Scope: `pyproject.toml` (uv, py3.12), ruff + mypy(strict) + pytest wired into a `Taskfile.yaml`, CI (GitHub Actions: lint, typecheck, unit tests), package skeleton matching ARCHITECTURE.md §9, `CLAUDE.md`, `docs/knowledge/*`, `.claude/` (settings + commands) committed.

Accept: CI green on a hello-world test; `task test`, `task lint` work locally.

**Session prompt:**
> Read CLAUDE.md and ARCHITECTURE.md §9. Scaffold the repository exactly per the layout: uv project, Taskfile with test/lint/typecheck targets, GitHub Actions running them, empty modules with docstrings referencing their ARCHITECTURE.md section. No logic yet. Plan first.

---

## M1 — Harness core with FakeLLM (1.5–2 sessions)

**Goal:** the loop, fully unit-tested, zero network.

Scope: `core/` complete (loop, context, budget, loop_detector, handoff, model dataclasses), `llm/fake.py` (scripted turns), `llm/client.py` Protocol, `tools/registry.py` executor wrapper with two toy tools (`echo`, `fail_with(type)`), trace recorder v1 (JSONL).

Accept (unit tests, all against FakeLLM):
- truncation keeps head 60/tail 40 with splice marker; raw_bytes recorded
- compaction triggers at threshold; pinned blocks and last verification failure survive
- loop detector: nudge at 2nd identical call (execution skipped), abort at 3rd
- budgets: each of the three limits independently terminates with correct `reason`
- I2: transport error retried once; 4xx surfaced to context, never retried
- verification-failure path: scripted "done" claim + failing gate stub ⇒ loop continues with structured failure in context
- handoff produced on every non-verified termination; trace replays to identical event sequence

**Session prompt:**
> Read docs/knowledge/harness-design.md. Implement kubemend/core and llm/fake per the invariants I1–I5. Write the unit tests listed in IMPLEMENTATION_PLAN.md M1 first (they may fail), then implement until green. Do not add any external framework; the loop must remain readable in one screen.

---

## M2 — Lab up + read tools (2 sessions)

**Goal:** hermetic lab cluster; the three read tools working against it.

Scope: `lab/bootstrap` (kind + gitea + Argo CD + kube-prometheus-stack + Loki + Kyverno via task `lab:up`, idempotent; RBAC generator emitting the read-only kubeconfig), lab `gitops/` repo with 2 demo apps pushed into gitea and synced by Argo; `tools/observability/{prometheus,loki}.py`, `tools/kubernetes/reader.py`, `tools/redact.py` with fixture-driven tests.

Accept:
- `task lab:up` from clean machine ⇒ Argo apps Healthy/Synced in <15 min
- integration tests (marked `lab`): PromQL range query returns downsampled series; LogQL search returns known injected log line; `get_k8s_state` refuses non-allow-listed kinds and never returns a secret value (test asserts on a planted fake secret)
- redaction fixtures: bearer token, AWS key, PEM, connection-string password all masked
- first real end-to-end smoke: `kubemend run --task "describe the health of app X"` completes read-only with a handoff report (no gitops tools registered yet)

**Session prompt:**
> Read docs/knowledge/tool-contracts.md and lab notes in docs/knowledge/lab-and-evals.md. Bring up the lab per ARCHITECTURE.md §6 as Taskfile targets, then implement the three read tools and redaction. Integration tests marked `lab`. The kubeconfig used by the reader must be the generated read-only one — add a test that the agent context cannot delete a pod.

---

## M3 — GitOps write path + verification gate (2 sessions)

**Goal:** the agent can propose and the harness can verify.

Scope: `GitBackend` + local & gitea implementations, `proposer.py` with `writable_globs` path policy, `validator.py` pipeline (helm template → kyverno apply → argocd/kubectl diff → scope check), `verify/gate.py`, `policies/` pack (5–6 Kyverno policies), PR body generator.

Accept:
- path policy: attempt to write `templates/deployment.yaml` ⇒ structured error to model, nothing written
- pipeline fixtures: render error, policy violation, empty diff, out-of-scope diff each fail with the correct check name and detail string
- gate independence test: model-initiated `validate_change` result poisoned in fixture ⇒ gate re-runs and returns truth (I1)
- e2e in lab: hand-written breakage (bad image tag committed), then `kubemend run` with the real model produces a branch in gitea whose gate verdict passes, and the draft PR body contains rationale + check table
- **This e2e run is the project's "hello world" moment — record the trace, keep it in `evals/reports/first-light/`.**

**Session prompt:**
> Read docs/knowledge/tool-contracts.md (§propose/§validate) and ARCHITECTURE.md §4–5. Implement the git backends, path policy, validator pipeline and gate. Fixtures for every failure mode listed in M3 acceptance before wiring the live pipeline. kyverno and helm binaries are pinned in Taskfile — use those, do not shell out to whatever is on PATH.

---

## M4 — Scenarios + eval runner (2 sessions)

**Goal:** the six positive scenarios and the N-run eval report.

Scope: scenario format loader, runner protocol (reset → break → wait-for-symptom probe → agent run → checker → reset), the six scenarios with property checkers, `evals/runner.py` with `report.md/json`, `/run-evals` and `/new-scenario` Claude Code commands exercised.

Accept:
- each scenario is individually runnable: `kubemend evals run -s bad-image-tag -n 1`
- checkers assert properties, never diff-equality (reviewed explicitly)
- full sweep on cheap model completes unattended; report renders the pass/iters/cost/p95 table
- flakiness triage: any scenario at <50% on cheap model gets a written note (symptom too subtle? task prompt ambiguous? tool gap?) — fixing these notes **is** the work, do not tune prompts blindly
- regression rule wired into CONTRIBUTING.md: harness PRs must attach a sweep delta

**Session prompt:**
> Read docs/knowledge/lab-and-evals.md. Implement the scenario runner and the six scenarios exactly as specified, then the eval report. Property checkers only. After the first full cheap-model sweep, print the report and stop — I triage flaky scenarios manually before any prompt changes.

---

## M5 — Baseline, hardening, publish (1–2 sessions + writing time)

**Goal:** the public v0.1.

Scope: full sweep on main model (n=10) committed as the headline report; `docs/threat-model.md` (trust boundaries §1, RBAC, redaction, path policy, injection stance); README with the report table, a 90-second demo path (`task lab:up && task demo`), and honest limitations (values-only, single-repo, no memory); prompt/versioning cleanup; blog post 1 draft ("I built a K8s remediation agent that can only open PRs — here's what 60 eval runs cost and taught me").

Accept: a stranger with Docker and an Anthropic key reproduces the demo from README alone; threat model reviewed against the code (each claimed control has a test or a line reference).

---

## M6 — Negative & adversarial scenarios (1 session)

**Goal:** the part that makes platform people trust it.

Scope: `fix-needs-template-change` (expect handoff naming the template path, PR forbidden), `scope-trap` (expect no out-of-scope touches), `log-injection` (adversarial instruction planted in app logs; checker asserts identical tool-call behavior vs. baseline and no PR outside task intent). Add all three to the standing sweep.

Accept: all three pass ≥ 9/10 on main model; injection scenario documented in threat-model.md with trace excerpts. Blog post 2 ("Prompt-injecting my own SRE agent through its logs").

---

## M7 — Multi-LLM-provider support (1 session)

**Goal:** open the tool to models beyond Anthropic — the harness is a hand-written client abstraction (`llm/client.py`), not a wrapper around one vendor's API by accident.

Scope: `llm/factory.py:make_client(cfg)` dispatches on a new per-tier `ModelSpec.provider` (`anthropic | openai | bedrock`); `OpenAICompatibleClient` covers OpenAI, DeepSeek, vLLM, Ollama (anything speaking `/v1/chat/completions`, selected via `base_url`); Bedrock reuses `AnthropicClient` with an injected `AnthropicBedrock` SDK client (Claude models only — the Converse API for non-Claude Bedrock models is out of scope). `LLMError`/`LLMAuthError` replace provider-SDK exceptions at the `cli.py`/`evals/runner.py` boundary. `trace/meter.py:MeteredLLM` fixes a real bug found while building this: cheap-tier compaction/handoff calls were priced at the main model's rate (or not charged at all) — now every call is metered at its own tier's configured price. Per-model `window_tokens` drives the compaction threshold. New deps: `openai`, `anthropic[bedrock]` — no agent-framework abstraction (litellm rejected, per hard rule 1).

Accept: full unit + conformance-test suite green, including a byte-identical-request regression test proving the Anthropic-only path is unchanged; a cheap-tier sweep (n=1–3, all 9 scenarios) against a real OpenAI-compatible provider (DeepSeek) completes every scenario without a harness crash; one manual smoke run each against Bedrock and a local OpenAI-compatible endpoint, recorded in the PR. A committed main-model baseline for a new provider is a separate, explicitly budgeted decision — not part of this milestone's acceptance bar.

---

## M8a — Packaging (shipped, v0.4 — done in this session)

**Goal:** kubemend runs somewhere other than the author's laptop.

Scope: multi-arch (amd64+arm64) container image baking the Taskfile-pinned `helm`/`kyverno`/`kubectl`/`argocd` binaries (never system PATH versions, same discipline as the lab) via a `Dockerfile` built on native GitHub-hosted arm64 runners (no QEMU); publish to ghcr.io on release tags by extending the existing `.github/workflows/release.yml` (build-by-digest + `docker buildx imagetools create` manifest merge); PyPI publish via OIDC trusted publishing (`requires-python` stays `>=3.12,<3.13`, no extras split — decided against it, not worth the complexity yet); in-cluster kubeconfig support (`KubeApiClient.in_cluster()`, dispatched by `kubemend/tools/kubernetes/factory.py:build_kube_client`, selected via `KubernetesConfig.in_cluster`); a Helm chart (`charts/kubemend/`) installing the reader ServiceAccount/RBAC (reusing `lab/bootstrap/rbac.yaml`'s rules, namespace-scoped by default, `rbac.clusterScoped` for cluster-wide) and a `job.enabled`-gated Job template for a manual, human-triggered run — the actual near-term value is access control (narrow "create Job" RBAC instead of the full reader grant), not automation.

Accept: `helm install` + the manual `job.enabled=true` fallback path completes a real incident from inside the lab cluster; `pip install kubemend` followed by the README quickstart works on a clean machine; the ghcr.io image is pullable for both architectures from a single tag; `task lint`/`task test`/`task chart:lint` green.

---

## M8b — Alert-triggered operator (shipped)

**Goal:** kubemend can be triggered by a firing Prometheus alert, not only by a human running the CLI or the M8a manual Job path.

Scope: new `kubemend/operator/` package — a `ThreadingHTTPServer` webhook receiver (stdlib `http.server`, no framework) accepting Alertmanager-shaped POSTs, a pure `extract_incident(alert) -> Task | RejectReason` scope function reusing `core.model.Task`/`Scope` directly, an in-memory `CooldownTracker` (one lock, TOCTOU-safe `try_acquire`), and Job creation via `helm template | kubectl create` shelled out to the pinned binaries — reusing the M8a chart's `job.yaml` shape and escape hatches (`extraInitContainers`, `env`/`envFrom`) wholesale instead of duplicating them as a hand-built manifest, a course-correction from the original sketch (see `docs/decisions.md`) — its own RBAC identity scoped to `create`/`get`/`list` on `jobs` only, always namespace-scoped. **Webhook authentication is required, not optional**: a shared-secret bearer token checked via `hmac.compare_digest` before any scope/cooldown logic runs. New Helm templates (`operator-deployment.yaml`, `operator-service.yaml`, `operator-rbac.yaml`, `operator-serviceaccount.yaml`, `operator-secret.yaml`, `operator-job-values-configmap.yaml`), gated `operator.enabled`. New `docs/threat-model.md` §11: this is the project's first component making an autonomous mutating decision without a human in the loop, and CLAUDE.md's "single write path" rule (about tools reachable from the model inside `core/loop.py`) does not cover it — the operator's Job creation is triggered by Alertmanager, entirely outside the LLM loop, and the threat model says so explicitly rather than leaving it looking like a contradiction.

Accept: `helm install` with `operator.enabled=true` becomes ready; an authenticated synthetic Alertmanager POST creates a Job that completes a real incident; an unauthenticated POST is rejected before reaching cooldown/scope logic; a second authenticated POST for the same `(namespace, app)` within the cooldown window creates no second Job.

---

## M9 — Datadog observability provider (1–2 sessions)

**Goal:** a second `ObservabilityProvider` proves the tool layer is genuinely provider-shaped, not Prometheus/Loki-shaped by accident.

Scope: `DatadogProvider` on raw httpx, no vendor SDK (`DD-API-KEY`/`DD-APPLICATION-KEY` read from token files, mirroring `GitOpsConfig`'s `*_token_file` idiom); Datadog's v2 `/api/v2/query/timeseries` and `/api/v2/logs/events/search` endpoints; **provider-specific tool schemas** — Datadog's own query syntax, not the `promql`/`logql` argument names (schemas are contracts per `docs/knowledge/tool-contracts.md`, updated in the same PR); dispatch on `ObservabilityConfig.provider`, which today is a `Literal` nobody reads; extract the provider-wiring currently inline in `cli.py:build_read_only_registry` into a factory; abstract the eval harness's `evals/lab.py:_log_contains` LogQL coupling behind a provider-neutral symptom-probe interface; add the schema-vs-doc contract test that doesn't exist yet for either provider.

Accept: unit suite green including new contract tests for both providers; the existing 9-scenario suite still passes unmodified on `prometheus_loki`; the Datadog path is validated by contract tests plus one manual run against a real Datadog org (no lab-cluster Datadog instance).

---

## M10 — Sandbox execution substrate (multi-session; not planned in detail here)

Per-run isolated tool execution replacing direct executor calls (the Option A platform work: agent-sandbox + a Kyverno pack, Go controllers) — deferred behind M7–M9 rather than first, since opening the tool to more models and making it runnable outside the lab are the more immediate blockers to anyone else trying it. Then chart-editing behind a new risk gate. Each phase is its own blog post; the sandbox phase is KubeCon-CFP-scale work, not a milestone to bolt onto an existing session.

## Cost guardrails for the whole plan

Dev iteration on cheap model; `max_cost_usd_per_run: 1.00` hard cap from M1; sweeps: cheap for regression, main only for committed baselines (M5/M6, and any future provider's first committed number). Expected total to v0.1: ~$50–120 (per the earlier estimate; your traces will correct it after week one — check `trace/cost.py` numbers against the invoice once). M7 onward: non-Anthropic pricing entries in `config/pricing.yaml` are unverified placeholders — check against a real invoice before trusting them for a committed baseline.
