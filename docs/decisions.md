# Development decisions

A record of what was decided, why, and what problem prompted it — including
the mistakes, since a decision log that only shows the clean path isn't
useful to whoever hits the same wall next. Organized by milestone, roughly
chronological within each. Cross-reference: `ARCHITECTURE.md` has the design
as it stands today; this file has the reasoning and false starts that got it
there.

## M0 — Scaffold & CI

- **No agent framework (LangChain/CrewAI/AutoGen).** Stated as a hard rule in
  `CLAUDE.md` from the start, not discovered later. The loop, context
  management, and tool registry are hand-written because understanding those
  tradeoffs is the point of the project, not an implementation detail to
  abstract away.
- **`uv` + `go-task`, not Makefiles or bare pip.** `uv` for reproducible,
  fast dependency resolution with a committed lockfile; `go-task` for a
  single declarative command surface across dev, lab, and evals rather than
  a growing pile of shell scripts.
- **Pinned tool binaries (helm, kyverno, kind, argocd, kubectl) into
  `.lab/bin/`, never PATH.** A developer machine's system `helm` was a full
  major version ahead of the pinned one during early testing and would have
  rendered different manifests than CI — a silent, hard-to-diagnose
  divergence if left to PATH resolution.

## M1 — Harness core

- **Five invariants (I1–I5) as the spine of the design**, not aspirational
  principles: I1 no trusted self-report, I2 errors return rather than raise,
  I3 redaction precedes context, I4 bounded everything, I5 single write
  path. Every later design decision gets checked against these first;
  several (the quota-check stage, the read-tool relocation) exist because a
  live run showed one of them wasn't actually being upheld yet.
- **Prompt caching needs two breakpoints, not one.** Pinning only the system
  block gave ~11% cache hit rate; adding a second breakpoint at the
  conversation tail brought it to ~53%, because the system block alone is a
  small fraction of what a multi-turn investigation re-sends.
- **A loop-detector nudge budget (`MAX_BARREN_CLAIMS`), added after watching
  a real trace.** An early run spent 12 of 15 iterations re-asserting
  completion after a failed verdict, with no tool calls in between for the
  loop detector's normal repeat-call detection to catch. The fix: track
  consecutive claims-without-progress separately and hand off after three,
  cutting one real run from 15 iterations/$0.077 to 3/$0.025.
- **Context truncation keeps head 60 + tail 40 tokens with a splice marker**,
  not a flat cutoff — so a truncated result still shows the shape of what was
  cut and a hint to narrow the query, rather than silently vanishing.

## M2 — Lab + read tools

- **`task lab:up` was built and tested layer by layer, not end-to-end, until
  M2's own acceptance check forced a clean run.** The gap was real: a
  `taints: []` kind-cluster patch that had seemed harmless broke cluster
  creation outright (kind already removes the taint; the explicit empty
  patch conflicted with that). Found only by actually tearing down and
  rebuilding from nothing, which is why "has `lab:up`/`lab:down` actually
  been run clean" became a standing thing to re-verify at milestone
  boundaries rather than assumed.
- **Loki needs persistent storage in the lab, `persistence.enabled: false`
  crash-loops.** Discovered via `mkdir /var/loki: read-only file system`; the
  fix required a full `helm uninstall` because the StatefulSet's
  `volumeClaimTemplates` are immutable post-creation — a values change alone
  wasn't enough.
- **macOS hides the editable install's `.pth` file (`UF_HIDDEN`), and
  CPython 3.11+ silently skips hidden `.pth` files.** This one recurred
  *four separate times* across the project (M2, and again during the M5
  `task demo` build), each time looking like a different bug
  (`ModuleNotFoundError: No module named 'kubemend.cli'`, then later
  `No module named 'kubemend'` from a script invoked much later in a longer
  task). The durable fix isn't "run `fix:pth` once" — it's "don't trust an
  earlier pipeline step's side effect to survive an unrelated gap of
  seconds-to-minutes; re-assert the precondition immediately before the
  operation that needs it." `task demo`'s final form re-clears the flag
  directly before invoking `kubemend`, not just via its `sync` dependency.

## M3 — GitOps write path + verification gate

