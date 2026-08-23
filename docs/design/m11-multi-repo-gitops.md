# M11 design — Multi-repo GitOps, phase A: per-app chart repos + one central values repo

**Status:** draft, awaiting review. Per `IMPLEMENTATION_PLAN.md` M11, implementation
does not start until this doc is approved.

**Goal restated:** support the GitOps shape where each app's Helm chart lives in its
own git repo while all apps' values live together in one central repo. Today the whole
codebase assumes one checkout doing both jobs. This design splits the *read* surface
across checkouts and routes the validator's render across them, while the *write*
surface stays exactly where it is: `propose_git_change`, one tool, one values repo,
glob-constrained (CLAUDE.md hard rule 3 — extended in mechanics, untouched in spirit).

**Non-goal:** multiple values repos. That is M12. §9 below records what this design
deliberately leaves open so M12 can build on it without rework.

---

## 1. Vocabulary and the two modes

- **Single-repo mode** — today's shape (`ARCHITECTURE.md` §4.1): one repo holds
  `apps/<app>/{Chart.yaml,templates/,values*.yaml}`. This mode remains the default and
  its behavior is byte-for-byte unchanged by this milestone.
- **Split mode** — new: the central **values repo** holds `apps/<app>/values*.yaml`
  (and, by existing lab convention, `argocd/apps/<app>.yaml`); each app's **chart
  repo** holds that app's chart (`Chart.yaml`, `templates/`, the chart's own default
  `values.yaml`).

The mode discriminator is purely configuration: `gitops.chart_repos` absent → single-repo
mode; present → split mode. No CLI flag, no new env var, no behavioral sniffing of repo
contents.

A load-bearing simplification this design leans on: **a run's `Scope` names exactly one
app** (`kubemend/core/model.py`, `Scope.namespace` + `Scope.app`). So a run needs at
most **two** checkouts — the central values repo and the scoped app's chart repo — no
matter how many chart repos the fleet has. Every piece below exploits this.

## 2. Config: the new `gitops.chart_repos` section

`GitOpsConfig` (`kubemend/config.py`) keeps every existing field with its existing
meaning. `repo_path`, `writable_globs`, `base_branch`, and the `gitea_*` fields all
continue to describe **the values repo** — in single-repo mode that repo happens to
also contain the charts, which is exactly today's reading. One field is added:

```python
class ChartRepoSpec(BaseModel):
    url: str                      # the repo's clone URL (see "why url" below)
    chart_path: str = "."         # dir of the chart within that repo
    base_branch: str = "main"     # branch chart reads resolve against

class ChartReposConfig(BaseModel):
    # Where the deployment's init containers put the clones: <checkout_root>/<app>.
    # In-cluster this is /workspace-charts (see §7); the relative default matches
    # local dev, same idiom as gitops.repo_path.
    checkout_root: Path = Path(".lab/chart-workspaces")
    # Convention route for fleets: "https://git.corp/charts/{app}.git". Explicit
    # `apps` entries override it. `{app}` is the only substitution.
    url_template: str | None = None
    template_chart_path: str = "."   # chart_path used for template-routed apps
    apps: dict[str, ChartRepoSpec] = Field(default_factory=dict)

class GitOpsConfig(BaseModel):
    ...existing fields unchanged...
    chart_repos: ChartReposConfig | None = None   # None => single-repo mode
```

Example `kubemend.yaml` fragment (split mode):

```yaml
gitops:
  backend: gitea
  repo_path: /workspace                 # the central values repo
  writable_globs: ["apps/**/values*.yaml"]
  base_branch: main
  chart_repos:
    checkout_root: /workspace-charts
    url_template: "https://git.corp/platform/{app}-chart.git"
    apps:
      shop-api:                         # explicit entry, overrides the template
        url: "https://git.corp/legacy/shop-api.git"
        chart_path: "chart"
```

