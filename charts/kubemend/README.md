# kubemend Helm chart

Installs the read-only ServiceAccount/RBAC kubemend needs to run in-cluster,
plus a `kubemend.yaml` ConfigMap and a gated `Job` template for triggering
one incident-response run. Optionally also installs the M8b operator — a
small HTTP service that receives Alertmanager webhooks and creates that same
kind of Job on its own, without a human running the command below by hand.

## Why a chart at all

The value here is access control, not automation by default. Without this,
running `kubemend` means the operator's own machine needs a kubeconfig with
the full read-only RBAC this chart installs. With it, an on-call engineer
only needs permission to *create a Job* in one namespace — a much narrower
grant — while the Job itself runs with its own tightly-scoped in-cluster
ServiceAccount. Automation is opt-in and separate: see "Alert-triggered
operator (M8b)" below.

## Install

```bash
helm install kubemend charts/kubemend -n kubemend-system --create-namespace
```

By default this installs namespace-scoped RBAC (`rbac.clusterScoped=false`)
and spawns nothing — `job.enabled` defaults to `false`.

## Trigger a run (manual fallback path)

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

Whoever runs this only needs `create` on `jobs` in `kubemend-system` — never
the reader ServiceAccount's own permissions.

## GitOps repo checkout and credentials

This chart is deliberately backend-agnostic: it does not clone your GitOps
repo or hold any git credentials. The write path
(`kubemend/tools/gitops/*_backend.py`) expects an already-cloned repo at
`/workspace`, not a fresh one, so you need to provide that via
`job.extraInitContainers` (shares the same `/workspace` `emptyDir` the main
container mounts). Example, cloning over HTTPS with a token from a Secret
you create yourself:

```yaml
job:
  extraInitContainers:
    - name: clone-gitops-repo
      image: alpine/git:2.45.2
      command: ["sh", "-c"]
      args:
        - git clone --branch main "https://x-access-token:${GIT_TOKEN}@your-git-host/org/gitops.git" /workspace
      env:
        - name: GIT_TOKEN
          valueFrom:
            secretKeyRef:
              name: kubemend-git-token
              key: token
      volumeMounts:
        - name: workspace
          mountPath: /workspace
```

`config.overrides` is where the rest of `kubemend.yaml`'s GitOps section
goes (`gitops.backend`, `gitops.gitea_api_url`, etc.) — same shape as a
local `kubemend.yaml` file.

### Split mode (M11): per-app chart repos

When `gitops.chart_repos` is set, the chart(s) live in their own repo(s),
separate from the values repo mounted at `/workspace` above. This chart
always mounts a second `emptyDir` at `/workspace-charts` for exactly that —
add one init container per chart repo, cloning to `/workspace-charts/<app>`
(the harness only ever looks for `chart_repos.checkout_root/<app>`, matching
`checkout_root: /workspace-charts` in `config.overrides`):

```yaml
config:
  overrides:
    gitops:
      repo_path: /workspace
      chart_repos:
        checkout_root: /workspace-charts
        apps:
          shop-api:
            url: https://your-git-host/org/shop-api-chart.git
            chart_path: "."

job:
  extraInitContainers:
    - name: clone-gitops-repo
      image: alpine/git:2.45.2
      command: ["sh", "-c"]
      args:
        - git clone --branch main "https://x-access-token:${GIT_TOKEN}@your-git-host/org/gitops.git" /workspace
      env:
        - name: GIT_TOKEN
          valueFrom: {secretKeyRef: {name: kubemend-git-token, key: token}}
      volumeMounts:
        - {name: workspace, mountPath: /workspace}
    - name: clone-shop-api-chart
      image: alpine/git:2.45.2
      command: ["sh", "-c"]
      args:
        - git clone --branch main "https://x-access-token:${GIT_TOKEN}@your-git-host/org/shop-api-chart.git" /workspace-charts/shop-api
      env:
        - name: GIT_TOKEN
          valueFrom: {secretKeyRef: {name: kubemend-git-token, key: token}}
      volumeMounts:
        - {name: chart-workspace, mountPath: /workspace-charts}
```

### Multi-values mode (M12): per-team or per-environment values repos

When `gitops.values_repos` is set, each app's values are routed to one of
several named repos. Checkouts go under `/workspace-values/<repo-name>` (the
third `emptyDir` this chart always mounts) — keyed by **repo name, not app**,
since one repo usually holds many apps' values and cloning per app would fetch
the same repo repeatedly:

