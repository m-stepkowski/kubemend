# Knowledge: Tool Contracts (v0.1)

These schemas are contracts: the model's behavior is trained against their descriptions, checkers assume their semantics, and traces are replayed against them. Any change lands with (a) this doc updated, (b) schema tests updated, (c) a note in the PR about eval impact.

Common executor rules (implemented once in `tools/registry.py`): JSON-Schema validation of arguments (`invalid_arguments` error back to model on failure) → execute with per-tool timeout → I2 retry policy → redaction (`tools/redact.py`) → truncation (head 60/tail 40 at `result_token_cap`, splice marker with `raw_bytes` and a narrow-your-query hint) → trace event.

---

`query_metrics`/`search_logs` are registered once per run, from whichever
provider `observability.provider` selects (`kubemend/tools/observability/
factory.py:build_observability_tools`) — a run only ever sees one provider's
pair, never both. The tool *names* and result shapes (`MetricResult`/
`LogResult`) are identical across providers; only the argument names and
executor behavior below differ, since each provider's own query language is
exposed directly rather than translated into a lowest-common-denominator DSL.

## query_metrics — prometheus_loki  (tier: read, timeout 20s)

```json
{"name": "query_metrics",
 "description": "Run a PromQL range query against the cluster's Prometheus. Prefer rate()/increase() over raw counters. Narrow by namespace/pod labels; wide queries will be truncated.",
 "input_schema": {"type": "object", "properties": {
   "promql": {"type": "string"},
   "start":  {"type": "string", "description": "RFC3339 or relative like -30m"},
   "end":    {"type": "string", "description": "RFC3339 or 'now'"},
   "step":   {"type": "string", "description": "e.g. 30s, 1m; default auto"}},
  "required": ["promql", "start", "end"]}}
```

Executor: `/api/v1/query_range`; downsample every series to ≤ `max_points` (100) via stride; payload = `{series: [{labels, points: [[ts, val], ...]}], resolution_note}`. Empty result is **not** an error: `{series: [], hint: "no series matched; check label selectors"}`.

## query_metrics — datadog  (tier: read, timeout 20s)

```json
{"name": "query_metrics",
 "description": "Run a Datadog metric query against the cluster's Datadog integration, e.g. avg:kubernetes.cpu.usage.total{pod_name:shop-api-*}. Narrow by tags; wide queries will be truncated.",
 "input_schema": {"type": "object", "properties": {
   "metric_query": {"type": "string"},
   "start":        {"type": "string", "description": "RFC3339 or relative like -30m"},
   "end":          {"type": "string", "description": "RFC3339 or 'now'"},
   "step":         {"type": "string", "description": "e.g. 30s, 1m; default Datadog's own rollup"}},
  "required": ["metric_query", "start", "end"]}}
```

Executor: POST `/api/v2/query/timeseries` (single formula/query pair, `from`/`to` in epoch ms; `step` maps to the request's `interval` in ms, omitted entirely when not given so Datadog's own rollup decides). Response is a shared time axis (`data.attributes.times`) zipped against each series' parallel value array (`data.attributes.values[i]`, `None` for a gap) and its `group_tags` (`"key:value"` strings, parsed into the `labels` dict) — `None` gap points are dropped, not kept as zero. Downsampled client-side to ≤ `max_points` afterward, same `downsample()` helper Prometheus uses. Payload shape and empty-result-is-a-hint behavior match `query_metrics — prometheus_loki` exactly. A malformed response (unexpected nesting) raises `client_error` rather than propagating a parse exception.

## search_logs — prometheus_loki  (tier: read, timeout 20s)

```json
{"name": "search_logs",
 "description": "Run a LogQL query against Loki. Use stream selectors ({namespace=\"x\", pod=~\"y.*\"}) plus line filters (|= \"error\"). Results over the limit are cut server-side; narrow the time range or add filters.",
 "input_schema": {"type": "object", "properties": {
   "logql":     {"type": "string"},
   "start":     {"type": "string"}, "end": {"type": "string"},
   "limit":     {"type": "integer", "maximum": 500, "default": 200},
   "direction": {"type": "string", "enum": ["backward", "forward"], "default": "backward"}},
  "required": ["logql", "start", "end"]}}
```