**Why `url` is in config when the harness never clones.** The init containers do the
cloning (§7), so at runtime the harness only needs `checkout_root/<app>`. The URL is
still configured for three reasons: (1) **fail-fast validation** — at wiring time the
harness compares the checkout's `origin` remote against the configured URL and refuses
to run on mismatch, which catches the routing bug class (right checkout dir, wrong repo
in it) before any model tokens are spent; (2) the PR body and handoff report can name
the chart repo a human should look at; (3) it is the single place the deployment docs
point at when writing the clone init containers, so config and deployment cannot
silently disagree about which repo an app's chart lives in.

**Backward compatibility, stated plainly:** `chart_repos` defaults to `None`. An
existing `kubemend.yaml` parses to a config identical in meaning to today's, and every
code path below is specified to reduce to current behavior when `chart_repos is None`.
This is additive; there is no migration.

## 3. Routing: `app -> chart repo`

Resolution, implemented as one pure function in the gitops tool layer (proposed home:
`kubemend/tools/gitops/routing.py`), called from `cli.py`'s factories — **not** from
`core/loop.py`:

1. `chart_repos.apps[app]` if present (explicit entry wins).
2. Else `chart_repos.url_template` with `{app}` substituted, `template_chart_path`,
   base branch `"main"`.
3. Else: structured wiring-time error — "split mode is configured but no chart repo
   route exists for app `<app>`; add `gitops.chart_repos.apps.<app>` or set
   `url_template`." The run fails before the loop starts, the same fail-fast posture as
   the missing-gitea-token check in `cli.py:127`.

Either way the checkout is expected at `chart_repos.checkout_root / app`; a missing or
origin-mismatched checkout is the same wiring-time error. Routing never consults
anything the model produced — it keys off `Task.scope.app`, which the harness sets.

**Scale story, explicitly.** The exhaustive `apps:` map is fine for the fleets M11
targets (single-digit-to-tens of apps) and is *not* fine at hundreds — nobody will
maintain 300 stanzas, and the union-clone deployment pattern (§7) stops being
reasonable at that size too; the two limits arrive together, which is why this design
spends no mechanism on either. `url_template` is the cheap convention hook that covers
the common "all our chart repos follow one naming scheme" fleet in one line, and it is
designed in *now* because retrofitting it later would change routing precedence
semantics. The third option from the milestone entry — a manifest file inside the
values repo listing chart repos per app — is **rejected for M11**: it moves routing
authority from operator-owned config into repo content fetched at run time, adding a
failure mode (manifest missing/stale/malformed) and a new trust surface (repo content
steering which repo the harness reads) for no capability the template doesn't already
provide at this scale. Revisit in M12 if per-team values-repo ownership makes
config-owned routing genuinely painful; note that such a manifest would be read from
the base branch and sits outside `writable_globs`, so the agent could not edit its own
routing — the objection is operational, not a write-path hole.

## 4. Read side: `reader.py`

`GitOpsReader` (the dataclass) is **unchanged** — one `repo_path`, one `base_branch`,
reads via `git show <base_branch>:<path>`. The base-branch-not-working-tree rule keeps
its existing rationale for the values repo (the proposer parks that checkout on
`kubemend/<run_id>`) and is trivially right for the chart checkout, which nothing ever
writes to.

What changes is the *tool* layer: `read_gitops_file_spec` / `list_gitops_files_spec`
grow from taking one reader to taking a small mapping, and the tool schemas gain one
optional parameter:

```
repo: enum ["values", "chart"], default "values"
```

