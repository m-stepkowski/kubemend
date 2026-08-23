# M12 — Multi-repo GitOps, phase B: multiple values repos

Status: **partially implemented**. Written 2026-08-23, after M11 shipped
(v0.8.0). Companion to `docs/design/m11-multi-repo-gitops.md`, which this doc
assumes you have read — it defines split mode, `ChartRoute`, `ReaderRoute`, and
the wiring seams M12 extends.

Done: config + routing (§3-4), write-path wiring (§6), per-repo layout
(§9 q2), token liveness (§8). Outstanding: lab fixtures and the acceptance
scenario (§10), chart/docs (§11), and §7's reason-string refinement.

Scope per `IMPLEMENTATION_PLAN.md` M12: route the *values* repo per app, not
just the chart repo. Plus one unrelated item bundled in by the plan: lab token
staleness (§8).

---

## 1. The asymmetry that shapes this design

M11's `chart_repos` and M12's values repos look symmetric and are not. The
difference decides the config shape, so it goes first:

| | chart repos (M11) | values repos (M12) |
|---|---|---|
| Cardinality vs. apps | **1:1** — each app has its own chart repo | **N:1** — many apps share one repo (per-team, per-environment) |
| Access | read-only | **the write target** |
| Needs forge coordinates | no | **yes** (a PR is opened against it) |
| Needs `writable_globs` | no | **yes**, and layouts may differ per repo |
| Checkout keyed by | app (`checkout_root/<app>`) | **repo name**, not app (§3) |

The 1:1-vs-N:1 row is the substantive one. M11 clones `checkout_root/<app>`
because an app's chart repo is that app's alone. Doing the same for values
would clone one shared team repo once per app in it — wasteful at 5 apps,
absurd at 50, and it invites two checkouts of the same repo drifting apart
mid-run. So values repos are **named**, apps map to a name, and the checkout is
`checkout_root/<repo-name>`.

That is why this is a sibling section rather than a merged
`(app) -> (chart repo, values repo)` route object, which M11 §9 left open. A
merged table would force the N:1 case into a 1:1 shape and re-introduce exactly
the duplicate-checkout problem. **Decision: two tables, keyed differently on
purpose.**

## 2. What does *not* change

- `propose_git_change` stays the only write-capable tool (CLAUDE.md hard rule
  3). It gains no sibling and no new argument. Routing happens at wiring time,
  above the tool.
- **One run writes to exactly one values repo.** A `Task`'s `Scope` names one
  app; one app maps to one values repo; `Proposer` holds one backend and one
  branch. This invariant is what keeps rule 3's spirit intact, and §7 makes the
  multi-repo-proposal case a named failure rather than a silent partial write.
- The reader's `repo` enum stays two-valued (`"values"` / `"chart"`). `"values"`
  simply resolves to the *routed* repo. The model's vocabulary does not grow
  with the fleet — as M11 §9 committed.
- `kubemend/core/` — zero changes. Same rule-7 check as M11.
- **Backward compatibility is structural**: `values_repos` absent ⇒ byte-for-byte
  today's behavior, with `gitops.repo_path` / `writable_globs` / `base_branch` /
  `gitea_owner` / `gitea_repo` continuing to describe the single values repo.
  Additive, not a migration — same promise M11 made and kept.

## 3. Config: the new `gitops.values_repos` section

```python
class ValuesRepoSpec(BaseModel):
    url: str
    base_branch: str = "main"
    # Per-repo, because two teams' repos may genuinely differ in layout.
    # Falls back to GitOpsConfig.writable_globs when unset.
    writable_globs: list[str] | None = None
    # Forge coordinates for the PR call. Only used when backend == "gitea".
    # Default to parsing them off `url`? See §9 open question 1.
    gitea_owner: str | None = None
    gitea_repo: str | None = None


class ValuesReposConfig(BaseModel):
    # Init containers clone each *named* repo to checkout_root/<name>.
    # In-cluster: /workspace-values.
    checkout_root: Path = Path(".lab/values-workspaces")
    repos: dict[str, ValuesRepoSpec] = Field(default_factory=dict)
    # app -> repo name. The N:1 mapping.
    apps: dict[str, str] = Field(default_factory=dict)
    # Optional catch-all for apps with no explicit entry.
    default: str | None = None


class GitOpsConfig(BaseModel):
    ...
    chart_repos: ChartReposConfig | None = None    # M11
    values_repos: ValuesReposConfig | None = None  # M12, None => single values repo
```

