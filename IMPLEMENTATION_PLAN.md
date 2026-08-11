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

## After v0.1 — the road you already chose

M7 sketch (not planned in detail here): sandbox execution substrate — replace direct executor calls with per-run isolated execution (this is where the Option A platform work begins, agent-sandbox + Kyverno pack, Go controllers), then Dynatrace provider as the second `ObservabilityProvider`, then chart-editing behind a risk gate, then alert-triggered runs. Each is a blog post; the sandbox phase is a KubeCon CFP.

## Cost guardrails for the whole plan

Dev iteration on cheap model; `max_cost_usd_per_run: 1.00` hard cap from M1; sweeps: cheap for regression, main only for committed baselines (M5/M6). Expected total to v0.1: ~$50–120 (per the earlier estimate; your traces will correct it after week one — check `trace/cost.py` numbers against the invoice once).