- `"values"` → the reader over `gitops.repo_path` (identical to today's reader).
- `"chart"` → a second `GitOpsReader` over `checkout_root/<scope.app>`, with that
  route's `base_branch`. Because scope is single-app, `"chart"` is unambiguous per run
  — no repo URLs or keys in the model's vocabulary, and the tool surface does not grow
  when the fleet does.
- In single-repo mode, `"chart"` returns a structured `client_error`: "this is a
  single-repo setup; chart templates live in the same repo — read them with the
  default repo." Same schema in both modes, so `docs/knowledge/tool-contracts.md`
  documents one contract, not two variants.

Chart reads are **rooted at `chart_path`**: the model asks for
`templates/deployment.yaml`, and the executor prefixes `chart_path` before the
`git show`. This keeps the model's world uniform across apps regardless of where each
chart repo keeps its chart, and it slightly *narrows* the chart-repo read surface to
the chart itself as a free side effect. `list_gitops_files(repo="chart")` filters to
the `chart_path` prefix and re-relativizes the same way.

Defaulting `repo` to `"values"` means every existing prompt, scenario, and trace
replay is untouched. The parameter addition is a schema change, so per CLAUDE.md the
same PR updates `docs/knowledge/tool-contracts.md`.

`kubemend/prompts/system.md.j2` line 8's "the GitOps repository" (singular) gets a
conditional block: in split mode the run context states that this app's chart is
readable under `repo: "chart"`, values under the default, and that only the values
repo is writable. Prompt change, versioned and reviewed like code per convention — no
inline strings.

## 5. Write side: `proposer.py` — no code change

`Proposer`, `is_writable`, and the `propose_git_change` schema are **unchanged**. This
deserves its own section precisely because nothing happens in it:

- The write target in split mode is always the one central values repo — the same
  `gitops.repo_path` and the same single `GitBackend` instance `cli.py` builds today.
  `writable_globs` keeps meaning "paths within the values repo," which is what it
  already means.
- The chart checkouts are wired into a `GitOpsReader` and the `Validator` only. No
  `GitBackend` is ever constructed for a chart repo, so the inability to write to one
  is structural, not policy — the same by-construction property `backend.py`'s
  docstring claims for I5 today.
- **Explicit statement, per the milestone:** `propose_git_change` remains the only
  write-capable tool. It gains no sibling and, in M11, not even a routing step of its
  own — the milestone's "routing step" lands entirely in read-side and validator
  wiring, because phase A's write target is singular by definition. (M12 is where
  write-side routing appears, and §9 shows the seam it will use.)

`verify/gate.py` is also unchanged: `_apps_touched` maps `apps/<name>/values*.yaml`
back to app names, and the values repo keeps that layout in split mode.

## 6. Validator: `validator.py`, stage by stage

`Validator` keeps `repo_path` (the values repo) and gains one optional field, which is
also its mode switch:

```python
# app -> directory containing that app's chart (checkout_root/<app>/<chart_path>),
# resolved by routing at wiring time. None => single-repo mode.
chart_dirs: Mapping[str, Path] | None = None
```

with one helper replacing the hardcoded join at `validator.py:218`:

```python
def _chart_dir(self, app: str) -> Path:
    if self.chart_dirs is None:
        return self.repo_path / "apps" / app          # today's line, verbatim
    if app not in self.chart_dirs:
        raise KeyError(...)                            # surfaced as a failed CheckResult
    return self.chart_dirs[app]
```

The `KeyError` case is real: the model can write `apps/<other-app>/values.yaml` for an
app outside scope. Today that renders fine and dies at the scope check; in split mode
the other app's chart was never cloned, so `_render` fails first — the check detail
must say "no chart checkout for `<other-app>`; this run's scope is `<scope.app>`" so
the failure reads as scope, not as harness breakage.

**Render (`_render`)** — the sharpest edge, and a finding from reading the code rather
than the docs: `helm template` is invoked *without any `--values` flag*
(`validator.py:220-229`). Single-repo mode works because the app's values.yaml sits
inside the chart directory and helm picks it up implicitly. In split mode that
implicit pickup is exactly what breaks, so the split-mode render becomes:

```
helm template <app> <chart_dirs[app]> \
  --values <repo_path>/apps/<app>/values.yaml \
  --namespace <scope.namespace> --kube-version <kube_version>
```

