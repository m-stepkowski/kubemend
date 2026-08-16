# Knowledge: Operator Design (v0.1, M8b)

The operator (`kubemend operator serve`, `kubemend/operator/`) is a small HTTP receiver that turns a firing Alertmanager alert into the same kind of incident-response Job the manual `helm template ... | kubectl create` path (M8a) already creates by hand — see `docs/threat-model.md` §11 for the safety framing. This doc is the contract: what an alert has to look like, and what happens to it, in order.

## Request path

`POST /webhook` on the port `operator.port` exposes (default 8080), shaped like an Alertmanager webhook receiver payload:

```json
{"status": "firing", "alerts": [{"status": "firing", "labels": {...}, "annotations": {...}}, ...]}
```

Order of operations per request (`kubemend/operator/webhook.py:make_handler`), each step gating the next:

1. **Auth** (`is_authorized`, `kubemend/operator/webhook.py`) — the `Authorization: Bearer <token>` header is checked via `hmac.compare_digest` against `operator.webhookToken` *before the body is even read*. A missing or wrong token gets a `401` and nothing else runs.
2. **Parse** — the JSON body's `alerts` list. A malformed body gets a `400`.
3. **Per alert**, in order:
   a. **Scope extraction** (`extract_incident`, `kubemend/operator/scope.py`) — see the contract below.
   b. **Cooldown** (`CooldownTracker.try_acquire`, `kubemend/operator/cooldown.py`) — keyed on `(namespace, app)`.
   c. **Job creation** (`create_job`, `kubemend/operator/jobs.py`) — shells out to `helm template | kubectl create` using the same chart's `templates/job.yaml` the manual path uses.

Every alert gets exactly one structured decision logged (`received` / `rejected_unauthorized` / `rejected_malformed` / `rejected_cooldown` / `triggered` / `job_creation_failed`) via stdlib `logging` to stdout — this is the operator's audit trail. It is not a JSONL trace; the `kubemend run` inside the spawned Job still writes its own trace, unchanged.

## The alert → incident contract (`extract_incident`)

`extract_incident(alert: dict) -> Task | RejectReason` (`kubemend/operator/scope.py`) is a pure function — no I/O, so it's testable without an HTTP server.

| Alert field | Required | Becomes |
|---|---|---|
| `status` | yes | Must be `"firing"`. `"resolved"` (or anything else) is a normal, expected reject — not an error. |
| `labels.namespace` | yes | `Scope.namespace` |
| `labels.app` | yes | `Scope.app` |
| `labels.alertname` | no (defaults to `"alert"`) | Prefixed onto `Task.statement` |
| `annotations.summary` | one of summary/description required | `Task.statement`'s body |
| `annotations.description` | (fallback if `summary` absent) | `Task.statement`'s body |

Missing `namespace`/`app`, or missing both `summary` and `description`, is a hard reject (`RejectReason`) — the operator never fabricates a task statement or guesses a scope. This mirrors I2's "errors are information, not termination" spirit, applied to a process that sits outside `core/loop.py`.

The resulting `Task`/`Scope` are the exact same `kubemend/core/model.py` types a human's `kubemend run --task ... --namespace ... --app ...` produces — no parallel incident type.

## Job creation (`create_job`)

Renders `templates/job.yaml` from the same chart the manual path uses (`charts/kubemend/`, baked into the container image at `KUBEMEND_OPERATOR__CHART_DIR`), using two values sources:

- **Static** (`operator.job_values_file`, default `/etc/kubemend/operator-job-values.yaml`): `image.*`, `serviceAccount.name`, and every `job.*` field except `namespace`/`app`/`task` — rendered once at `helm install` time by `templates/operator-job-values-configmap.yaml` from the same `job.*` chart values the manual path's `--set` flags would otherwise repeat. GitOps-checkout init containers and LLM credentials configured there apply automatically; nothing to wire twice.
- **Dynamic** (stdin, per request): `job.enabled: true`, `job.namespace`, `job.app`, `job.task` — as a YAML document, not `--set` flags. Alert text can contain commas, `=`, or backslashes, all meaningful in Helm's `--set` mini-syntax; a YAML document sidesteps that escaping problem entirely.

The rendered manifest is piped to `kubectl create -f -`. Both subprocess calls use a list argv (`subprocess.run([...])`, never `shell=True`) — alert-derived text must never reach a shell.

## Cooldown

`CooldownTracker` (`kubemend/operator/cooldown.py`) is in-memory, keyed on `(namespace, app)`, one `threading.Lock` guarding a plain dict — `try_acquire` is atomic (check-then-set under the same lock acquisition) so two concurrent requests for the same scope can't both win. It resets on operator restart: a crash-loop during an alert storm defeats it. Documented, not solved, in v1 — see `docs/threat-model.md` §11.

## Versioning

Like the other `docs/knowledge/` contract docs: a change to the alert→scope mapping, the webhook payload shape, or the Job-values-file schema lands with this doc updated in the same PR.