- **Read tools for the GitOps repo were missing at first, and the first live
  run showed exactly why that's not optional.** `propose_git_change` demands
  complete file contents, not a diff. Without a way to read the current
  file, the model reconstructed `values.yaml` from memory and silently
  dropped a field, failing every subsequent render. `read_gitops_file` and
  `list_gitops_files` were added directly in response to that trace, not
  speculatively.
- **Those read tools resolve against the base branch, never the working
  tree** — a second live run showed why the obvious alternative fails: the
  proposer leaves the checkout on `kubemend/<run_id>`, so a working-tree read
  hands the model back its own last (broken) proposal, and it concludes the
  chart's templates are broken rather than noticing its own mistake.
- **`kubectl diff --server-side` cannot be the diff engine against the
  read-only ServiceAccount.** It's a dry-run apply, and Kubernetes authorizes
  dry-run identically to a real write — the read-only credential is refused
  outright. Argo CD's `app diff --local` became the primary path specifically
  *because* Argo holds its own separate, human-governed identity; the
  alternative (granting the agent's own identity write-shaped RBAC just for
  dry-run) would have quietly broken the "agent has no cluster write path"
  claim the whole project rests on.
- **Kyverno reporting `pass: 0, fail: 0` was treated as success until a live
  sweep caught it.** A namespace-selector mismatch meant zero policy rules
  ever evaluated against the rendered manifest, and an empty pass looks
  identical to a real one by exit code alone. Fixed to fail closed: zero
  rules evaluated is now a named failure (`no_policies_applied`), not a
  pass. This is the first of several "the gate said yes and was wrong"
  findings that shaped the eventual house rule — see the M5 entry on
  contamination detection below.
- **Gitea has no draft-PR field on its create-PR API; `WIP:` in the title is
  the actual mechanism.** The backend was sending `draft: true` in the
  payload, which gitea silently ignores — every PR was landing
  review-ready, quietly breaking the "draft PRs only" claim until checked
  against gitea's real API behavior instead of trusted from the SDK-shaped
  request.
- **`kubemend.yaml` is loaded as a pydantic-settings *source*, not passed as
  constructor kwargs.** The original loader parsed YAML and passed it as
  `RunConfig(**data)`, and init arguments outrank environment variables in
  pydantic-settings — so every `KUBEMEND_*` env override was silently
  ignored for any key the file mentioned, which was nearly all of them. This
  is the kind of bug that produces no error and no warning; it was only
  caught because the gitea backend genuinely needed the env override to
  work and didn't.

## M4 — Scenarios + eval runner

- **Scenario checkers assert properties, never diff-equality**, per
  `docs/knowledge/lab-and-evals.md` from the start — many valid fixes exist
  for one fault, and a checker pinned to one exact diff would fail correct
  fixes and pass nothing else.
- **Lab fault fixtures needed real chart engineering, not just a values
  change, for three of six scenarios.** `missing-configmap-key` used
  `envFrom`, which silently tolerates a missing ConfigMap key — no
  observable failure, so the scenario never actually manifested until the
  chart switched to an explicit `configMapKeyRef` (required by default) plus
  a `checksum/config` pod annotation (a running pod is never restarted just
  because a ConfigMap it references changed; the checksum forces a rollout
  when it does). `bad-env-endpoint` needed an actual sidecar making the
  broken request, since the real container (nginx) never calls the
  configured upstream URL on its own. `quota-conflict` needed a
  `ResourceQuota` object to exist at all.
- **A checker's own threshold can be wrong in a way that looks like a model
  failure.** `missing-configmap-key`'s checker required the restored value
  to be non-empty; `configMapKeyRef` only requires the *key* to be present,
  and an empty string is a fully valid fix. Three of five runs that had
  correctly diagnosed and fixed the fault were reported as failures because
  the checker was enforcing an opinion, not the actual required property.
  Caught by reading the traces behind a below-threshold pass rate rather
  than accepting the number.