Helm has no objection to the chart dir and the values file living under different
roots — both are plain local paths — so **no composed temporary tree is needed** for
render, policy, or the kubectl diff. The values path deliberately reads the values
repo's **working tree**, which sits on the run branch: that is the current §5 contract
("executed against the active branch's working tree") expressed explicitly instead of
via helm's implicit chart-dir lookup. The chart checkout, meanwhile, just sits on its
base branch — nothing checks it out anywhere else.

Only `values.yaml` is passed, matching single-repo behavior exactly: today a proposal
touching only `values-prod.yaml` renders without it and fails `no_effective_change`.
That is a **pre-existing gap** (`ARCHITECTURE.md` §5 says "base+env values"; the code
renders base only), shared by both modes. Fixing it means learning the env overlay
order — the right source is the Application spec's `spec.source.helm.valueFiles`
(single-repo) / the `$values/...` refs (split) — and it is filed as a follow-up issue
per the no-TODO-without-issue rule, not smuggled into M11.

**Policy (`_policy`)** — no change. It operates on the rendered stream and a temp file
under `repo_path`; nothing in it knows where the chart came from.

**Diff (`_diff`)** — **RESOLVED by lab spike, 2026-08-23.** In split mode the live
Argo CD `Application` is a **multi-source app** (chart repo as one source, values
repo via `ref: values` + `$values/...` as another) — unlike the lab's current
single-source specs (`lab/gitops/argocd/apps/shop-api.yaml`). Upstream Argo CD
documents that `argocd app diff --local` does not support multi-source applications,
so `_argocd_diff`'s `--local <repo_path>/apps/<app>` call (`validator.py:294-305`)
cannot simply be re-pointed. **Decided mechanism: option 1 below, confirmed working
on the pinned CLI/server — no fallback needed.**

Spike setup: a real multi-source `Application` (`lab/gitops/argocd/apps/shop-api-split.yaml`,
new `lab:m11-spike-chart-repo` Taskfile target seeding a second gitea repo,
`kubemend/shop-api-chart`, holding just the chart) synced clean on the first try —
server `quay.io/argoproj/argocd:v2.13.2`, CLI `v2.13.1`, both well above the 2.6/2.8
minimum for multi-source support. A run branch was pushed to the values repo with
`replicaCount: 2 → 3` (simulating a `Proposer` commit), then:

```
argocd app diff shop-api-split \
  --revisions kubemend/m11-spike-test --source-positions 2 \
  --server localhost:8080 --auth-token "$(cat .lab/argocd-token)" --plaintext
```

produced exactly the expected diff (`< replicas: 2` / `> replicas: 3`) against the
live Deployment, exit code 1 (the existing "1 = differences exist, that's success"
convention `_argocd_diff` already relies on for single-source). The live app was
unaffected — `argocd app diff` is read-only regardless of source count — confirmed by
re-checking `spec.replicas` (still 2) and sync/health status (still `Synced Healthy`)
immediately after.

1. **`argocd app diff --revisions <run-branch> --source-positions <n>`** — diffs a
   pushed revision of the values source (source position `2`, 1-indexed, matching the
   `sources` list order in the Application spec) against live, leaving the chart
   source at its default revision. **Confirmed working.** This is the split-mode diff
   mechanism `_argocd_diff` implements. Works only when the run branch is actually on
   the forge — holds for `GiteaBackend` (and any real deployment) but not
   `LocalGitBackend`; split mode is therefore forge-backend-only, stated explicitly as
   a deployment requirement rather than silently unsupported.
2. `kubectl diff --server-side` — **not needed.** Option 1 worked cleanly; this
   fallback (and its RBAC cost — dry-run apply is authorized like a real write, so the
   read-only ServiceAccount would need a scoped grant) is dropped from the design
   rather than carried as unused code.
3. Fail-closed-on-both-disappointing — moot, since option 1 succeeded.

Single-repo mode keeps today's `--local` path untouched — nothing about this changes it.

