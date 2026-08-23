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
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)


class ModelSpec(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    provider: Literal["anthropic", "openai", "bedrock"] = "anthropic"
    name: str
    max_cost_usd_per_run: float = 1.00
    # openai provider only; None resolves to api.openai.com. Setting this is
    # what turns "openai" into "OpenAI-compatible": DeepSeek, vLLM, Ollama,
    # or anything else speaking the /v1/chat/completions dialect.
    base_url: str | None = None
    # bedrock provider only; None defers to the AWS env/profile chain.
    aws_region: str | None = None
    # None falls back to context.model_window_tokens. Per-model rather than
    # global because window sizes vary a lot across providers (128k local
    # models vs. 200k+ hosted) — see harness-design.md on why the denominator
    # is configured, not auto-derived: a model swap must never silently
    # change when compaction fires.
    window_tokens: int | None = None


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
    provider: Literal["prometheus_loki", "datadog", "grafana_cloud"] = "prometheus_loki"
    prometheus_url: str = "http://localhost:9090"
    loki_url: str = "http://localhost:3100"

    # Only used when provider == "datadog". Keys are read from files rather
    # than held as plain strings, same idiom as gitea_token_file/argocd's
    # token_file, so a credential never lands in a committed file or a log line.
    datadog_site: str = "datadoghq.com"
    datadog_api_key_file: Path = Path(".lab/datadog-api-key")
    datadog_app_key_file: Path = Path(".lab/datadog-app-key")

    # Only used when provider == "grafana_cloud". Grafana Cloud's hosted
    # Mimir/Loki speak the same query APIs prometheus_loki does, so this
    # reuses PrometheusProvider/LokiProvider with HTTP Basic Auth added — the
    # instance ID as username, one Access Policy token (shared across both)
    # as password. URLs/instance IDs are account-specific with no sane
    # default, unlike Datadog's enumerable sites.
    grafana_cloud_prometheus_url: str = ""
    grafana_cloud_prometheus_instance_id: str = ""
    grafana_cloud_loki_url: str = ""
    grafana_cloud_loki_instance_id: str = ""
    grafana_cloud_token_file: Path = Path(".lab/grafana-cloud-token")


class KubernetesConfig(BaseModel):
    kubeconfig: Path = Path("~/.kube/kubemend-lab-readonly")
    context: str = "kind-kubemend"
    # True when running as a Job/Pod (M8a): auth comes from the projected
    # ServiceAccount token Kubernetes mounts automatically, and kubeconfig/
    # context are ignored entirely.
    in_cluster: bool = False


class ChartRepoSpec(BaseModel):
    """One app's chart repo, in split mode (M11 design doc §2)."""

    url: str
    chart_path: str = "."
    base_branch: str = "main"


class ChartReposConfig(BaseModel):
    """Split-mode chart routing (M11 design doc §2-3). Absent on `GitOpsConfig`
    (`chart_repos: None`) means single-repo mode; presence is the only mode
    discriminator kubemend has."""

    # Where the deployment's init containers clone each app's chart to:
    # checkout_root/<app>. In-cluster this is /workspace-charts; the relative
    # default matches local dev, same idiom as gitops.repo_path.
    checkout_root: Path = Path(".lab/chart-workspaces")
    # Convention route for fleets: "https://git.corp/charts/{app}.git". An
    # explicit `apps` entry for the same app overrides it.
    url_template: str | None = None
    template_chart_path: str = "."
    apps: dict[str, ChartRepoSpec] = Field(default_factory=dict)


class ValuesRepoSpec(BaseModel):
    """One *named* values repo, in multi-values mode (M12 design doc §3).

    Named, not app-keyed, because values repos are N:1 with apps (a per-team or
    per-environment repo holds many apps' values) while chart repos are 1:1 —
    see the M12 doc's §1 table for why that asymmetry drives the whole shape.
    """

    url: str
    base_branch: str = "main"
    # Per-repo: two teams' repos may genuinely differ in layout. None falls
    # back to GitOpsConfig.writable_globs, so the common case stays one setting.
    writable_globs: list[str] | None = None
    # Where an app's directory sits in this repo. The default is the layout
    # kubemend hardcoded before M12; a repo laid out per environment instead
    # sets e.g. "environments/prod/{app}".
    app_dir_template: str = "apps/{app}"

    @field_validator("app_dir_template")
    @classmethod
    def _template_must_vary_by_app(cls, value: str) -> str:
        # Without the placeholder every app resolves to one directory, and the
        # validator would render whichever app it saw against another app's
        # values — wrong, and silently so.
        if "{app}" not in value:
            raise ValueError(f"app_dir_template must contain '{{app}}'; got {value!r}")
        return value
    # Forge coordinates for the PR call, only used when backend == "gitea".
    # Explicit rather than parsed off `url` (M12 §9 q1): an unparsed URL fails
    # loudly at wiring time, a mis-parsed one opens a PR against a real but
    # wrong repo.
    gitea_owner: str | None = None
    gitea_repo: str | None = None


class ValuesReposConfig(BaseModel):
    """Multi-values-repo routing (M12 design doc §3-4). Absent on
    `GitOpsConfig` (`values_repos: None`) means the single values repo
    described by `repo_path`/`writable_globs`/`base_branch`/`gitea_*`."""

    # Init containers clone each *named* repo to checkout_root/<name> — keyed
    # by repo name, not app, so a repo shared by many apps is cloned once.
    # In-cluster this is /workspace-values.
    checkout_root: Path = Path(".lab/values-workspaces")
    repos: dict[str, ValuesRepoSpec] = Field(default_factory=dict)
    # app -> repo name. The N:1 mapping; the value must be a key of `repos`.
    apps: dict[str, str] = Field(default_factory=dict)
    # Catch-all for apps with no explicit entry — the realistic "most apps live
    # in the main repo, a few don't" shape. No `url_template` sibling here
    # (unlike chart_repos): with an N:1 mapping the grouping *is* the
    # information, so there is nothing to template.
    default: str | None = None


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

    # Only used in split mode (M11): repo_path/writable_globs/base_branch
    # above keep describing the central values repo unchanged — in split
    # mode that repo just no longer also holds the charts. None => today's
    # single-repo behavior, byte-for-byte; this is additive, not a migration.
    chart_repos: ChartReposConfig | None = None

    # Only used in multi-values mode (M12): the repo_path/writable_globs/
    # base_branch/gitea_* fields above describe the single values repo, and
    # this section overrides *which* repo those describe, per app. None =>
    # one values repo, exactly as above. Orthogonal to chart_repos: either,
    # both, or neither can be set.
    values_repos: ValuesReposConfig | None = None


class ArgoCdConfig(BaseModel):
    """Used by the verification gate's diff stage (ARCHITECTURE.md §5).

    Argo computes the diff under its own identity, so the agent process never
    needs a cluster credential that can write. `kubectl diff` cannot be the
    primary path: it is a dry-run apply, and Kubernetes authorizes dry-run
    exactly like a real write, so the read-only ServiceAccount is refused.
    """

    server: str = "localhost:8080"
    # The lab forwards Argo's HTTP port, so there is no TLS to verify.
    plaintext: bool = True
    token_file: Path = Path(".lab/argocd-token")


class OperatorConfig(BaseModel):
    """`kubemend operator serve` (M8b) — receives Alertmanager webhooks and
    creates the same kind of incident-response Job the manual
    `helm template ... | kubectl create` path does (docs/knowledge/
    operator-design.md). Disabled by default at the chart level.
    """

    port: int = 8080
    cooldown_seconds: float = 300.0
    # File, not an inline value — same idiom as gitea_token_file/argocd's
    # token_file, never held as a plain config string.
    webhook_token_file: Path = Path("/etc/kubemend/webhook-token")
    # Baked into the container image at a fixed path (Dockerfile); the
    # relative default matches a repo checkout for local dev/testing.
    chart_dir: Path = Path("charts/kubemend")
    # Rendered once at `helm install` time (templates/
    # operator-job-values-configmap.yaml) and mounted here — the operator
    # only ever adds the three per-alert dynamic fields on top of this.
    job_values_file: Path = Path("/etc/kubemend/operator-job-values.yaml")
    # The namespace to render/create Jobs into — the operator's own release
    # namespace, injected by the chart as an env var.
    namespace: str = "default"
    # The operator's own Helm release name, injected by the chart. Required
    # (not just cosmetic): templates/job.yaml references the config
    # ConfigMap by `{{ include "kubemend.fullname" . }}-config`, which
    # resolves to `.Release.Name`-config — `helm template` without a real
    # release name silently defaults to the literal "release-name", pointing
    # every spawned Job at a ConfigMap that doesn't exist.
    release_name: str = "kubemend"


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
    argocd: ArgoCdConfig = Field(default_factory=ArgoCdConfig)
    # The Kyverno policy pack the verification gate scans every proposal
    # against (§5). Relative default matches a repo checkout's layout; a
    # container image bakes its own copy in and overrides this via env, same
    # as model.pricing_table.
    policies_dir: Path = Path("policies")
    operator: OperatorConfig = Field(default_factory=OperatorConfig)


def load_config(path: Path | str = Path("kubemend.yaml")) -> RunConfig:
    """Read kubemend.yaml. A missing file yields defaults, not an error — the
    unit suite and `kubemend --help` must work in a bare checkout.

    The file is loaded as a *settings source* rather than as constructor
    arguments. Init arguments outrank environment variables in pydantic-settings,
    so passing the parsed YAML as kwargs silently disabled every KUBEMEND_*
    override for any key the file happened to mention — which is nearly all of
    them. Precedence here is env > file > defaults, which is what this module's
    docstring has always promised.
    """
    source = Path(path)
    if not source.exists():
        return RunConfig()

    class _FileBackedConfig(RunConfig):
        @classmethod
        def settings_customise_sources(
            cls,
            settings_cls: type[BaseSettings],
            init_settings: PydanticBaseSettingsSource,
            env_settings: PydanticBaseSettingsSource,
            dotenv_settings: PydanticBaseSettingsSource,
            file_secret_settings: PydanticBaseSettingsSource,
        ) -> tuple[PydanticBaseSettingsSource, ...]:
            return (
                init_settings,
                env_settings,
                YamlConfigSettingsSource(settings_cls, yaml_file=source),
                file_secret_settings,
            )

    return _FileBackedConfig()
