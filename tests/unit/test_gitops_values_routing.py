"""Values-repo routing for multi-values mode (M12 design doc §4).

Both resolution-precedence cases and every fail-fast wiring-time check. The
one shape that has no chart-routing analogue gets the most attention: values
repos are N:1 with apps, so two apps sharing a repo must resolve to *one*
checkout — cloning the same repo twice is the bug this keying exists to
prevent (M12 §1).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from git import Repo

from kubemend.config import ValuesReposConfig, ValuesRepoSpec
from kubemend.tools.gitops.routing import ValuesRouteError, resolve_values_route

DEFAULT_GLOBS = ["apps/**/values*.yaml"]


def _checkout(root: Path, name: str, origin_url: str) -> Path:
    checkout = root / name
    checkout.mkdir(parents=True)
    repo = Repo.init(checkout, initial_branch="main")
    repo.create_remote("origin", origin_url)
    return checkout


def _cfg(tmp_path: Path, **kwargs: object) -> ValuesReposConfig:
    defaults: dict[str, object] = {
        "checkout_root": tmp_path,
        "repos": {"platform": ValuesRepoSpec(url="https://git.corp/platform/values.git")},
    }
    defaults.update(kwargs)
    return ValuesReposConfig(**defaults)  # type: ignore[arg-type]


# -- resolution precedence ------------------------------------------------


def test_an_explicit_apps_entry_resolves_to_its_named_repo(tmp_path: Path) -> None:
    _checkout(tmp_path, "platform", "https://git.corp/platform/values.git")
    cfg = _cfg(tmp_path, apps={"shop-api": "platform"})

    route = resolve_values_route("shop-api", cfg, default_writable_globs=DEFAULT_GLOBS)

    assert route.name == "platform"
    assert route.checkout_root == tmp_path / "platform"
    assert route.url == "https://git.corp/platform/values.git"


def test_the_default_repo_catches_an_app_with_no_explicit_entry(tmp_path: Path) -> None:
    _checkout(tmp_path, "platform", "https://git.corp/platform/values.git")
    cfg = _cfg(tmp_path, apps={"other-app": "platform"}, default="platform")

    route = resolve_values_route("shop-api", cfg, default_writable_globs=DEFAULT_GLOBS)

    assert route.name == "platform"


def test_an_explicit_entry_wins_over_the_default(tmp_path: Path) -> None:
    _checkout(tmp_path, "payments", "https://git.corp/payments/values.git")
    cfg = _cfg(
        tmp_path,
        repos={
            "platform": ValuesRepoSpec(url="https://git.corp/platform/values.git"),
            "payments": ValuesRepoSpec(url="https://git.corp/payments/values.git"),
        },
        apps={"checkout-api": "payments"},
        default="platform",
    )

    route = resolve_values_route("checkout-api", cfg, default_writable_globs=DEFAULT_GLOBS)

    assert route.name == "payments"


def test_two_apps_sharing_a_repo_resolve_to_the_same_single_checkout(tmp_path: Path) -> None:
    """The N:1 case that makes values repos differ from chart repos (M12 §1):
    keying checkouts by repo name, not by app, is what stops one shared team
    repo being cloned once per app in it."""
    _checkout(tmp_path, "platform", "https://git.corp/platform/values.git")
    cfg = _cfg(tmp_path, apps={"shop-api": "platform", "shop-worker": "platform"})

    api = resolve_values_route("shop-api", cfg, default_writable_globs=DEFAULT_GLOBS)
    worker = resolve_values_route("shop-worker", cfg, default_writable_globs=DEFAULT_GLOBS)

    assert api.checkout_root == worker.checkout_root
    assert "shop-api" not in str(api.checkout_root), "checkouts are keyed by repo, not app"


# -- writable_globs fallback ----------------------------------------------


def test_a_repo_without_writable_globs_inherits_the_global_default(tmp_path: Path) -> None:
    _checkout(tmp_path, "platform", "https://git.corp/platform/values.git")
    cfg = _cfg(tmp_path, apps={"shop-api": "platform"})

    route = resolve_values_route("shop-api", cfg, default_writable_globs=DEFAULT_GLOBS)

    assert route.writable_globs == DEFAULT_GLOBS


def test_a_repo_with_its_own_writable_globs_overrides_the_default(tmp_path: Path) -> None:
    """Two teams' repos may genuinely differ in layout — that is why the field
    is per-repo rather than only global."""
    _checkout(tmp_path, "payments", "https://git.corp/payments/values.git")
    cfg = _cfg(
        tmp_path,
        repos={
            "payments": ValuesRepoSpec(
                url="https://git.corp/payments/values.git",
                writable_globs=["environments/**/values*.yaml"],
            )
        },
        apps={"checkout-api": "payments"},
    )

    route = resolve_values_route("checkout-api", cfg, default_writable_globs=DEFAULT_GLOBS)

    assert route.writable_globs == ["environments/**/values*.yaml"]


def test_the_returned_globs_are_a_copy_the_caller_cannot_mutate_config_through(
    tmp_path: Path,
) -> None:
    _checkout(tmp_path, "platform", "https://git.corp/platform/values.git")
    cfg = _cfg(tmp_path, apps={"shop-api": "platform"})
    globs = ["apps/**/values*.yaml"]

    route = resolve_values_route("shop-api", cfg, default_writable_globs=globs)
    route.writable_globs.append("**/*")

    assert globs == ["apps/**/values*.yaml"], "the path policy must not be aliased into config"


# -- fail-fast wiring-time checks -----------------------------------------


def test_an_app_with_no_route_and_no_default_names_the_config_to_add(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)

    with pytest.raises(ValuesRouteError) as exc:
        resolve_values_route("shop-api", cfg, default_writable_globs=DEFAULT_GLOBS)

    assert "gitops.values_repos.apps.shop-api" in str(exc.value)
    assert "gitops.values_repos.default" in str(exc.value)


def test_a_dangling_repo_name_names_both_the_app_and_the_missing_repo(tmp_path: Path) -> None:
    """A typo'd repo name is invisible otherwise — the app resolves fine, then
    nothing is there."""
    cfg = _cfg(tmp_path, apps={"shop-api": "platfrom"})

    with pytest.raises(ValuesRouteError) as exc:
        resolve_values_route("shop-api", cfg, default_writable_globs=DEFAULT_GLOBS)

    assert "shop-api" in str(exc.value)
    assert "platfrom" in str(exc.value)
    assert "platform" in str(exc.value), "the message should list what is actually defined"


def test_a_missing_checkout_fails_before_the_run_starts(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, apps={"shop-api": "platform"})

    with pytest.raises(ValuesRouteError, match="no values repo checkout"):
        resolve_values_route("shop-api", cfg, default_writable_globs=DEFAULT_GLOBS)


def test_a_checkout_of_the_wrong_repo_is_caught_by_the_origin_check(tmp_path: Path) -> None:
    """Right directory, wrong repo cloned into it — the bug class the url
    field exists to catch, shared with chart routing (M11 §2)."""
    _checkout(tmp_path, "platform", "https://git.corp/SOMETHING-ELSE/values.git")
    cfg = _cfg(tmp_path, apps={"shop-api": "platform"})

    with pytest.raises(ValuesRouteError) as exc:
        resolve_values_route("shop-api", cfg, default_writable_globs=DEFAULT_GLOBS)

    assert "SOMETHING-ELSE" in str(exc.value)
    assert "https://git.corp/platform/values.git" in str(exc.value)


def test_a_relative_checkout_root_is_resolved_absolute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same regression M11 shipped for chart routes: the validator runs helm
    with a cwd of its own, so a relative checkout_root resolves against the
    wrong directory entirely."""
    monkeypatch.chdir(tmp_path)
    _checkout(tmp_path / "values-workspaces", "platform", "https://git.corp/platform/values.git")
    cfg = ValuesReposConfig(
        checkout_root=Path("values-workspaces"),
        repos={"platform": ValuesRepoSpec(url="https://git.corp/platform/values.git")},
        apps={"shop-api": "platform"},
    )

    route = resolve_values_route("shop-api", cfg, default_writable_globs=DEFAULT_GLOBS)

    assert route.checkout_root.is_absolute()
    assert route.checkout_root == tmp_path / "values-workspaces" / "platform"


