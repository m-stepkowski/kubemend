"""Chart-repo routing for split mode (M11 design doc §3).

All three resolution-precedence cases, plus every fail-fast wiring-time
check — a missing checkout and an origin mismatch are meant to look
identical to a caller (both `ChartRouteError`), since neither is something a
run can recover from mid-flight.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from git import Repo

from kubemend.config import ChartReposConfig, ChartRepoSpec
from kubemend.tools.gitops.routing import ChartRouteError, resolve_chart_route


def _checkout(root: Path, app: str, origin_url: str) -> Path:
    checkout = root / app
    checkout.mkdir(parents=True)
    repo = Repo.init(checkout, initial_branch="main")
    repo.create_remote("origin", origin_url)
    return checkout


def test_a_relative_checkout_root_resolves_against_the_cwd_not_wherever_helm_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: found via a live acceptance-scenario run, not the fixtures
    above (all of which pass an already-absolute tmp_path). The validator
    invokes helm with cwd set to the *values* repo, so a checkout_root left
    relative (a real config, e.g. the committed default
    `.lab/chart-workspaces`, is written relative on purpose) resolved against
    the wrong directory and helm reported "path not found" for a checkout
    that was really there."""
    monkeypatch.chdir(tmp_path)
    _checkout(tmp_path / "chart-workspaces", "shop-api", "https://git.corp/shop-api.git")
    cfg = ChartReposConfig(
        checkout_root=Path("chart-workspaces"),
        apps={"shop-api": ChartRepoSpec(url="https://git.corp/shop-api.git")},
    )

    route = resolve_chart_route("shop-api", cfg)

    assert route.checkout_root.is_absolute()
    assert route.checkout_root == tmp_path / "chart-workspaces" / "shop-api"


def test_an_explicit_apps_entry_wins_over_the_template(tmp_path: Path) -> None:
    _checkout(tmp_path, "shop-api", "https://git.corp/legacy/shop-api.git")
    cfg = ChartReposConfig(
        checkout_root=tmp_path,
        url_template="https://git.corp/platform/{app}-chart.git",
        apps={
            "shop-api": ChartRepoSpec(
                url="https://git.corp/legacy/shop-api.git", chart_path="chart"
            )
        },
    )

    route = resolve_chart_route("shop-api", cfg)

    assert route.url == "https://git.corp/legacy/shop-api.git"
    assert route.chart_path == "chart"
    assert route.checkout_root == tmp_path / "shop-api"
    assert route.chart_dir == tmp_path / "shop-api" / "chart"


def test_falls_back_to_the_url_template_when_no_explicit_entry(tmp_path: Path) -> None:
    _checkout(tmp_path, "shop-worker", "https://git.corp/platform/shop-worker-chart.git")
    cfg = ChartReposConfig(
        checkout_root=tmp_path,
        url_template="https://git.corp/platform/{app}-chart.git",
        template_chart_path=".",
    )

    route = resolve_chart_route("shop-worker", cfg)

    assert route.url == "https://git.corp/platform/shop-worker-chart.git"
    assert route.chart_path == "."
    assert route.base_branch == "main"


def test_no_route_at_all_raises_a_useful_error(tmp_path: Path) -> None:
    cfg = ChartReposConfig(checkout_root=tmp_path)

    with pytest.raises(ChartRouteError, match="no chart repo route exists for app 'shop-api'"):
        resolve_chart_route("shop-api", cfg)


def test_missing_checkout_raises_before_touching_anything_else(tmp_path: Path) -> None:
    cfg = ChartReposConfig(
        checkout_root=tmp_path,
        apps={"shop-api": ChartRepoSpec(url="https://git.corp/shop-api.git")},
    )

    with pytest.raises(ChartRouteError, match="no chart checkout at"):
        resolve_chart_route("shop-api", cfg)


def test_origin_mismatch_is_the_same_error_class_as_a_missing_checkout(tmp_path: Path) -> None:
    _checkout(tmp_path, "shop-api", "https://git.corp/wrong-repo.git")
    cfg = ChartReposConfig(
        checkout_root=tmp_path,
        apps={"shop-api": ChartRepoSpec(url="https://git.corp/shop-api.git")},
    )

    with pytest.raises(ChartRouteError, match="has origin 'https://git\\.corp/wrong-repo\\.git'"):
        resolve_chart_route("shop-api", cfg)


def test_resolution_never_touches_the_filesystem_before_checking_the_route(tmp_path: Path) -> None:
    """A route lookup miss must fail on the route itself, not surface as a
    confusing "no checkout" error for a path that was never going to matter."""
    cfg = ChartReposConfig(checkout_root=tmp_path / "does-not-exist")

    with pytest.raises(ChartRouteError, match="no chart repo route exists"):
        resolve_chart_route("shop-api", cfg)