- **The validation pipeline had no live-quota-headroom check, and four
  independently-passing stages (render, policy, diff, scope) don't imply a
  proposal is actually schedulable.** Added a fifth stage
  (`Validator._quota`) that projects a proposed replica count against the
  namespace's live `ResourceQuota.status.used`, subtracting the target
  resource's own current contribution first (a namespace can hold more than
  one workload against a shared quota — the check doesn't assume the quota
  belongs solely to the app being fixed). Read-only, same surface
  `get_k8s_state` uses; no new privileged identity.
- **That new check's first version read `spec.replicas` instead of
  `status.replicas`** — desired count instead of actual, which is exactly
  backwards when the desired count is the very thing that's broken. It
  inverted the arithmetic into a negative "other usage" and let an
  over-quota proposal look like it fit, dropping `quota-conflict`'s pass
  rate to zero on the very fix the check exists to catch. The lesson
  travels: when checking "how much does X currently consume," a resource's
  own spec is what it *wants*, not what it *has* — `status` is the field
  that reflects reality when the two have diverged, which is the entire
  premise of a fault-injection scenario.
- **Fixing that math bug didn't just make the gate stricter — it made the
  model better at the task, with no prompt change.** `validate_change` is
  available to the model mid-run as a self-check; once its answer was
  correct instead of wrong, the model could use the real failure message
  to converge on a working fix by itself. `quota-conflict` went from 1/5 to
  4/5 (and later 5/5, see M5) purely from the harness telling the truth.
  Filed as a reminder that "make the verifier stricter" and "make the model
  perform better" are not always in tension — a verifier that lies to a
  model mid-loop is exactly as unhelpful as one that lies to a human holding
  a runbook.