Executor: `/loki/api/v1/query_range`; payload = `{streams: [{labels, lines: [[ts, line], ...]}], total_lines, limited: bool}`. Every line passes redaction (logs are the most likely secret-leak and injection vector).

## search_logs — datadog  (tier: read, timeout 20s)

```json
{"name": "search_logs",
 "description": "Search logs via Datadog's log search syntax, e.g. 'service:shop-api status:error'. Results over the limit are cut server-side; narrow the time range or add filters.",
 "input_schema": {"type": "object", "properties": {
   "log_query": {"type": "string"},
   "start":     {"type": "string"}, "end": {"type": "string"},
   "limit":     {"type": "integer", "maximum": 500, "default": 200},
   "direction": {"type": "string", "enum": ["backward", "forward"], "default": "backward"}},
  "required": ["log_query", "start", "end"]}}
```

Executor: POST `/api/v2/logs/events/search` (`filter.query`/`from`/`to` RFC3339, `sort` derived from `direction`, `page.limit` clamped to `MAX_LIMIT` (500) server-side, never trusted from the model). Datadog returns a **flat**, ungrouped list of log events rather than Loki's pre-grouped streams — events are grouped here by their sorted tag set into `LogStream`s so the payload shape matches `search_logs — prometheus_loki` exactly. `limited` is derived from the presence of a next-page cursor (`meta.page.after`), not a count comparison. Every message passes redaction, same as Loki.

## get_k8s_state  (tier: read, timeout 15s)

```json
{"name": "get_k8s_state",
 "description": "Read Kubernetes state (read-only). Allowed kinds: pod, deployment, statefulset, service, configmap (keys only), event, hpa, resourcequota, ingress. Secrets are never readable. Provide name OR selector.",
 "input_schema": {"type": "object", "properties": {
   "kind":           {"type": "string", "enum": ["pod","deployment","statefulset","service","configmap","event","hpa","resourcequota","ingress"]},
   "namespace":      {"type": "string"},
   "name":           {"type": "string"},
   "selector":       {"type": "string", "description": "label selector"},
   "include_events": {"type": "boolean", "default": true}},
  "required": ["kind", "namespace"]}}
```

Executor: kubernetes client on the **generated read-only kubeconfig**; strips `managedFields`/`last-applied` noise; configmaps return key names + value byte-lengths only; pod specs pass env-var redaction (values → `<redacted:NAME>` unless allow-listed); events sorted by lastTimestamp desc, capped 30.

Two layers reject a disallowed kind, and which one fires depends on the caller:

* **Through the registry** (how the model always reaches it): the `kind` enum above fails schema validation first, so the model gets `{"error":{"type":"invalid_arguments", ...}}` naming the allowed values, and the executor never runs. This is the desirable order — it costs nothing and the enum is also what steers the model toward valid kinds in the first place.
* **Calling the reader directly** (tests, future internal callers): `KubernetesReader.get_state` raises `ForbiddenKind`, surfacing as `{"error":{"type":"forbidden_kind", ...}}`.

Neither is the security boundary. The ServiceAccount in `lab/bootstrap/rbac.yaml` holds no verbs on `secrets` at all, so both layers are defence in depth over an identity that cannot make the request.

## read_gitops_file  (tier: read, timeout 10s)

```json
{"name": "read_gitops_file",
 "description": "Read one file from the GitOps repository as it currently stands on the base branch. Use this before propose_git_change so the contents you submit are the current file with your edit applied, rather than a reconstruction — propose_git_change replaces the whole file, so anything you omit is deleted. Chart templates and Chart.yaml are readable too, which is how you tell which values a chart requires.",
 "input_schema": {"type": "object", "properties": {
   "path": {"type": "string", "description": "Repo-relative path, e.g. apps/shop-api/values.yaml"}},
  "required": ["path"]}}
```

