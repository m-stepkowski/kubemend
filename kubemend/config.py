"""Configuration model for `kubemend.yaml` (ARCHITECTURE.md §8).

Loads the single config file with environment-variable overrides via
pydantic-settings: model tiers and pricing table, run budgets, context knobs,
observability endpoints, the read-only kubeconfig, and the GitOps backend with
its `writable_globs` path policy.

Every default here is duplicated in kubemend.yaml so the file stays readable as
documentation; the values in this module are what a run actually uses when a key
is absent.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ModelSpec(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    name: str
    max_cost_usd_per_run: float = 1.00


class ModelConfig(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    main: ModelSpec = ModelSpec(name="claude-sonnet-5", max_cost_usd_per_run=1.00)
    cheap: ModelSpec = ModelSpec(name="claude-haiku-4-5")
    pricing_table: Path = Path("config/pricing.yaml")


class BudgetConfig(BaseModel):
    """Defaults justified in docs/knowledge/harness-design.md."""

    max_iterations: int = 15
    max_wall_seconds: int = 600


class ContextConfig(BaseModel):
    result_token_cap: int = 6000
    compact_threshold: float = 0.70
    # Not in the §8 sketch, but compaction needs a denominator: the threshold is
    # a fraction *of the model window*, so the window has to be configurable
    # alongside the model that defines it.
    model_window_tokens: int = 200_000

    model_config = ConfigDict(protected_namespaces=())


class ObservabilityConfig(BaseModel):
    provider: Literal["prometheus_loki"] = "prometheus_loki"
    prometheus_url: str = "http://localhost:9090"
    loki_url: str = "http://localhost:3100"


class KubernetesConfig(BaseModel):
    kubeconfig: Path = Path("~/.kube/kubemend-lab-readonly")
    context: str = "kind-kubemend"


class GitOpsConfig(BaseModel):
    backend: Literal["local", "gitea"] = "local"
    # Inside .lab rather than a sibling directory: the workspace is generated,
    # disposable, and gitignored along with the credentials it is cloned with.
    repo_path: Path = Path(".lab/gitops-workspace")
    writable_globs: list[str] = Field(default_factory=lambda: ["apps/**/values*.yaml"])
    base_branch: str = "main"

    # Only used when backend == "gitea". The token is read from a file rather
    # than held in config so it never lands in a committed file or a log line.
    gitea_api_url: str = "http://localhost:3000/api/v1"
    gitea_owner: str = "kubemend"
    gitea_repo: str = "gitops"
    gitea_token_file: Path = Path(".lab/gitea-token")


class RunConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="KUBEMEND_",
        env_nested_delimiter="__",
        protected_namespaces=(),
    )

    model: ModelConfig = Field(default_factory=ModelConfig)
    budgets: BudgetConfig = Field(default_factory=BudgetConfig)
    context: ContextConfig = Field(default_factory=ContextConfig)
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)
    kubernetes: KubernetesConfig = Field(default_factory=KubernetesConfig)
    gitops: GitOpsConfig = Field(default_factory=GitOpsConfig)


def load_config(path: Path | str = Path("kubemend.yaml")) -> RunConfig:
    """Read kubemend.yaml. A missing file yields defaults, not an error — the
    unit suite and `kubemend --help` must work in a bare checkout.
    """
    source = Path(path)
    if not source.exists():
        return RunConfig()
    data: dict[str, Any] = yaml.safe_load(source.read_text()) or {}
    return RunConfig(**data)