# -- forge coordinates ----------------------------------------------------


def test_gitea_coordinates_are_carried_through_when_set(tmp_path: Path) -> None:
    _checkout(tmp_path, "platform", "https://git.corp/platform/values.git")
    cfg = _cfg(
        tmp_path,
        repos={
            "platform": ValuesRepoSpec(
                url="https://git.corp/platform/values.git",
                gitea_owner="platform",
                gitea_repo="values",
            )
        },
        apps={"shop-api": "platform"},
    )

    route = resolve_values_route("shop-api", cfg, default_writable_globs=DEFAULT_GLOBS)

    assert (route.gitea_owner, route.gitea_repo) == ("platform", "values")


def test_gitea_coordinates_are_none_when_unset_rather_than_guessed_from_the_url(
    tmp_path: Path,
) -> None:
    """M12 §9 q1: explicit, never parsed off the URL — an unparsed URL fails
    loudly at wiring time, a mis-parsed one opens a PR against a real but
    wrong repo."""
    _checkout(tmp_path, "platform", "https://git.corp/platform/values.git")
    cfg = _cfg(tmp_path, apps={"shop-api": "platform"})

    route = resolve_values_route("shop-api", cfg, default_writable_globs=DEFAULT_GLOBS)

    assert route.gitea_owner is None
    assert route.gitea_repo is None