Executor: confines every path to the repository root — absolute paths and any
path escaping the root return `path_not_writable`'s read-side twin
`path_not_readable`; `.git/**` is refused because it holds the push credential.
Missing file ⇒ `not_found`. Payload `{path, content, truncated}`, content capped
at 64 KB.

Reads are deliberately **wider** than `writable_globs`: the model needs
templates and Chart.yaml to know which values a chart consumes, and reading them
has no side effect. This does not touch I5 — `propose_git_change` remains the
only write path.

Registered only when the write path is (`--read-only` runs omit both): with no
proposer there is nothing to write and no reason to spend context on chart
internals.

## list_gitops_files  (tier: read, timeout 10s)

```json
{"name": "list_gitops_files",
 "description": "List files in the GitOps repository matching a glob, e.g. 'apps/shop-api/**/*' to see a chart's layout before reading its templates.",
 "input_schema": {"type": "object", "properties": {
   "pattern": {"type": "string", "description": "Glob relative to the repository root; defaults to **/*"}},
  "required": []}}
```

Executor: globs from the repository root, filters to files, excludes `.git/**`.
Payload `{pattern, paths}`, all paths repo-relative.

## propose_git_change  (tier: propose, timeout 30s)

```json
{"name": "propose_git_change",
 "description": "Propose a fix by writing complete new contents for GitOps values files and opening/updating ONE draft PR for this run. Only paths matching apps/**/values*.yaml are writable. Provide the full file content, not a diff. Rationale must reference concrete evidence (metric/log/state) gathered this run.",
 "input_schema": {"type": "object", "properties": {
   "files":        {"type": "object", "additionalProperties": {"type": "string"},
                    "description": "path -> full new file content"},
   "rationale":    {"type": "string"},
   "incident_ref": {"type": "string"}},
  "required": ["files", "rationale"]}}
```

Executor: enforce `writable_globs` per path (any violation ⇒ `path_not_writable`, nothing written); YAML-parse each file (`invalid_yaml` on failure — cheap pre-gate that saves render cycles); first call opens branch `kubemend/<run_id>` off base, later calls amend it; PR body = rationale + evidence refs + (after gate) the check table. Payload: `{branch, files_written, pr_ref?}`.

## validate_change  (tier: verify, timeout 120s)

```json
{"name": "validate_change",
 "description": "Validate the current proposal branch: helm render, Kyverno policy check, live diff, scope check, and live quota headroom. Returns per-check pass/fail with details. Use this to self-check before declaring the task done; the harness will re-run it independently anyway.",
 "input_schema": {"type": "object", "properties": {}, "required": []}}
```

Executor: runs the §5 pipeline on the run's active branch; no branch ⇒ `{"error":{"type":"no_active_proposal"}}`. Payload mirrors `Verdict`: `{passed, checks: [{name, passed, detail}], diff_summary}`. The scope-check implementation details are never surfaced to the model beyond pass/fail + offending resource — the model should satisfy scope, not learn to game the checker.

**Quota stage (added after a live sweep caught the gap):** render/policy/diff/scope all pass on a Deployment whose replica count the live `ResourceQuota` would refuse — the diff is real, in-scope, and policy-clean, yet the resulting pods would sit Pending forever. This stage renders each proposed Deployment/StatefulSet's replica count against the namespace's live `ResourceQuota.status.used.pods`, subtracting the resource's own current live contribution first (a namespace can hold more than one workload against a shared quota — the check does not assume the quota belongs solely to the app being fixed). Only `pods` today; `requests.cpu`/`requests.memory` would need the same shape. Read-only (`list_resource`/`get_resource`, the same surface `get_k8s_state` uses) — no new privilege beyond what the read-only ServiceAccount already holds. Skipped (passes) when no kube client is wired into the `Validator`.