- **Hyphenated scenario directory names (`bad-image-tag`) can't form a
  dotted Python package**, so `mypy`'s default whole-repo run treats every
  `checker.py` as colliding module `checker`. Solved by type-checking each
  scenario's `checker.py` individually (`find lab/scenarios -name
  checker.py | xargs -n1 mypy`), one process per file, rather than trying to
  force a package structure the naming convention doesn't support.

## M5 — Baseline, hardening, publish

- **The main-model baseline sweep runs at n=5, not the plan's original
  n=10.** A cost/rigor tradeoff made explicitly, not silently: n=5 on the
  full model still gives a real, non-anecdotal signal per scenario at
  roughly half the spend, and the project's own cost-guardrail section
  already treats main-model sweeps as reserved for committed baselines
  specifically because they're expensive.
- **A "stopped" background-task notification does not mean the process is
  dead — always verify with `ps` before assuming a clear field.** The
  costliest lesson of the milestone: trusting a dropped notification channel
  led to running two copies of the same harness against the same lab
  concurrently for roughly twenty minutes. Full account, including how the
  resulting data was salvaged rather than discarded wholesale:
  [`docs/blog/02-i-accidentally-ran-two-copies-of-my-own-verification-harness.md`](blog/02-i-accidentally-ran-two-copies-of-my-own-verification-harness.md).
- **A single contamination signature is not sufficient evidence of
  cleanliness.** The first pass at auditing the concurrent-run traces
  checked only for one known symptom (a polluted shared-quota number) and
  called everything else clean. A second, closer read of one "clean" trace
  found an entirely different symptom — a git/file race
  (`kyverno: failed to load resources: stat ...: no such file or
  directory`) — that the first scan had no way to catch because it was only
  taught to look for the pattern already seen once. The working rule now:
  when auditing for an unknown-shaped problem, read raw evidence for at
  least a sample before trusting a scan built from only the first symptom
  found.
- **`task demo`'s first version looked correct and failed on first real
  use, three separate ways**, each only found by actually running it rather
  than reading it: (1) the macOS `.pth`-hiding issue recurring across a
  multi-minute gap between `sync` and the delayed `kubemend` invocation —
  see the M2 entry, same root cause, different callsite; (2) no self-heal
  against a previous interrupted run leaving a fault injected and
  uncommitted-reset, which made the *second* attempt fail differently
  (`sed` producing no-op content, `git commit` failing under `set -eu`,
  masking the real problem behind a confusing error); (3) a stale-branch
  display bug (`git branch --list 'kubemend/*' | tail -1` picks whichever
  of many accumulated branches sorts last, not the one the current run
  created) — fixed by parsing the run's own trace path out of its printed
  output instead of re-deriving it from ambient git state. All three are
  instances of the same underlying pattern: a script that assumes a clean,
  first-time environment breaks the moment it's re-run, interrupted, or run
  after something else already touched the same shared state — exactly the
  condition a "reproduce this from the README" acceptance bar is supposed
  to stress.
- **`task demo` cleanup uses one combined `trap ... EXIT` handler, not
  two.** Bash only honors the *last* `trap` registered for a given signal;
  an earlier version set one trap for port-forward cleanup and a second,
  separate `git reset` at the end of the script under the assumption both
  would run — the second would have silently replaced the first rather than
  adding to it, and neither runs at all if the script exits early. A single
  handler covering every form of cleanup, gated on whether that specific
  cleanup step's precondition was actually reached, is the only version that
  is correct under early exit.

## M7 — Multi-LLM-provider support

- **Tool results already render as plain text, not native provider blocks —
  this made cross-provider support far cheaper than expected.**
  `core/context.py`'s `Exchange.render()` turns every tool call/result pair
  into two `Message` objects (`tool_call ...(...)`, `tool_result ...: ...`);
  `ToolCall.id` is traced but never sent back to any provider. No client
  needs `tool_use_id`/`tool_call_id` pairing, and OpenAI's strict
  tool-call/tool-result pairing validation never comes into play. Adding
  OpenAI-compatible support was a schema-translation and message-shaping
  exercise, not a rethink of the conversation model.
- **`anthropic.AnthropicBedrock` is not a subclass of `anthropic.Anthropic`**
  (`AnthropicBedrock.__mro__` confirms it — different transport/auth, same
  `messages.create` surface). `AnthropicClient` already accepted a `client=`
  override for tests; widening that parameter's type to a union was enough
  to serve Bedrock with zero new client code — the factory constructs
  `AnthropicBedrock` and injects it, `AnthropicClient` never knows Bedrock
  exists.
- **A pydantic-settings env-only override can fail if a required field has
  no field-level default.** `ModelSpec.name: str` has no default (only the
  *outer* `ModelConfig.cheap` field-assignment provides one, as a whole
  instance). Setting only `KUBEMEND_MODEL__CHEAP__PROVIDER` via env, without
  also setting `..._NAME`, fails construction — env-source dicts don't
  merge with a sibling field's class-level default at that nesting level.
  Not a new bug (the same would happen today overriding just
  `max_cost_usd_per_run` alone), but M7 is the first place a real
  provider-switch workflow made it observable. Any real provider switch via
  env vars must set `NAME` alongside whatever else it overrides.
- **`loop.py`'s ~150-line ceiling (hard rule 7) held, but only just.** The
  `LLMError` catch + `_fatal_error` path initially pushed the file to 160
  lines; factoring `_handoff` and `_fatal_error`'s shared "build a failed
  `RunResult` + trace it" logic into one `_finish` helper cut real
  duplication and brought it to 146 before the window-override and
  `MeteredLLM` wiring landed — both nets were checked against the ceiling
  before committing, not after.
- **The `openai` SDK in this environment depends on `httpx2`** (the httpx
  2.x line, published under that package name during its rollout), not the
  `httpx` 0.x this repo uses elsewhere for Prometheus/Loki. Genuinely two
  different packages installed side by side — surprising enough to be worth
  a comment at the one test file that has to know (`test_llm_conformance.py`'s
  wire-format test, which mocks `httpx2.Client`).
- **Writing the per-model-window test surfaced two real gotchas in
  `Context.compact()` and `ToolRegistry`, not bugs in the new code.**
  `compact()` no-ops below 3 exchanges regardless of rendered size — 2 are
  always kept, so `to_evict` stays 0 until a 3rd exists — a single
  giant-payload exchange never triggers a real compaction call no matter
  how far over threshold it pushes `should_compact()`. Separately,
  `ToolRegistry`'s own `result_token_cap` (default 6,000) is independent of
  `ContextConfig.result_token_cap` — a test that only raises the latter
  still gets its payload truncated to ~6k tokens before `Context` ever sees
  it. Both are pre-existing, correct behavior; they just made a
  synthetic-payload test fail confusingly (wrong reason) before the actual
  window-override wiring was confirmed working via `git stash` on
  `loop.py`.
- **Cost figures from before this milestone undercount cheap-tier
  spend.** `MeteredLLM` fixed a real bug: `loop.py` previously priced and
  traced only its own main-tier call, so `Context.compact()` and
  `core/handoff.request_handoff()` calls (always cheap-tier) were either
  priced at the *main* model's rate or not charged to the budget at all.
  Every M4–M6 baseline number reported before this lands is a genuine
  undercount relative to reports produced after — not a regression when the
  next baseline's total looks higher.
- **Pricing entries for non-Anthropic models are explicitly marked
  unverified** (`config/pricing.yaml`, sourced from public pricing pages at
  M7 time, not an invoice) — provider list prices change without notice,
  and this table does not update itself. Check against a real bill before
  trusting these for a committed baseline, same discipline as the original
  Anthropic entries.
- **`litellm` (and every other multi-provider abstraction library) stayed
  out**, consistent with hard rule 1: three provider clients (~350 lines
  combined) is genuinely smaller and more auditable than adopting a
  dependency whose whole job is to sit exactly where this project's
  hand-written-harness claim lives.
- **A reasoning-tier OpenAI model rejects tool calls unless
  `reasoning_effort: "none"` is set — and setting that param on a
  non-reasoning model is itself a 400.** Found by actually running the
  acceptance sweep against a real OpenAI account, not by reading the API
  docs: `gpt-5.6-luna` (the cheap-tier flagship at M7 time) returned "Function
  tools with reasoning_effort are not supported... set reasoning_effort to
  'none'" the first time a tool schema was attached, while `gpt-4.1-mini`
  rejects the same param outright as an unrecognized argument. No reliable,
  future-proof way to tell which kind a given model string is without a
  hardcoded list that goes stale the next time OpenAI ships a model —
  `openai_client.py` deliberately does not guess, and documents the gap
  next to the code. The acceptance sweep itself ran on `gpt-4.1-mini`
  (proven working with tools) rather than the reasoning-tier model this
  milestone's docs originally used as the illustrative example.
- **The all-9-scenarios acceptance canary's low pass rate (1/9) was almost
  entirely two pre-existing, provider-independent lab issues, not the new
  OpenAI client.** First, a stale `.lab/argocd-token` (the lab cluster had
  been up ~2 days) made every gate verdict fail with `"invalid session:
  Token is expired"` regardless of what was proposed — the model's actual
  fixes were correct, and the "loop_detected" terminations were it
  correctly retrying against a gate that could never pass. Second,
  `evals/lab.py:reset()` force-pushes the git revert and returns
  immediately without polling for Argo to actually converge, so running
  all 9 scenarios back-to-back (rather than in the smaller batches every
  prior sweep this project has run used) hit a transient quota race
  (`used: pods=4, limited: pods=4`) as one scenario's rollout was still
  settling when the next one's fault landed. Re-running individual
  scenarios after regenerating the token (`rm .lab/argocd-token && task
  lab:argocd-token`) passed cleanly on the first try — the actual M7
  provider-layer code was correct the whole time; neither issue is new in
  M7 or specific to it. Filed as follow-ups, not fixed under this
  milestone's scope: a token-freshness check/re-auth in the lab tooling,
  and a converge-before-next-scenario wait in `reset()`.
