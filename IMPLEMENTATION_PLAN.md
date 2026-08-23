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

## M9 — Datadog observability provider (shipped)

**Goal:** a second `ObservabilityProvider` proves the tool layer is genuinely provider-shaped, not Prometheus/Loki-shaped by accident.

Scope: `DatadogProvider` on raw httpx, no vendor SDK (`DD-API-KEY`/`DD-APPLICATION-KEY` read from token files, mirroring `GitOpsConfig`'s `*_token_file` idiom); Datadog's v2 `/api/v2/query/timeseries` and `/api/v2/logs/events/search` endpoints; **provider-specific tool schemas** — Datadog's own query syntax, not the `promql`/`logql` argument names (schemas are contracts per `docs/knowledge/tool-contracts.md`, updated in the same PR); dispatch on `ObservabilityConfig.provider`, which today is a `Literal` nobody reads; extract the provider-wiring currently inline in `cli.py:build_read_only_registry` into a factory; abstract the eval harness's `evals/lab.py:_log_contains` LogQL coupling behind a provider-neutral symptom-probe interface; add the schema-vs-doc contract test that doesn't exist yet for either provider.

Accept: unit suite green including new contract tests for both providers; the existing 9-scenario suite still passes unmodified on `prometheus_loki`; the Datadog path is validated by contract tests plus one manual run against a real Datadog org.

Also added (opt-in, not part of `lab:up`): `task lab:datadog-agent` installs a real Datadog Agent (node agent only, cluster-agent disabled — kube-prometheus-stack already runs kube-state-metrics) into the kind lab via `lab/bootstrap/values/datadog.yaml`, so the lab cluster's own metrics/logs can flow to a real org for validation instead of only synthetic points.

Manual run: against a real (fresh) `datadoghq.eu` org. First against no data at all — confirmed auth via `DD-API-KEY`/`DD-APPLICATION-KEY` succeeds, `query_metrics` against a metric nothing reports correctly returns the empty-series hint rather than erroring, and `search_logs` initially surfaced Datadog's real `invalid_argument(No valid indexes specified)` error as a `ClientError` instead of crashing or hanging (the org had no log index provisioned yet with zero data). Then submitted synthetic multi-tag metrics (`/api/v1/series`) and logs (`/api/v2/logs` intake) directly and re-queried through `DatadogProvider`: `query_metrics` correctly split a grouped query into 2 `MetricSeries` with the right `labels`/values, and `search_logs` correctly grouped 2 flat log events into 2 `LogStream`s by tag set (the index appeared once real data arrived) — so the zip-into-series and tag-set-grouping paths are now proven against live Datadog responses, not just `test_datadog.py`'s `MockTransport` fixtures.

Finally, ran the full pipeline live end-to-end: `task lab:up` against the kind lab, `task lab:datadog-agent` to also report the lab's own cluster data to Datadog, then `task evals -- -s bad-image-tag -n 1 --model cheap` with the cheap tier pointed at `gpt-4.1-mini` (`KUBEMEND_MODEL__CHEAP__PROVIDER=openai KUBEMEND_MODEL__CHEAP__NAME=gpt-4.1-mini`). Passed 1/1 after fixing an unrelated stale `.lab/argocd-token` (left over from cluster teardown/recreate churn during this session — `lab:argocd-token` only regenerates when the file is absent, so it never noticed the underlying cluster had changed). Confirmed afterward that the lab's *real* cluster metrics/logs (not synthetic) are queryable through `DatadogProvider`: `avg:kubernetes.cpu.usage.total{kube_namespace:shop} by {pod_name}` returned live per-pod series, and `search_logs` on `kube_namespace:shop` returned real shop-api container logs grouped by tag set with full Kubernetes metadata (`kube_deployment`, `kube_replica_set`, `image_tag`, etc.).

---

## M9b — Grafana Cloud observability provider (shipped, with M9 in v0.7)

**Goal:** a third `ObservabilityProvider` — and the counter-case to M9: where Datadog proved the seam by requiring a whole new client, Grafana Cloud proved it by requiring almost nothing.

Scope: Grafana Cloud's hosted Mimir/Loki are wire-compatible with the same `/api/v1/query_range`/`/loki/api/v1/query_range` APIs, so no new provider class — `PrometheusProvider`/`LokiProvider` gained an `auth: httpx.BasicAuth | None` constructor param (instance ID as username, one shared Access Policy token as password, token file-based like every other credential). `ObservabilityConfig` gained `grafana_cloud_*` fields (URLs/instance IDs default empty — account-specific, no sane default — validated non-empty by the factory's `_require_set`). Tool schemas are byte-identical to `prometheus_loki` (`promql`/`logql`, not renamed) since it's real PromQL/LogQL. Opt-in `task lab:grafana-agent` installs Grafana Alloy (single `deployment`, not a DaemonSet — `discovery.kubernetes` + `prometheus.scrape` + `loki.source.kubernetes` all work over the API server, no hostPath) with account values injected via `alloy.extraEnv` + Alloy's `sys.env()`, never written into the committed values file.

