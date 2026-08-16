# kubemend Helm chart

Installs the read-only ServiceAccount/RBAC kubemend needs to run in-cluster,
plus a `kubemend.yaml` ConfigMap and a gated `Job` template for triggering
one incident-response run.

## Why a chart at all

The value here is access control, not automation (that's a separate,
alert-triggered operator — not part of this chart). Without this, running
`kubemend` means the operator's own machine needs a kubeconfig with the full
read-only RBAC this chart installs. With it, an on-call engineer only needs
permission to *create a Job* in one namespace — a much narrower grant — while
the Job itself runs with its own tightly-scoped in-cluster ServiceAccount.

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