**Spike artifacts left in the lab** (not spike-only scaffolding — this is the same
infra Phase 5's acceptance scenario needs): the `shop-api-chart` gitea repo, the
`shop-api-split` Application (namespace `shop-split`, isolated from the single-source
`shop-api` demo), and the `lab:m11-spike-chart-repo` Taskfile target. The scratch run
branch (`kubemend/m11-spike-test`) was deleted after the spike; it was throwaway,
unlike the app/chart-repo infra.

**Scope (`_scope`) and quota (`_quota`)** — no change. Both consume parsed diff output
and live cluster state; neither touches a repo path.

## 7. Deployment: checkout pattern for the chart

The chart keeps its stance of never cloning and never holding git credentials
(`charts/kubemend/README.md` §"GitOps repo checkout and credentials"). Changes:

- `charts/kubemend/templates/job.yaml` mounts a second `emptyDir` named
  `chart-workspace` at `/workspace-charts`, unconditionally (an empty dir is free, and
  an always-present mount keeps the README example copy-pasteable). `/workspace`
  remains the values repo. Clones do **not** go inside `/workspace` — a nested
  repo would sit as an untracked dir in the checkout the proposer commits from.
- The in-cluster config sets `gitops.chart_repos.checkout_root: /workspace-charts`;
  init containers clone each chart repo to `/workspace-charts/<app>`.

**Union vs. lazy, with a recommendation.** The options from the milestone entry:

- *Union*: static `job.extraInitContainers` clone every configured chart repo at Job
  start.
- *Lazy*: clone only the incident's app's chart repo — requires the app name at init
  time, so either the operator templates per-alert init containers or a clone script
  reads the mounted config plus an app env var.

**Recommendation: union, for M11.** Reasoning: (1) the scale at which union cloning
hurts is the scale at which M11's explicit routing map is already the wrong tool
(§3) — the two limits coincide, so lazy cloning buys headroom M11 cannot use;
(2) `--depth 1 --single-branch` clones of chart-sized repos cost seconds of Job
startup against a run whose wall budget is 600s; (3) union keeps the clone list
static, reviewable YAML with zero logic in it, whereas lazy puts routing knowledge in
a *second* place (an init-container script) that can drift from `chart_repos`; (4) the
harness fail-fasts at wiring if `scope.app`'s checkout is missing (§3), so a stale
union list surfaces immediately and legibly. Lazy cloning is the natural companion to
a future convention-routed large fleet, and nothing here obstructs it: laziness is
purely a deployment-side choice — the code only ever looks for `checkout_root/<app>`
and cannot tell how it got there. Record it as the expected follow-up when
`url_template`-scale fleets materialize.

The chart README's checkout section gains a split-mode example (values repo →
`/workspace`, N chart clones → `/workspace-charts/<app>`), and `docs/getting-started.md`
step 3 ("what shape is your GitOps repo") gains the split-mode branch it currently
defers "until M11/M12 land".

## 8. Wiring: what changes in `cli.py`, and the rule-7 check

All assembly stays in `cli.py`'s factory layer:

- `execute_incident` (`cli.py:192-197`): in split mode, resolve the route for
  `task.scope.app`, run the origin/existence checks, build the second `GitOpsReader`,
  and hand the reader mapping to the two read-tool spec factories.
- `build_write_path` (`cli.py:146`): pass `chart_dirs={app: <resolved dir>}` to
  `Validator`. `Proposer` construction is untouched.