```yaml
config:
  overrides:
    gitops:
      backend: gitea
      values_repos:
        checkout_root: /workspace-values
        repos:
          platform:
            url: https://your-git-host/org/platform-values.git
            gitea_owner: org
            gitea_repo: platform-values
          payments:
            url: https://your-git-host/org/payments-values.git
            gitea_owner: org
            gitea_repo: payments-values
            # Optional, when a repo's layout differs from the default:
            app_dir_template: "environments/prod/{app}"
        apps:
          shop-api: platform
          checkout-api: payments
        default: platform

job:
  extraInitContainers:
    - name: clone-platform-values
      image: alpine/git:2.45.2
      command: ["sh", "-c"]
      args:
        - git clone --branch main "https://x-access-token:${GIT_TOKEN}@your-git-host/org/platform-values.git" /workspace-values/platform
      env:
        - name: GIT_TOKEN
          valueFrom: {secretKeyRef: {name: kubemend-git-token, key: token}}
      volumeMounts:
        - {name: values-workspace, mountPath: /workspace-values}
    - name: clone-payments-values
      image: alpine/git:2.45.2
      command: ["sh", "-c"]
      args:
        - git clone --branch main "https://x-access-token:${GIT_TOKEN}@your-git-host/org/payments-values.git" /workspace-values/payments
      env:
        - name: GIT_TOKEN
          valueFrom: {secretKeyRef: {name: kubemend-git-token, key: token}}
      volumeMounts:
        - {name: values-workspace, mountPath: /workspace-values}
```

`gitea_owner`/`gitea_repo` are **required per repo** when `backend: gitea` —
the run fails at wiring time without them rather than falling back to the
top-level coordinates, which would open the PR against a real but wrong repo.

Split mode and multi-values mode are independent: set either, both, or
neither.

## LLM credentials

The image never bakes in an API key. Supply one via `job.env` or
`job.envFrom`, same as any other Secret-backed env var:

```yaml
job:
  env:
    - name: ANTHROPIC_API_KEY
      valueFrom:
        secretKeyRef:
          name: kubemend-llm-credentials
          key: anthropic-api-key
```

## Alert-triggered operator (M8b)

```bash
helm upgrade kubemend charts/kubemend -n kubemend-system \
  --set operator.enabled=true \
  --set operator.webhookToken="$(openssl rand -hex 32)"
```

This deploys a `Deployment` + `Service` running `kubemend operator serve` —
a stdlib `http.server`, no framework — with its own ServiceAccount and RBAC
(`create`/`get`/`list` on `jobs` only, always namespace-scoped to the release
namespace, a distinct and narrower identity than the reader SA). It shells
out to `helm template` + `kubectl create` internally to spawn Jobs, reusing
this same chart's `job.yaml` and every `job.*` value set above (GitOps
checkout, LLM credentials) — nothing to configure twice.

Point your Alertmanager at `http://<service>.<namespace>.svc:8080/webhook`
with `Authorization: Bearer <operator.webhookToken>`. Every request is
checked against that token before any other logic runs; a second alert for
the same `(namespace, app)` within `operator.cooldownSeconds` (default 300)
creates no second Job. Read `docs/threat-model.md` §11 before enabling this
in a real cluster — this is the project's first component that creates a
Job without a human in the loop.

## Values

| Key | Default | Meaning |
|---|---|---|
| `image.repository` | `ghcr.io/m-stepkowski/kubemend` | Image to run |
| `image.tag` | `""` (chart `appVersion`) | Override to pin a specific build |
| `serviceAccount.name` | `kubemend-reader` | ServiceAccount the reader RBAC and Job both use |
| `rbac.clusterScoped` | `false` | `Role`/`RoleBinding` vs. `ClusterRole`/`ClusterRoleBinding` — same rules either way |
| `config.overrides` | `{}` | Deep-merged over `kubernetes.in_cluster: true` into the rendered `kubemend.yaml` |
| `job.enabled` | `false` | Gate — a plain `helm install` never spawns a Job |
| `job.namespace` / `job.app` / `job.task` | `""` | Required when `job.enabled=true` — same meaning as `kubemend run`'s flags |
| `job.extraInitContainers` / `extraVolumes` / `extraVolumeMounts` | `[]` | How you wire in a GitOps repo checkout (see above) |
| *(always mounted)* `/workspace` / `/workspace-charts` / `/workspace-values` | — | `emptyDir`s for the values repo, per-app chart checkouts (split mode), and per-repo values checkouts (multi-values mode) — see the two sections above |
| `operator.enabled` | `false` | Gate — deploys the webhook receiver + its own ServiceAccount/RBAC |
| `operator.webhookToken` | `""` | Required when `operator.enabled=true`; render fails without it |
| `operator.cooldownSeconds` | `300` | Minimum seconds between two triggered Jobs for the same `(namespace, app)` |
| `operator.port` | `8080` | Port the webhook `Service`/`Deployment` listen on |
| `operator.env` / `envFrom` | `[]` | Extra env on the operator process itself (not the Jobs it spawns — use `job.env`/`envFrom` for those) |
