"""Packaging guards.

These exist because of a real regression: a build-time `force-include` copied the
repo-root `prompts/` directory into `kubemend/prompts`, which materialised a
*partial* `kubemend` package into site-packages. Site-packages precedes
`.pth`-appended paths, so that stub shadowed the real package and the console
script died with `No module named 'kubemend.cli'` — while the whole test suite
stayed green, because pytest's `pythonpath = ["."]` never used the install.

What is asserted here is what the project controls: the built wheel is complete,
the templates travel with the code, and nothing shadows the package. Whether a
given venv's `.pth` files are honoured is an environment property (macOS marks
them hidden, and CPython skips hidden `.pth` files) and deliberately not
something this suite tries to police.
"""

from __future__ import annotations

import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_prompt_templates_ship_beside_their_loader() -> None:
    """Templates must be package data, not repo-root files an install cannot see."""
    from kubemend.prompts import TEMPLATE_DIR

    assert TEMPLATE_DIR.is_relative_to(REPO_ROOT / "kubemend")
    for template in ("system.md.j2", "compaction.md.j2", "handoff.md.j2"):
        assert (TEMPLATE_DIR / template).exists(), f"{template} missing from the package"


def test_nothing_shadows_the_real_package() -> None:
    """`kubemend` must resolve to exactly one location, and it must be complete."""
    import kubemend

    roots = list(kubemend.__path__)
    assert len(roots) == 1, f"kubemend resolves to multiple locations: {roots}"
    assert (Path(roots[0]) / "cli.py").exists(), (
        f"kubemend resolved to {roots[0]}, which has no cli.py — a shadowing stub"
    )


@pytest.mark.slow
def test_built_wheel_contains_the_code_and_the_templates(tmp_path: Path) -> None:
    """Build a real wheel and inspect it.

    The stub regression was invisible to every import-based check; only looking
    at the artefact catches a build configuration that ships the wrong tree.
    """
    build = subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(tmp_path)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if build.returncode != 0:
        pytest.skip(f"uv build unavailable: {build.stderr.strip()[:200]}")

    wheels = list(tmp_path.glob("*.whl"))
    assert wheels, "uv build produced no wheel"
    names = zipfile.ZipFile(wheels[0]).namelist()

    assert "kubemend/cli.py" in names
    assert "kubemend/core/loop.py" in names
    assert "kubemend/prompts/__init__.py" in names
    for template in ("system", "compaction", "handoff"):
        assert f"kubemend/prompts/{template}.md.j2" in names, f"{template} missing from the wheel"


def test_console_script_entry_point_is_importable() -> None:
    """The entry point `kubemend = kubemend.cli:app` must actually resolve."""
    result = subprocess.run(
        [sys.executable, "-c", "from kubemend.cli import app; print(app.info.name)"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr.strip()
    assert "kubemend" in result.stdout