Shipped and validated live against a real `datadoghq.eu`-region Grafana Cloud account: real lab-cluster metrics (`up` series) and logs (tagged `namespace`/`pod`/`container` by the Alloy relabel rules, deliberately matching the lab Loki schema) round-tripped correctly through the unchanged providers.

---

# Next iterations (planned 2026-08-22, reprioritized 2026-08-23)

**Reprioritized 2026-08-23** — adoption-facing gaps (an onboarding runbook, and real-world GitOps repo shapes the current single-checkout model can't handle) outrank the original ordering below. Current priority, highest first: **M10 (adopter runbook) → M11 (multi-repo, phase A) → M12 (multi-repo, phase B) → M13 (distributed tracing) → M14–M16 (the original eval-integrity/provider-parity/sandbox sequence, unchanged in content, renumbered)**.

**M10, M11, M12 and M13 shipped 2026-08-23** (v0.8, v0.9, v0.10). M14 is next.

## M10 — Adopter runbook (shipped, v0.8)

**Goal:** a single ordered document a new adopter follows start to finish, not reference material they have to assemble themselves.

Today's docs are real but scattered: README's "Deploy in-cluster" (chart install, manual Job trigger, operator), `charts/kubemend/README.md` (GitOps checkout wiring, LLM credentials, operator webhook), and `kubemend.yaml`'s own comments (observability provider choice) each answer one piece, with no single path connecting them. The lab's `task demo` is the only true guided walkthrough, and it's lab-only.

Scope: one new doc (`docs/getting-started.md`, linked prominently from README) walking the actual decision sequence a real adopter hits, each step pointing at the existing authoritative doc rather than duplicating it:
1. **Which observability backend do you have** — `prometheus_loki` / `datadog` / `grafana_cloud`, credentials needed, pointer to README's "Observability providers" table.
2. **Which trigger do you want** — manual Job (`helm template ... | kubectl create`) vs alert-triggered operator (`operator.enabled=true`) — decision criteria (do you already have Alertmanager routing? do you want a human clicking "run" per incident, at least at first?), pointer to `charts/kubemend/README.md`.
3. **What shape is your GitOps repo** — single repo today (until M11/M12 land); the `job.extraInitContainers` clone pattern; where `writable_globs` needs to point.
4. **Install and do a safe first run** — the existing `--read-only` CLI flag (`kubemend run --read-only`, registers no write path — the model can investigate and hand off, but nothing can open a PR) as the recommended first real-incident trial before turning on the write path. This flag exists today but is undocumented outside `cli.py`'s own docstring — surface it here as the trust-building step it actually is.
5. **Verify it worked** — what a successful run looks like (draft PR opened, trace written), where to find the trace, what `validate_change`'s check table means.

Accept: a person who has never read the codebase can follow the doc alone, end to end, against their own cluster, and reach either a draft PR or a `--read-only` handoff. No new code — this is a documentation milestone; if writing it surfaces a genuine UX gap (e.g., a missing flag, a config field with no good default), record that as a separate follow-up rather than scope-creeping the doc into a code change.

## M11 — Multi-repo GitOps, phase A: per-app chart repos + one central values repo (shipped, v0.8)

**Goal:** support the GitOps shape where each app's Helm chart lives in its own repo, but all apps' values live together in one central repo — a common real-world split this project has never had to handle.

Today `GitOpsConfig` assumes exactly one checked-out repo (`repo_path`, one `writable_globs`, one `base_branch`), and the chart's own init-container pattern clones exactly one repo into `/workspace`. `propose_git_change` is the single write path into that one checkout (CLAUDE.md hard rule 3) — multi-repo support must preserve that rule's *spirit* (one tool, one kind of side effect, glob-constrained) while extending its *mechanics* (which repo, selected how).

This needs a design session before code, same discipline as M12/M16's sandbox work — the open questions are genuinely unresolved:
- **Read side**: `read_gitops_file`/`list_gitops_files` need to read chart templates/`Chart.yaml` from the app's own chart repo (to know which values a chart consumes — the existing rationale for why reads are wider than `writable_globs`) while `propose_git_change` writes only into the central values repo. Two checkouts, one read-mostly and one write-target, is a different shape from today's single checkout doing both.
- **Routing**: given a `Task`/`Scope` (namespace, app), how does the harness know which chart repo corresponds to that app? A config mapping (`app -> chart repo URL`) is the obvious answer — needs a sane story for a fleet of many apps (one entry per app is fine at small scale; large fleets may want a convention/lookup instead of an exhaustive list).
- **Validator**: `validate_change`'s helm-render step currently renders one checkout against itself; it needs to render the central repo's proposed values against the *separate* chart repo's templates — check whether `helm template <chart-path> --values <central-repo-values>` cleanly supports cross-checkout paths, or whether the validator needs to compose a temporary combined tree.
- **Chart deployment**: extend the init-container pattern to multiple clones (one per chart repo actually touched by a run, plus the one central values repo), or clone the union of all configured chart repos up front — trade off Job startup cost against complexity.

Design doc should also state explicitly: this does *not* add a new write-capable tool. `propose_git_change` stays the only one; it gains a routing step, not a sibling.

Accept: design doc reviewed and approved before implementation starts; once implemented, a lab scenario (or a new one) proves an incident in an app whose chart lives in repo A gets a correct values-only PR opened against repo B.

## M12 — Multi-repo GitOps, phase B: multiple values repos (shipped, v0.9)

**Goal:** support multiple *values* repos (e.g. per-team or per-environment), not just multiple chart repos.

Scope: extends M11's routing concept — instead of routing only by "which chart repo does this app's chart live in," also route by "which values repo does this app's values live in." Whether this is a second independent routing table or a combined `(app) -> (chart repo, values repo)` mapping is a design question for this milestone, informed by whatever M11 actually built rather than pre-decided now. Sequenced after M11 deliberately: M11 already has to solve "more than one repo in play at once," and M12 is that same mechanism applied to the write side instead of only the read side — attempting both at once risks a design that's shaped by neither case cleanly.

Also in scope (unrelated to routing, bundled in because M11's acceptance run kept tripping over it): **lab token staleness**. `lab:workspace`'s inline gitea-token creation and `lab:argocd-token` both gate regeneration on the token *file being present* (`-f`/`-s`), never on whether it still authenticates — so a `task lab:gitea`/`task lab:argocd` helm upgrade that rotates the underlying admin session (observed twice during M11: once for argocd, once for gitea) leaves a stale cached token that fails with an opaque `invalid username, password or token` deep in a run, not at token-generation time. Fix: have both targets do a cheap liveness check against the stored token (e.g. an authenticated API call) before trusting it, regenerating on failure rather than only on absence. Split out of M14 item (2), which named only the argocd half of this same bug class.

Accept: same shape as M11 — design doc first (routing), then a lab-provable case where the correct values repo (out of more than one configured) receives the PR; separately, `task lab:up` on a cluster whose gitea/argocd admin session was rotated underneath an existing token file yields working tokens with no manual `rm`.

**Shipped.** `checkout-api-values-repo` passes 3/3 (gpt-4.1-mini, mean 5.7 iterations, $0.02, 59s p95), opening its PR against the routed repo and leaving the other untouched. Token liveness fixed for both gitea and argocd, each verified against a known-bad credential. Design doc: `docs/design/m12-multi-values-repos.md`. Two items deliberately left open and recorded there: `evals/runner.py`'s `_build_lab` is still not route-aware (one workspace from `gitops.repo_path`), and `values_repos.default` has unit-test coverage only. The acceptance work also surfaced a real product defect — `list_gitops_files` answered an unmatched glob with a bare empty list, giving no signal that the *prefix* was wrong — fixed by returning the repository's actual layout (§10c).

## M13 — Distributed tracing as a third observability pillar (shipped, v0.10)

**Goal:** close the metrics/logs/traces gap — today only two of the three pillars exist.

Scope: a `query_traces` tool alongside `query_metrics`/`search_logs`, following the same provider-dispatch pattern (`ObservabilityProvider`-style protocol, one implementation per backend selected via `ObservabilityConfig.provider`). Needs, per provider: Datadog APM (`/api/v2/spans/events/search` or the equivalent trace-search endpoint), Grafana Cloud (Tempo, TraceQL, likely reusing the same Basic Auth pattern `PrometheusProvider`/`LokiProvider` just gained if Tempo's HTTP API is close enough — verify, don't assume), and `prometheus_loki` — check whether the lab should grow a Tempo instance or whether tracing is scoped to the two hosted providers only, since self-hosted tracing is a bigger lab-infra lift than metrics/logs were. Schema design should follow the tool-contracts.md discipline used for the other two pillars from day one, not bolted on after.

Accept: `query_traces` tool contract documented in `docs/knowledge/tool-contracts.md` alongside the existing two; at least one provider has a real, live-validated implementation (matching the M9/M9b bar — contract tests plus one manual run against a real account); the scenario/eval framework decides deliberately whether trace-based scenarios are in scope for this milestone or a follow-up (a trace-diagnosable incident is a different failure shape than the current crash-loop/OOM/bad-config scenario set).

**Status: shipped.** Acceptance criterion met — `TempoProvider` is live-validated end to end against a real Tempo.

Shipped: a per-pillar `observability.enable` toggle (`{metrics, logs, traces}`; metrics/logs default on so every pre-M13 config is unchanged, traces default off); `TempoProvider` (TraceQL) serving both self-hosted Tempo and Grafana Cloud; `DatadogProvider.query_traces` (APM span search); dispatch for all three providers; a lab Tempo (`task lab:tempo`, part of `lab:up`); contract sections plus the drift test extended; `ARCHITECTURE.md` §3.2, README, `kubemend.yaml`, `docs/getting-started.md`.

**Live validation (2026-08-23).** `tests/integration/test_lab_traces.py` pushes a trace over OTLP into the lab's Tempo and reads it back through the real provider, asserting TraceQL search, the `/api/traces/<id>` fetch, OTLP `batches`/`scopeSpans`/`attributes` parsing, `service.name` resolution, nanos→ms durations, slowest-first ordering, parentless-root selection, server-side duration filtering, and redaction of a planted credential in a span attribute. Grafana Cloud's hosted Tempo separately answered `/api/search` with the same contract, which is why one provider class covers both.

That live run earned its keep immediately: **Tempo's `minDuration` parameter is silently ignored for TraceQL searches** — a 5s floor still returned a 900ms trace. Fixed by composing the floor into the query as a second spanset (`{...} && {duration > Nms}`). The unit test that "covered" this asserted the parameter was *sent*, not honoured — a check that could not fail, which is the third instance of that pattern in this session and is now called out in the test's own docstring.

Two design decisions worth carrying forward:
- Tracing sits behind its own `TracesSource` Protocol, not a third method on `ObservabilityProvider` — metrics and logs exist in every backend targeted here, tracing does not, and folding it in would oblige every provider to implement a method most cannot serve. Same reason `enable.traces` defaults off.
- A disabled pillar registers **no tool**, rather than one that errors. An always-failing tool spends the model's iterations discovering a backend that isn't there.

Outstanding, deliberately scoped out:
1. **Datadog APM is not live-validated.** Auth, endpoint and the 400⇒`ClientError`-never-retried path are confirmed against the live API, but every span search returns `"No valid indexes specified"` on an org that has never enabled APM, so the response parsing is unproven. The acceptance bar asks for *one* provider live-validated; Tempo is that provider. Revisit if a Datadog-instrumented service ever exists to test against.
2. **Trace-based eval scenario**: not attempted. A trace-diagnosable incident is a different failure shape from the current scenario set and needs an instrumented demo app emitting spans — a scenario-design problem, not a provider one. Now unblocked by the lab Tempo; a natural follow-up milestone.

---

## M14 — Eval integrity + cheap-tier baseline (1 session)

**Goal:** sweep numbers can be trusted again, and the M7 surface gets its first committed baseline.

Scope: (1) **Infra-vs-model failure classification** — the validator's diff stage currently reports Argo CD auth/transport failures (`Unauthenticated`, connection refused) identically to a real diff mismatch, so a run where the model proposed the correct fix gets recorded as a model failure. Classify these as `infra_error` in the check detail; the eval runner excludes such iterations from the pass rate and reports them separately (flag, never retry — CLAUDE.md's no-retries-for-flakes rule applies). (2) **Pricing verification** — `config/pricing.yaml`'s `gpt-4.1-mini` entry (and any other model actually swept) checked against a real invoice; drop the "unverified" caveat where verified. (3) **Committed cheap-tier baseline** — full 9-scenario sweep, n=3–5, `KUBEMEND_MODEL__CHEAP__PROVIDER=openai KUBEMEND_MODEL__CHEAP__NAME=gpt-4.1-mini`, committed under `evals/reports/` (single-run evidence from this session: bad-image-tag 1/1, $0.03, 109s — a full sweep is a few dollars).

(`lab:argocd-token`/`lab:workspace` staleness, originally item (2) here, moved to M12 — it recurred for gitea-token during M11's acceptance run, so it's fixed once for both rather than split across two milestones.)

Accept: a deliberately-broken validator dependency (e.g. stale token) produces an `infra_error`-classified iteration, not a failed one, in the sweep report; the committed gpt-4.1-mini baseline lands with verified pricing behind its cost column.

## M15 — Observability-provider eval parity, grafana_cloud first (1–2 sessions)

**Goal:** one non-default observability provider goes from "one manual run" to eval-backed.

Scope: run the existing 9-scenario suite end-to-end through `provider: grafana_cloud`, using `task lab:grafana-agent` to push the lab's own data to a real account. `grafana_cloud` first because it's nearly free: real PromQL/LogQL, and the Alloy relabel rules already emit `namespace`/`pod`/`container` labels matching what `evals/lab.py:loki_log_contains_query` and the scenario probes expect. Work items: the eval runner builds its provider/probe wiring from `ObservabilityConfig` instead of assuming the lab's Loki (the `LogContainsQueryBuilder` injection seam in `LabHandle` exists for exactly this); symptom-probe timeouts get documented headroom for cloud ingestion latency (measure it, state it, do not paper over it with retries); scenario checkers audited for any remaining Loki-schema assumptions. Datadog parity is explicitly a stretch goal — it needs a Datadog-syntax query builder and different metric names in probes; attempt only after grafana_cloud passes, and time-box it.

Accept: a committed small-n sweep on `grafana_cloud` with pass rates comparable to the `prometheus_loki` baseline (differences explained, not hidden); the probe/checker layer provider-neutral by injection, not by per-provider forks of the runner; the Alloy pipeline hardened through repeated real use (closing the "validated once against one account" caveat in `docs/knowledge/lab-and-evals.md`).

## M16 — Sandbox execution substrate (multi-session; design doc first)

Per-run isolated tool execution replacing direct executor calls (the Option A platform work: agent-sandbox + a Kyverno pack, Go controllers) — then chart-editing behind a new risk gate. Still the right next *big* bet: chart-editing is gated on it, and M8b's operator raised the autonomy level enough that execution isolation now buys real risk reduction, not just hygiene. But the first session's deliverable is a **design doc + threat-model delta**, not code, answering: (a) what exactly is isolated, and where the trust boundary sits between sandbox and gate; (b) the constraint that the sandbox must sit behind the existing `registry.execute()` seam — if the design requires restructuring `core/loop.py`, that is the CLAUDE.md rule-7 design-discussion trigger firing, and the design is wrong; (c) an honest evaluation of whether chart-editing strictly requires full M16, or whether a narrower risk gate (widened `writable_globs` + a mandatory-human-review path + a stricter Kyverno pack for chart-touching PRs, all atop the existing render/policy/diff gate) could ship it a milestone earlier, with the sandbox following as defense-in-depth. KubeCon-CFP-scale work; each phase its own blog post.

## Hardening candidates (no milestone; pick up opportunistically)

- **Operator cooldown is in-memory** (M8b): a pod restart forgets all cooldowns, so an alert storm plus a crashlooping operator can spawn duplicate Jobs for the same incident. Check whether Job naming is deterministic per `(namespace, app, window)`; if not, deterministic naming is a cheap idempotency fix with outsized safety value — the operator is the one component acting without a human.
- Persistent memory across runs stays deliberately deferred: no trace or user evidence yet that it blocks anyone, and it would grow `core/`. The burden of proof is on it. (Multi-GitOps-repo support, the other item previously deferred here, is promoted above to M11/M12 as of the 2026-08-23 reprioritization.)

## Cost guardrails for the whole plan

Dev iteration on cheap model; `max_cost_usd_per_run: 1.00` hard cap from M1; sweeps: cheap for regression, main only for committed baselines (M5/M6, and any future provider's first committed number). Expected total to v0.1: ~$50–120 (per the earlier estimate; your traces will correct it after week one — check `trace/cost.py` numbers against the invoice once). M7 onward: non-Anthropic pricing entries in `config/pricing.yaml` are unverified placeholders — check against a real invoice before trusting them for a committed baseline.
