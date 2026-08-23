"""Repo routing: `app -> chart repo` (M11 §2-3) and `app -> values repo` (M12 §4).

`resolve_chart_route`/`resolve_values_route` are the only places those two
resolutions happen. Both are pure functions called from `cli.py`'s factories at
wiring time, before the loop starts — never from `kubemend/core/`, and never
re-evaluated mid-run, since a run's `Scope` names exactly one app (M11 §1).
Routing never consults anything the model produced; it keys off
`Task.scope.app`, which the harness sets.

Each is called only when its config section is present (`chart_repos` /
`values_repos` not None) — the caller owns that check, so this module has no
single-repo-mode branch to keep in sync with anything.

The two are deliberately *not* one combined route table: chart repos are 1:1
with apps and values repos are N:1 (M12 §1), so they are keyed differently —
chart checkouts by app, values checkouts by repo name.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from git import InvalidGitRepositoryError, NoSuchPathError, Repo

from kubemend.config import ChartReposConfig, ChartRepoSpec, ValuesReposConfig


class RouteError(RuntimeError):
    """A repo route can't be resolved, or the checkout it resolves to isn't
    there or isn't the right repo. Always a wiring-time failure — the run
    never starts, so there is no mid-run recovery story to design for."""


class ChartRouteError(RouteError):
    """Split mode (M11) is configured but an app's chart repo can't be resolved."""


class ValuesRouteError(RouteError):
    """Multiple values repos (M12) are configured but an app's values repo
    can't be resolved."""


@dataclass(frozen=True)
class ChartRoute:
    # Where the app's chart checkout's .git lives — checkout_root / app. This
    # is the root a GitOpsReader over the chart repo resolves reads against.
    checkout_root: Path
    # The chart's directory *within* that checkout, relative — "." for a repo
    # whose root is the chart itself. Read-side path prefixing and the
    # validator's render both use this, joined onto checkout_root differently
    # (a git-relative prefix vs. a real filesystem path), hence kept separate
    # rather than pre-joined.
    chart_path: str
    base_branch: str
    url: str

    @property
    def chart_dir(self) -> Path:
        """The real filesystem directory holding Chart.yaml/templates/ — what
        the validator's `helm template` needs."""
        return self.checkout_root / self.chart_path


def resolve_chart_route(app: str, cfg: ChartReposConfig) -> ChartRoute:
    """Resolve `app`'s chart repo (design doc §3's three-step precedence),
    then validate its checkout actually exists and is the right repo.

    1. `cfg.apps[app]` if present — an explicit entry always wins.
    2. Else `cfg.url_template` with `{app}` substituted, `cfg.template_chart_path`,
       base branch "main".
    3. Else a `ChartRouteError` naming exactly what config to add.

    A missing or origin-mismatched checkout is the same class of error as a
    missing route: all three are wiring-time failures with no story for
    catching them mid-run, so there's exactly one exception type.
    """
    spec = cfg.apps.get(app)
    if spec is None:
        if cfg.url_template is None:
            raise ChartRouteError(
                f"split mode is configured but no chart repo route exists for app "
                f"{app!r}; add gitops.chart_repos.apps.{app} or set "
                "gitops.chart_repos.url_template"
            )
        spec = ChartRepoSpec(
            url=cfg.url_template.format(app=app),
            chart_path=cfg.template_chart_path,
            base_branch="main",
        )

    # Resolved, not left relative: the validator invokes helm with cwd set to
    # the *values* repo (validator.py's _render), so a relative checkout_root
    # would resolve against the wrong directory entirely — found via a live
    # acceptance-scenario run, not by the unit tests, which all happened to
    # use already-absolute tmp_path checkouts. Same convention cli.py already
    # applies to gitops.repo_path.
    checkout_root = Path(cfg.checkout_root).expanduser().resolve() / app
    _check_checkout(checkout_root, spec.url, app, kind="chart", error=ChartRouteError)
    return ChartRoute(
        checkout_root=checkout_root,
        chart_path=spec.chart_path,
        base_branch=spec.base_branch,
        url=spec.url,
    )


def _check_checkout(
    checkout_root: Path,
    url: str,
    subject: str,
    *,
    kind: str,
    error: type[RouteError],
) -> None:
    """Fail-fast validation (M11 design doc §2, "why url is in config"): catches
    the routing bug class — right checkout directory, wrong repo cloned into
    it — before any model tokens are spent, not partway through a render.

    Shared by both route kinds (M12 §4): the failure mode is identical, only
    the noun differs. `subject` is whatever the caller keys checkouts by — an
    app for chart repos, a repo name for values repos.
    """
    try:
        origin = Repo(checkout_root).remotes.origin.url
    except (InvalidGitRepositoryError, NoSuchPathError) as exc:
        raise error(
            f"no {kind} checkout at {checkout_root} for {subject!r} (route: {url!r}) — "
            "the Job's init containers must clone it there before the run starts"
        ) from exc
    if origin != url:
        raise error(
            f"{kind} checkout at {checkout_root} for {subject!r} has origin {origin!r}, "
            f"expected {url!r} — wrong repo cloned into this checkout directory"
        )


# -- values repos (M12) --------------------------------------------------


@dataclass(frozen=True)
class ValuesRoute:
    """Which values repo this run writes to (M12 design doc §4).

    Carries everything the write path needs that used to come straight off
    `GitOpsConfig`: the checkout, the branch to propose against, the path
    policy, and the forge coordinates for the PR call.
    """

    # The repo's config key. Also its checkout directory name — repos are
    # cloned once by name, not once per app that maps to them.
    name: str
    checkout_root: Path
    base_branch: str
    url: str
    writable_globs: list[str]
    # Where an app's directory sits in this repo — the validator formats it
    # per app to find that app's values.yaml.
    app_dir_template: str
    # None when backend == "local", which never opens a real PR.
    gitea_owner: str | None
    gitea_repo: str | None


def resolve_values_route(
    app: str, cfg: ValuesReposConfig, *, default_writable_globs: list[str]
) -> ValuesRoute:
    """Resolve which values repo `app`'s values live in (design doc §4).

    1. `cfg.apps[app]` — an explicit entry always wins.
    2. Else `cfg.default`.
    3. Else a `ValuesRouteError` naming exactly what config to add.

    A name that resolves to no `cfg.repos` entry is a dangling reference, not
    a missing route: it gets its own message naming both halves, because the
    config typo it comes from is invisible otherwise.
    """
    name = cfg.apps.get(app) or cfg.default
    if name is None:
        raise ValuesRouteError(
            f"multiple values repos are configured but no route exists for app {app!r}; "
            f"add gitops.values_repos.apps.{app} or set gitops.values_repos.default"
        )
    spec = cfg.repos.get(name)
    if spec is None:
        raise ValuesRouteError(
            f"app {app!r} routes to values repo {name!r}, which is not defined in "
            f"gitops.values_repos.repos (defined: {sorted(cfg.repos)})"
        )

    # Absolute, for the same reason chart routes are — see the comment in
    # resolve_chart_route; the validator runs helm with a cwd of its own.
    checkout_root = Path(cfg.checkout_root).expanduser().resolve() / name
    _check_checkout(checkout_root, spec.url, name, kind="values repo", error=ValuesRouteError)
    return ValuesRoute(
        name=name,
        checkout_root=checkout_root,
        base_branch=spec.base_branch,
        url=spec.url,
        writable_globs=list(
            spec.writable_globs if spec.writable_globs is not None else default_writable_globs
        ),
        app_dir_template=spec.app_dir_template,
        gitea_owner=spec.gitea_owner,
        gitea_repo=spec.gitea_repo,
    )
