"""Configuration precedence (ARCHITECTURE.md §8).

The precedence test is the one that matters. The file used to be passed as
constructor arguments, and init arguments outrank environment variables in
pydantic-settings — so every KUBEMEND_* override was silently ignored for any
key kubemend.yaml happened to mention, which is nearly all of them. Nothing
failed; the override just did not apply, which is the worst way for a config
bug to behave.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kubemend.config import load_config

CONFIG = """\
budgets:
  max_iterations: 15
gitops:
  backend: local
  base_branch: main
observability:
  prometheus_url: http://localhost:9090
"""


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    target = tmp_path / "kubemend.yaml"
    target.write_text(CONFIG)
    return target


def test_file_values_are_applied(config_file: Path) -> None:
    cfg = load_config(config_file)

    assert cfg.gitops.backend == "local"
    assert cfg.budgets.max_iterations == 15


def test_env_overrides_a_key_the_file_also_sets(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point: the file must not win over the environment."""
    monkeypatch.setenv("KUBEMEND_GITOPS__BACKEND", "gitea")

    assert load_config(config_file).gitops.backend == "gitea"


def test_env_overrides_a_nested_scalar(config_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KUBEMEND_BUDGETS__MAX_ITERATIONS", "3")

    assert load_config(config_file).budgets.max_iterations == 3


def test_unset_keys_keep_their_file_value_when_a_sibling_is_overridden(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Overriding one key must not blank out the rest of its section."""
    monkeypatch.setenv("KUBEMEND_GITOPS__BACKEND", "gitea")

    cfg = load_config(config_file)

    assert cfg.gitops.backend == "gitea"
    assert cfg.gitops.base_branch == "main"


def test_missing_file_yields_defaults_and_still_reads_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`kubemend --help` and the unit suite must work in a bare checkout."""
    monkeypatch.setenv("KUBEMEND_GITOPS__BACKEND", "gitea")

    cfg = load_config(tmp_path / "absent.yaml")

    assert cfg.gitops.backend == "gitea"
    assert cfg.budgets.max_iterations == 15, "the field default"


def test_keys_absent_from_the_file_fall_back_to_defaults(config_file: Path) -> None:
    cfg = load_config(config_file)

    assert cfg.kubernetes.context == "kind-kubemend"