`kubemend/core/loop.py` is not modified; neither is anything else under
`kubemend/core/` (`model.py`'s `Scope`/`Task` already carry everything routing needs).
The loop keeps seeing an opaque `ToolRegistry` and an argument-free gate. Per the
design brief's tripwire: at no point did this design want repo-selection in the loop —
selection happens once per run at assembly time, which is earlier than the loop even
starts.

## 9. What M12 will stand on (and what M11 refuses to decide)

- The section is named `chart_repos`, not `repos`: M12 adds a sibling
  (`values_repos` or a combined per-app route — its call), no collision, no rename.
- M12's write routing has an obvious seam: `build_write_path` already chooses backend,
  proposer, and validator per run; choosing them *per route* is the same factory-level
  move this design used for the chart reader, with `Proposer` still holding exactly
  one backend per run. Nothing in M11 hardcodes "the values repo is `gitops.repo_path`"
  anywhere *except* that factory.
- The reader's `repo` enum stays two-valued in M12 — `"values"` simply resolves to the
  routed values repo. The model's vocabulary doesn't grow with the fleet in either
  milestone.
- Deliberately not decided now: whether M12 routes by app, namespace, or team;
  whether `chart_repos.apps` and a values-repo map merge into one per-app route
  object. The plan says these are M12 design questions informed by M11 in practice.

## 10. Change inventory

| Area | File(s) | Change |
|---|---|---|
| Config | `kubemend/config.py` | add `ChartRepoSpec`, `ChartReposConfig`, `GitOpsConfig.chart_repos: ... | None = None`; mirror in `kubemend.yaml` comments |
| Routing | `kubemend/tools/gitops/routing.py` (new) | pure resolution + checkout validation (origin match, existence) |
| Reader | `kubemend/tools/gitops/reader.py` | `GitOpsReader` dataclass unchanged; spec factories take a reader mapping; schemas gain optional `repo` enum; chart reads rooted at `chart_path` |
| Proposer | `kubemend/tools/gitops/proposer.py` | **no change** |
| Backend | `kubemend/tools/gitops/backend.py` + impls | **no change** |
| Gate | `kubemend/verify/gate.py` | **no change** |
| Validator | `kubemend/tools/gitops/validator.py` | `chart_dirs` field + `_chart_dir()`; split-mode `_render` passes explicit `--values`; split-mode `_diff` per §6 spike outcome |
| Loop / model | `kubemend/core/*` | **no change** (rule 7 upheld) |
| Wiring | `kubemend/cli.py` | route resolution + second reader + `chart_dirs` in the two factories |
| Prompts | `kubemend/prompts/system.md.j2` | conditional split-mode paragraph |
| Chart | `charts/kubemend/templates/job.yaml`, `charts/kubemend/README.md` | `chart-workspace` emptyDir at `/workspace-charts`; split-mode clone example |
| Docs | `docs/knowledge/tool-contracts.md`, `docs/getting-started.md`, `ARCHITECTURE.md` §4 | `repo` param contract; split-mode setup; repo-model addendum |
| Lab | `lab/`, `docs/knowledge/lab-and-evals.md` | second gitea repo holding a chart; a multi-source Application; the M11 acceptance scenario |

## 11. Risks and lab-verification items (implementation gate)

1. **Argo multi-source diff** (§6) — **RESOLVED 2026-08-23.**
   `--revisions`/`--source-positions` confirmed working against a real multi-source
   Application on the pinned CLI/server (v2.13.1/v2.13.2); no kubectl fallback needed,
   no RBAC-grant deployment requirement to document.
2. **Helm cross-root render** — **RESOLVED 2026-08-23.** Confirmed by
   `tests/integration/test_helm_cross_root_render.py` (real pinned helm binary, chart
   dir and values file on unrelated `tmp_path` roots) — `helm template <dir> --values
   <other-dir/file>` renders correctly, no composed temp tree needed.
3. **Overlay rendering gap** — pre-existing, shared by both modes, filed as an issue,
   explicitly out of M11 scope (§6).
4. **Acceptance** (from the plan): a lab scenario where the incident app's chart lives
   in gitea repo A and values in repo B; the run must open a correct values-only draft
   PR against repo B, with the gate's full pipeline passing — including a real diff.
   The infra for this now exists (`shop-api-chart` repo, `shop-api-split` Application,
   §6) — Phase 5 wires the actual fault-injection scenario and checker against it.