```yaml
gitops:
  backend: gitea
  values_repos:
    checkout_root: /workspace-values
    repos:
      platform:
        url: https://git.corp/platform/values.git
        gitea_owner: platform
        gitea_repo: values
      payments:
        url: https://git.corp/payments/values.git
        writable_globs: ["environments/**/values*.yaml"]   # different layout
        gitea_owner: payments
        gitea_repo: values
    apps:
      shop-api: platform
      shop-worker: platform      # N:1 — one checkout, two apps
      checkout-api: payments
    default: platform
```

**No `url_template` here**, deliberately, unlike `chart_repos`. A template is
what makes a 1:1 fleet mapping tractable (`charts/{app}.git`); for an N:1
mapping there is nothing to template — the interesting information *is* the
grouping. `default` covers the "most apps live in the main repo, a few don't"
case, which is the realistic shape.

## 4. Routing: `app -> values repo`

New `resolve_values_route(app, cfg) -> ValuesRoute` in the existing
`kubemend/tools/gitops/routing.py`, alongside `resolve_chart_route`. Same
discipline: pure function, called from `cli.py` at wiring time, never from
`core/`, never re-evaluated mid-run, keyed off `Task.scope.app` (harness-set,
never model-produced).

Resolution order:
1. `cfg.apps[app]` → a repo name; that name must exist in `cfg.repos`, else
   `ValuesRouteError` naming both the app and the dangling name.
2. Else `cfg.default` (same existence check).
3. Else `ValuesRouteError` naming exactly what config to add.

```python
@dataclass(frozen=True)
class ValuesRoute:
    name: str              # the repo's config key — also its checkout dir name
    checkout_root: Path    # checkout_root / name, absolute (see below)
    base_branch: str
    url: str
    writable_globs: list[str]
    gitea_owner: str | None
    gitea_repo: str | None
```