- **A genuine, model-specific finding, separate from both of the above**:
  `gpt-4.1-mini`'s first two `propose_git_change` attempts on
  `bad-image-tag` contained corrupted control-byte characters embedded in
  the JSON-encoded file content (`invalid_yaml` errors citing literal
  `#x0000`/`#x001e` bytes at a fixed position) — a generation artifact from
  asking a smaller model to reproduce a large text blob verbatim inside a
  function-call argument, not a schema or harness bug. The model correctly
  diagnosed the corruption from the validator's own error message and
  self-corrected on the third attempt, within budget — real evidence for
  the "verification failures re-enter verbatim and structured"
  trade-off (harness-design.md) working as intended even against a
  smaller model's rougher edges, not just against Claude.

## M8a — Packaging

- **The scope grew substantially during planning, not implementation.** The
  initial framing was "ship a Helm chart" — but a chart that only installs
  RBAC and a `helm install`-time nothing has no obvious trigger, which is a
  fair objection: what actually calls this on a cluster? Working through it
  landed on splitting the work in two: M8a ships the access-control value
  (narrow "create Job" RBAC beats a full reader kubeconfig on a human's
  laptop) via a manual, human-triggered Job path; the alert-triggered
  automation half moved to a separate, deferred M8b so it gets its own
  review as the project's first component making an autonomous mutating
  decision without a human in the loop.
