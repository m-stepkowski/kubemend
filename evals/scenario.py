"""Scenario loader (ARCHITECTURE.md §7, docs/knowledge/lab-and-evals.md).

Reads lab/scenarios/<name>/scenario.yaml and dynamically imports its
checker.py, so adding a scenario is a directory addition, never a code change
here.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Callable
from pathlib import Path

import yaml

from evals.lab import LabHandle
from evals.models import CheckReport, ScenarioSpec, SymptomProbe
from kubemend.core.model import RunResult, Scope

SCENARIOS_ROOT = Path("lab/scenarios")

CheckerFn = Callable[[RunResult, LabHandle], CheckReport]


def load_scenario(name: str, root: Path = SCENARIOS_ROOT) -> tuple[ScenarioSpec, CheckerFn]:
    directory = root / name
    spec_path = directory / "scenario.yaml"
    if not spec_path.is_file():
        raise FileNotFoundError(f"no scenario.yaml at {spec_path}")
    data = yaml.safe_load(spec_path.read_text()) or {}

    scope_data = data["scope"]
    probe_data = data["symptom_probe"]
    spec = ScenarioSpec(
        name=name,
        title=data["title"],
        scope=Scope(namespace=scope_data["namespace"], app=scope_data["app"]),
        task_prompt=data["task_prompt"],
        expected_outcome=data["expected_outcome"],
        symptom_probe=SymptomProbe(
            kind=probe_data["kind"],
            value=str(probe_data["value"]),
            condition_type=str(probe_data.get("condition_type", "")),
            timeout_s=float(probe_data.get("timeout_s", 120.0)),
            poll_interval_s=float(probe_data.get("poll_interval_s", 3.0)),
        ),
        tags=list(data.get("tags", [])),
    )
    return spec, _load_checker(name, directory / "checker.py")


def _load_checker(name: str, checker_path: Path) -> CheckerFn:
    if not checker_path.is_file():
        raise FileNotFoundError(f"no checker.py at {checker_path}")
    module_spec = importlib.util.spec_from_file_location(
        f"kubemend_scenario_{name}_checker", checker_path
    )
    if module_spec is None or module_spec.loader is None:
        raise ImportError(f"could not load checker module at {checker_path}")
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    checker = getattr(module, "check", None)
    if checker is None:
        raise AttributeError(f"{checker_path} does not define check(result, lab)")
    checker_fn: CheckerFn = checker
    return checker_fn


def list_scenarios(root: Path = SCENARIOS_ROOT) -> list[str]:
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir() and (p / "scenario.yaml").is_file())
