# Knowledge: Tool Contracts (v0.1)

These schemas are contracts: the model's behavior is trained against their descriptions, checkers assume their semantics, and traces are replayed against them. Any change lands with (a) this doc updated, (b) schema tests updated, (c) a note in the PR about eval impact.

Common executor rules (implemented once in `tools/registry.py`): JSON-Schema validation of arguments (`invalid_arguments` error back to model on failure) → execute with per-tool timeout → I2 retry policy → redaction (`tools/redact.py`) → truncation (head 60/tail 40 at `result_token_cap`, splice marker with `raw_bytes` and a narrow-your-query hint) → trace event.

---

## query_metrics  (tier: read, timeout 20s)

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

## search_logs  (tier: read, timeout 20s)

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

Executor: kubernetes client on the **generated read-only kubeconfig**; strips `managedFields`/`last-applied` noise; configmaps return key names + value byte-lengths only; pod specs pass env-var redaction (values → `<redacted:NAME>` unless allow-listed); events sorted by lastTimestamp desc, capped 30. Non-allow-listed kind ⇒ `{"error":{"type":"forbidden_kind", ...}}`.

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
 "description": "Validate the current proposal branch: helm render, Kyverno policy check, live diff, and scope check. Returns per-check pass/fail with details. Use this to self-check before declaring the task done; the harness will re-run it independently anyway.",
 "input_schema": {"type": "object", "properties": {}, "required": []}}
```

Executor: runs the §5 pipeline on the run's active branch; no branch ⇒ `{"error":{"type":"no_active_proposal"}}`. Payload mirrors `Verdict`: `{passed, checks: [{name, passed, detail}], diff_summary}`. The scope-check implementation details are never surfaced to the model beyond pass/fail + offending resource — the model should satisfy scope, not learn to game the checker.