Reuse M11's `_check_checkout` verbatim (existence + `origin` URL match,
fail-fast at wiring time). And **resolve `checkout_root` to an absolute path** —
M11 shipped that bug (relative path resolved against helm's cwd, not the
caller's) and its regression test; do not re-introduce it here. Generalizing
`_check_checkout` to serve both route types is a small refactor and the right
one — it is the same failure mode (right directory, wrong repo cloned into it)
for both.

## 5. Read side

`ReaderRoute`'s `"values"` entry is constructed from `values_route.checkout_root`
instead of `cfg.gitops.repo_path`. Nothing else changes — no prefix (values
repos are read from their root, unlike a chart's `chart_path`), no schema
change, no new enum value. This is a one-line substitution at the `cli.py`
construction site.

## 6. Write side and validator

**`Proposer`** — no code change. It already takes `writable_globs` and
`base_branch` as constructor arguments; they now come from the route instead of
straight off `cfg.gitops`.

**Backends** — `LocalGitBackend(route.checkout_root)` and
`GiteaBackend(route.checkout_root, owner=route.gitea_owner, repo=route.gitea_repo, ...)`.
Both already take their repo path and coordinates as arguments. No change inside
either class; this is entirely a `build_write_path` change.

**Validator** — `repo_path=route.checkout_root`. The split-mode `--values` flag
(M11 §6) becomes `route.checkout_root / "apps" / app / "values.yaml"`… which
exposes a latent assumption: **that path is hardcoded to the `apps/<app>/`
layout**, and §3 just introduced per-repo layouts. See §9 open question 2 — this
is the one place M12 genuinely digs into validator logic rather than swapping a
path.

**Argo diff** — unchanged. `--revisions kubemend/<run_id> --source-positions 2`
is per-Application, and each Application still has exactly two sources (its
chart, its values). Which *repo* source 2 points at is Argo's business, not the
flag's. No new spike needed here — but confirm it, cheaply, as part of the
acceptance run rather than by assertion (§10).

## 7. The multi-repo proposal case — a named failure, not a partial write

An incident whose fix needs values changes in two different values repos cannot
be one PR, and must never become two half-applied ones. `Proposer` holds one
branch against one backend; a second repo has no branch and no PR.

**Decision:** this is a first-class structured outcome, not an exception. The
path policy already rejects paths outside `writable_globs`; with routing, a path
belonging to *another* repo simply is not writable in this one, and the existing
`path_not_writable` error already fires with a clear message. What M12 adds is a
better *reason string* when the path would have been writable in a different
configured repo — "that file lives in values repo `payments`, this run writes to
`platform`" — so the model gets a real diagnosis instead of a generic policy
rejection, and the handoff names it.

No new blocking_reason enum value unless implementation shows the existing
`fix_not_expressible_in_values` genuinely does not fit. Decide during
implementation, with the actual message in hand.

## 8. Lab token staleness (bundled per `IMPLEMENTATION_PLAN.md`)

Unrelated to routing; folded into M12 because M11's acceptance run tripped over
it twice.

Both `lab:workspace`'s inline gitea-token block and `lab:argocd-token` gate
regeneration on the token *file existing* (`[ ! -f ]` / `[ -s ]`), never on
whether it still authenticates. A `task lab:gitea` / `task lab:argocd` helm
upgrade rotates the underlying admin session, and the cached token then fails
mid-run with an opaque `invalid username, password or token` — observed once for
argocd and once for gitea during M11, both times costing a full debugging cycle
because the failure surfaces nowhere near its cause.

**Fix:** replace the existence check with a liveness check in both targets —
make one cheap authenticated API call with the stored token; regenerate on any
non-2xx rather than only on absence. Roughly:

```sh
if [ -s "$TOKEN_FILE" ] && curl -fsS -o /dev/null \
     -H "Authorization: token $(cat "$TOKEN_FILE")" \
     http://localhost:3000/api/v1/user 2>/dev/null; then
  echo "gitea token still valid"
else
  # ... regenerate ...
fi
```

Note this is *validation*, not a retry loop — CLAUDE.md's no-retries-for-flakes
rule is about masking nondeterminism; checking whether a credential works before
trusting it is the opposite of masking.

**Implemented 2026-08-23, and the endpoint choice turned out to be the whole
problem.** Both obvious probes are wrong, each in a different direction, and
both were caught only by feeding them a deliberately-garbage token — never by
reading the docs:

- `argocd account get-user-info --auth-token <garbage>` **exits 0** and prints
  `Logged In: false`. As a liveness check it validates nothing and would have
  shipped as a no-op. Use `argocd app list`, which genuinely round-trips the
  credential and exercises the same read the gate's diff stage needs.
- Gitea's `/api/v1/user` returns **403 for a perfectly valid token**, because
  the agent's token is minted with scopes `["write:repository"]` and has no
  user scope. As a liveness check it fails always, regenerating a fresh token
  on every single run. Use `/api/v1/repos/kubemend/gitops` — 200 for a valid
  token, 401 for a stale one, and within the scope the token actually holds.

Generalizable rule for any future credential check here: **probe the permission
the credential is actually for, and verify the probe against a known-bad
credential before trusting it.** A check that cannot fail is worse than no
check, because it looks like coverage.

One further detail: gitea rejects creating a token whose name duplicates an
existing one, and the superseded token is still there after a rotation — so the
regeneration path mints `kubemend-agent-<epoch>`, not a fixed name.

## 9. Open questions (resolve before or during implementation)

1. **Forge coordinates: explicit or parsed?** — **RESOLVED: explicit.** Shipped
   as `gitea_owner`/`gitea_repo` on `ValuesRepoSpec`, no URL parsing. One
   addition found while wiring: when `backend: gitea` and a routed repo has no
   coordinates, `build_write_path` now **fails at wiring time** rather than
   falling back to the top-level `gitops.gitea_owner`/`gitea_repo`. The fallback
   was the dangerous option — it would open the PR against a real repo, the
   wrong one, which is precisely what per-repo coordinates exist to prevent.
2. **The `apps/<app>/values.yaml` hardcode** — **RESOLVED, and the design
   understated it.** The hardcode was in *two* places, not one: split-mode
   render's `--values` flag, and `_chart_dir`'s single-repo branch
   (`repo_path / "apps" / app`), which this doc never mentioned. Option (a)
   (derive from `writable_globs`) is definitively wrong for the reason
   sketched — a glob describes a set, render needs exactly one file — so
   shipped as (b), but as `app_dir_template` (default `"apps/{app}"`) rather
   than a values-only path: in single-repo mode that one directory holds the
   chart *and* its values, so a single template governs both call sites and
   the two cannot drift apart. Rejects a template lacking `{app}` at config
   load: without the placeholder every app resolves to one directory and the
   validator would render one app against another's values, silently.
3. **Does `default` earn its keep?** Still open — decide once the acceptance
   scenario (§10) exists and shows whether it exercises the branch.
4. **Interaction with M11's `chart_repos.url_template`** when both sections are
   configured: no technical conflict (different tables, different keys), but the
   combined config is the first one a reader has to hold two routing models in
   their head for. May want one worked full example in `kubemend.yaml`'s
   comments rather than two partial ones.

## 10. Acceptance

Per the plan: a lab-provable case where the correct values repo, out of more
than one configured, receives the PR.

Concretely — extending M11's lab fixtures rather than replacing them:
- A second gitea values repo, and a second app whose values live in it while
  the first app's stay in the existing one.
- A scenario tagged `multi-values` (excluded from the default `-s all` sweep by
  the same `_scenarios_for_all` mechanism M11 added, for the same reason: it
  always fails against a config that lacks the routing).
- The run must open its PR against the *routed* repo, with the full gate
  pipeline green — and a second scenario, or an assertion within the first,
  proving the *other* repo received nothing.
- Confirm (don't assume) the Argo `--source-positions 2` convention still holds
  when source 2 is a different repo per Application (§6).

Plus, for §8: a lab where the gitea/argocd admin session has been rotated under
an existing token file yields working tokens with no manual `rm`.

## 10b. Acceptance diagnosis (required by CLAUDE.md — scenario below 50%)

First sweep 0/3, second 1/3. Written before any prompt or tool change, per the
rule in `docs/knowledge/lab-and-evals.md`.

**Routing itself is not implicated.** Every passing run took the direct path:
`read_gitops_file apps/checkout-api/values.yaml` → `propose_git_change` →
verified, PR against `gitops-payments`, `gitops` untouched. That is the M12
property, and it held whenever the model got that far.

Two of the three first-sweep failures were **defects in this fixture**, not the
product:

1. The checker asserted `gitops` held *no* `kubemend/*` branch at all. That repo
   accumulates them from every prior run; it was failing on branches left by the
   morning's M11 sweeps. Now scoped to this run's own branch, derived from
   `result.trace_path.stem` (traces are named `<run_id>.jsonl`, and the proposer's
   branch is `kubemend/<run_id>`).
2. `values.yaml` carried a seven-line prose comment. `propose_git_change` takes
   whole-file content, so every line is one the model must reproduce byte-for-byte
   to change one tag — and it twice emitted control characters at the same offset,
   failing `invalid_yaml`. Trimmed.

**The remaining failure mode is a real product weakness, and M12 is the first
fixture that could expose it.** Both `loop_detected` runs are identical:

```
read_gitops_file   apps/shop-payments/checkout-api/values.yaml   -> not_found
list_gitops_files  apps/shop-payments/checkout-api/**            -> {"paths": []}
list_gitops_files  apps/shop-payments/**/*.yaml                  -> {"paths": []}
```

The model invents a namespace segment, then re-lists under the *same wrong
prefix* until the loop detector fires. Two things combine:

- **Why here and not in the nine v0.1 scenarios**: there, namespace `shop` is a
  prefix of app `shop-api`, so `apps/shop-api/values.yaml` incidentally "looks
  like" it contains the namespace and a wrong guess lands right anyway. M12's
  fixture is the first with a namespace (`shop-payments`) and app
  (`checkout-api`) that share no substring — which is the *normal* case in a
  real fleet, not an exotic one.
- **Why it doesn't recover**: `list_gitops_files` answers an unmatched glob with
  `{"paths": []}`. That is a dead end carrying no signal that the *prefix* is
  wrong, so the model's next guess is anchored on the same bad assumption. This
  repo's own stated principle — the validator's "specificity is what makes the
  retry loop converge" — is exactly what the read side is missing here.

Fix: make an empty match informative (§10c). Deliberately **not** fixed by
renaming the fixture's namespace to share a prefix with its app: that would
destroy the very property that made the defect visible, and would be papering
over rather than fixing. Also not fixed by touching a prompt — the affordance is
the problem, not the wording.

## 10c. `list_gitops_files`: an empty match returns the repo's real layout

When a glob matches nothing, the result now carries the paths the repository
actually holds (capped), alongside the empty `paths` list. A wrong prefix
becomes self-correcting in one turn instead of an unbounded guess loop. Contract
change recorded in `docs/knowledge/tool-contracts.md` in the same commit, per
CLAUDE.md.

## 11. Change inventory (projected)

| Area | File(s) | Change |
|---|---|---|
| Config | `kubemend/config.py` | add `ValuesRepoSpec`, `ValuesReposConfig`, `GitOpsConfig.values_repos: ... \| None = None` |
| Routing | `kubemend/tools/gitops/routing.py` | add `ValuesRoute`, `resolve_values_route`; generalize `_check_checkout` to serve both route types |
| Reader | `kubemend/tools/gitops/reader.py` | **no change** — `"values"` reader built from a different path at the call site |
| Proposer | `kubemend/tools/gitops/proposer.py` | **no change** expected; possibly a better `path_not_writable` reason string (§7) |
| Backends | `local_backend.py`, `gitea_backend.py` | **no change** — both already parameterized by path/coordinates |
| Validator | `kubemend/tools/gitops/validator.py` | `repo_path` from route; resolve the `apps/<app>/values.yaml` hardcode (§9 q2) |
| Loop / model | `kubemend/core/*` | **no change** (rule 7) |
| Wiring | `kubemend/cli.py` | resolve values route; thread it into backend/proposer/validator/reader — the single factory M11 §9 predicted |
| Evals | `evals/runner.py` | `_build_lab`'s and `run`'s `gitops.repo_path` assumptions become route-aware; new `multi-values` tag exclusion |
| Chart | `charts/kubemend/templates/job.yaml`, `README.md` | `values-workspace` emptyDir at `/workspace-values`; N-clone example |
| Lab | `Taskfile.yaml`, `lab/`, `policies/` | second values repo + app + scenario; **token liveness checks (§8)** |
| Docs | `ARCHITECTURE.md` §4.3, `docs/getting-started.md`, `docs/knowledge/tool-contracts.md` | third repo shape; `repo` param contract unchanged but re-stated |

## 12. What M12 refuses to decide

- **Routing by namespace or team** rather than app. `Scope` is
  `(namespace, app)`; app-keyed matches M11 and the loop's own vocabulary.
  A team dimension is a config-authoring convenience that can be layered later
  without touching the resolution seam.
- **Cross-repo atomic proposals.** §7 makes this a named failure. Making it
  actually work needs multi-branch/multi-PR coordination and a story for
  partial failure — a milestone of its own, if ever.
- **Merging `chart_repos` and `values_repos` into one route table.** §1 explains
  why the cardinalities differ; revisit only if a third repo kind appears and
  the duplication becomes real rather than apparent.