- **`evals/` is deliberately excluded from the wheel
  (`[tool.hatch.build.targets.wheel] packages = ["kubemend"]`), but
  `cli.py` unconditionally imported `evals.runner.evals_app` at module
  load.** Every real install — the container image, and any future `pip
  install kubemend` — crashed immediately with `ModuleNotFoundError: No
  module named 'evals'`, on *every* invocation, not just the `evals`
  subcommand. Found by actually running the built container image, not by
  code review. The existing `test_packaging.py` docstring already
  documented this exact blind spot from an earlier `prompts/` shadowing
  regression, but the console-script test didn't catch the regression this
  time either: it runs `python -c ...` with `cwd=REPO_ROOT`, and Python's
  `-c` mode sets `sys.path[0]` to the current directory, silently making
  `evals/` importable in the test even though it is absent from a real
  install. Fixed by making the import conditional
  (`try/except ModuleNotFoundError`). The recurring lesson: a test that
  passes because of the *directory it happens to run from* is not testing
  what a real install looks like — this is the second time this exact gap
  has bitten the project (see the `prompts/` note below it in git blame),
  and a durable fix would run the console-script test from a directory
  that has neither `evals/` nor the repo root on `sys.path` at all, not
  just patch this one import.
- **`python:3.12-slim` has no `git` binary, and GitPython checks for it
  eagerly at import time, not lazily.** Every `kubemend` invocation in the
  first container build failed with `ImportError: Bad git executable`,
  including ones that never touch a repository (e.g. `--help`). Fixed with
  `apt-get install -y git` in the runtime stage. Another "only found by
  actually running the image" bug, not something a `Dockerfile` review
  would have caught without knowing GitPython's specific import-time
  behavior.
- **Sprig's `mergeOverwrite` (not plain YAML string concatenation) is
  required in the chart's ConfigMap template** to deep-merge
  `config.overrides` on top of the chart's own defaults
  (`kubernetes.in_cluster: true`) without producing a YAML document with
  duplicate top-level keys — verified empirically with `helm template`
  rather than assumed from the Sprig docs, since a naive `toYaml`-twice
  approach silently produces invalid/last-key-wins YAML instead of an
  error.
- **The Job template's GitOps checkout is a generic escape hatch
  (`extraInitContainers`/`extraVolumes`/`extraVolumeMounts` sharing one
  `workspace` `emptyDir`), not a gitea-specific init container.** The write
  path (`kubemend/tools/gitops/*_backend.py`) expects an already-cloned
  repo at a fixed path, not a fresh one — hardcoding gitea's clone command
  into the chart would have made every non-lab GitOps backend (a real
  GitHub/GitLab repo) a chart fork instead of a values override.
- **`AnthropicBedrock`-style "widen an existing injection point" pattern
  repeated itself here**: `KubeApiClient.in_cluster()` is a new alternate
  constructor, not a change to `__init__`'s signature, mirroring the M7
  decision to keep `AnthropicClient.__init__`'s `client=` override
  single-purpose rather than growing a mode-selecting parameter.

## M8b — Alert-triggered operator

- **Reusing the chart's `job.yaml` via `helm template | kubectl create`,
  instead of a hand-rolled `kubernetes.client.BatchV1Api` dict, was a
  mid-planning course-correction, not the original design.** The first
  sketch of this milestone (written during M8a's own planning) had the
  operator build its own Job manifest independently, flagging "two
  Job-manifest representations can drift" as a known risk to accept. By the
  time M8b actually started, M8a's chart Job template had grown real
  flexibility (`extraInitContainers`, `env`/`envFrom`) that a hand-built
  dict would have had to duplicate — accepting the drift risk stopped
  looking cheap. Shelling out to the pinned `helm`/`kubectl` binaries
  (matching `validator.py`'s existing precedent) eliminated the duplication
  entirely instead of mitigating it with a contract test, which was the
  original fallback plan.
- **Alert text must never reach Helm's `--set` mini-syntax.** `--set` uses
  commas, `=`, and backslashes as its own delimiters — an alert's
  `annotations.summary` is free text from whoever wrote the alerting rule
  and can contain any of those. The per-alert dynamic fields
  (`job.namespace`/`job.app`/`job.task`) go through a YAML document on
  `helm template`'s stdin instead, sidestepping the escaping problem
  entirely rather than trying to escape correctly for Helm's own DSL.
  Verified against a task string containing commas, `=`, and quotes,
  rendered correctly end to end.
- **`HTTPServer.server_bind()` calls `socket.getfqdn()`, which can hang for
  ~35 seconds on some networks (observed on macOS in this environment).**
  Found by actually running the new integration test suite, not by reading
  `http.server`'s source — the first `ThreadingHTTPServer` construction in
  the whole test session ate 35s of "setup" time before any assertion ran.
  `server_name` (what `getfqdn()` sets) is never used by this handler.
  Fixed with a small `server_bind()` override (`OperatorHTTPServer` in
  `kubemend/operator/server.py`) that skips the reverse-DNS lookup and sets
  `server_name` to the bind address directly — dropped the integration
  suite from ~39s to ~4s.
- **The operator needs the Helm chart itself baked into its own container
  image** (`COPY charts/kubemend /usr/local/lib/kubemend-tools/chart` in
  `Dockerfile`) to render Jobs via `helm template` — a new instance of the
  same "asset lives outside `kubemend/`, needs its own COPY + baked-in
  default path" pattern `config/pricing.yaml` and `policies/` already hit in
  M8a. `.dockerignore`'s blanket `charts` exclusion needed a `!charts/kubemend`
  carve-out, same shape as the existing `*.md`/`!README.md` pair.
- **The operator's own RBAC is always namespace-scoped, with no
  `clusterScoped` toggle** (unlike the reader's). `templates/job.yaml`
  always sets `metadata.namespace: {{ .Release.Namespace }}` regardless of
  which namespace the *incident* targets — the Job itself only ever runs in
  the operator's own release namespace, so there's no cross-namespace case
  for the operator's own `create`/`get`/`list` grant to cover, simplifying
  the chart relative to the reader's two-shape design.
- **The operator does not mount the main `kubemend.yaml` ConfigMap at all.**
  It never runs the LLM loop itself — only the Jobs it spawns do, and those
  already get the config Job.yaml references by name. `OperatorConfig`'s
  fields (port, cooldown, token/values file paths, namespace) are fully
  configurable via `KUBEMEND_OPERATOR__*` env vars set directly on the
  Deployment, keeping the operator's own config surface deliberately
  smaller than a full run's.

## Cross-cutting lessons

- **Every one of the recurring bugs above (`.pth` hiding, the notification
  drop) was "fixed" more than once** because the first fix addressed one
  callsite instead of the underlying assumption. The durable version of a
  fix removes the *assumption* ("an earlier step's side effect survives
  until I need it"; "a status notification reflects reality") rather than
  patching the one place it was last observed failing.
- **A live cluster run is not optional evidence for anything touching the
  validator, the lab charts, or the scenario probes.** Every genuinely
  interesting bug in this log — the Kyverno fail-open, the read-tool gap,
  the quota math inversion, the concurrency incident itself — was invisible
  to code review and only became visible against a real cluster, run
  repeatedly. Unit tests with fixtures caught regressions once a bug's shape
  was known; none of them would have surfaced the bug in the first place.
- **Commits should not carry an AI attribution trailer on this project
  unless asked.** Standing instruction, stated more than once — the default
  behavior otherwise adds `Co-Authored-By`/session-link trailers to every
  commit, which this project's author does not want.
