"""Command-line entrypoint (ARCHITECTURE.md §9).

Exposes `kubemend run | evals | trace replay`. `run` is read-only in M2: the
three read tools are registered, the gitops tools are not, so the loop can only
investigate and hand off. That is a deliberate intermediate state — it exercises
the whole harness against a real cluster before the write path exists.
"""

from __future__ import annotations

import json
import textwrap
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import typer

from kubemend.config import RunConfig, load_config
from kubemend.core.loop import run as run_loop
from kubemend.core.model import RunResult, Scope, Task, Verdict
from kubemend.llm.client import LLMClient, LLMError
from kubemend.llm.factory import make_client
from kubemend.prompts import render
from kubemend.tools.gitops.backend import GitBackend
from kubemend.tools.gitops.gitea_backend import GiteaBackend
from kubemend.tools.gitops.local_backend import LocalGitBackend
from kubemend.tools.gitops.proposer import Proposer, propose_tool_spec
from kubemend.tools.gitops.reader import (
    GitOpsReader,
    list_gitops_files_spec,
    read_gitops_file_spec,
)
from kubemend.tools.gitops.validator import Validator
from kubemend.tools.kubernetes.factory import build_kube_client
from kubemend.tools.kubernetes.reader import KubernetesReader, k8s_tool_spec
from kubemend.tools.observability.loki import LokiProvider, logs_tool_spec
from kubemend.tools.observability.prometheus import PrometheusProvider, metrics_tool_spec
from kubemend.tools.registry import ToolRegistry
from kubemend.trace.recorder import TraceRecorder
from kubemend.trace.replay import replay as replay_events
from kubemend.verify.gate import PipelineGate, VerificationGate, validate_tool_spec

app = typer.Typer(
    name="kubemend",
    help="Diagnose Kubernetes incidents and propose GitOps fixes as draft PRs.",
    no_args_is_help=True,
)

trace_app = typer.Typer(help="Inspect the JSONL trace a run writes.", no_args_is_help=True)
app.add_typer(trace_app, name="trace")

# `evals/` is deliberately excluded from the built wheel (dev/eval-only, not
# part of what `pip install kubemend` or the container image ships — see
# pyproject.toml's [tool.hatch.build.targets.wheel] and docs/decisions.md).
# A real install must still work for `kubemend run`/`operator serve` without
# it; a dev checkout (evals/ importable via the repo root) gets the extra
# subcommand for free.
try:
    from evals.runner import evals_app
except ModuleNotFoundError:
    pass
else:
    app.add_typer(evals_app, name="evals")


class ReadOnlyGate:
    """Stands in for the verification gate until M3 builds the real one.

    It always fails, and that is correct rather than a placeholder cop-out: with
    no write path registered there is no proposal to verify, so a run can only
    legitimately end in a handoff. Returning `passed=True` here would fake
    exactly the trusted-self-report that I1 exists to prevent.
    """

    def verify(self) -> Verdict:
        from kubemend.core.model import CheckResult

        return Verdict(
            passed=False,
            checks=[
                CheckResult(
                    name="write_path",
                    passed=False,
                    detail=(
                        "No GitOps tools are registered in this build, so no change "
                        "was proposed and there is nothing to verify. Summarise your "
                        "findings instead."
                    ),
                )
            ],
        )


def build_read_only_registry(cfg: RunConfig) -> ToolRegistry:
    prometheus = PrometheusProvider(cfg.observability.prometheus_url)
    loki = LokiProvider(cfg.observability.loki_url)
    reader = KubernetesReader(build_kube_client(cfg.kubernetes))
    return ToolRegistry(
        [
            metrics_tool_spec(prometheus),
            logs_tool_spec(loki),
            k8s_tool_spec(reader),
        ],
        result_token_cap=cfg.context.result_token_cap,
        retry_backoff_s=0.5,
    )


def build_write_path(
    cfg: RunConfig, scope: Scope, run_id: str, lab_bin: Path
) -> tuple[Proposer, PipelineGate]:
    """Assemble the proposer, validator and gate for a run that may write.

    The binaries come from the Taskfile-managed directory rather than PATH: a
    developer machine here had a helm a full major version ahead of the pinned
    one, which would render different manifests than CI does.
    """
    backend: GitBackend = LocalGitBackend(cfg.gitops.repo_path)
    if cfg.gitops.backend == "gitea":
        token_file = Path(cfg.gitops.gitea_token_file).expanduser()
        if not token_file.exists():
            raise typer.BadParameter(
                f"gitops.backend is 'gitea' but no token at {token_file} — run `task lab:workspace`"
            )
        backend = GiteaBackend(
            cfg.gitops.repo_path,
            api_url=cfg.gitops.gitea_api_url,
            owner=cfg.gitops.gitea_owner,
            repo=cfg.gitops.gitea_repo,
            token=token_file.read_text().strip(),
        )
    proposer = Proposer(
        backend=backend,
        writable_globs=list(cfg.gitops.writable_globs),
        base_branch=cfg.gitops.base_branch,
        run_id=run_id,
    )
    argocd_token_file = Path(cfg.argocd.token_file).expanduser()
    validator = Validator(
        repo_path=Path(cfg.gitops.repo_path).expanduser().resolve(),
        scope=scope,
        helm_bin=lab_bin / "helm",
        kyverno_bin=lab_bin / "kyverno",
        kubectl_bin=lab_bin / "kubectl",
        policies_dir=cfg.policies_dir.resolve(),
        argocd_bin=lab_bin / "argocd",
        argocd_server=cfg.argocd.server,
        argocd_token=(argocd_token_file.read_text().strip() if argocd_token_file.is_file() else ""),
        argocd_plaintext=cfg.argocd.plaintext,
        # Same read-only identity the agent's own get_k8s_state tool uses —
        # the quota check only lists/gets, so it needs nothing more.
        kube=build_kube_client(cfg.kubernetes),
    )
    return proposer, PipelineGate(proposer=proposer, validator=validator)


def execute_incident(
    cfg: RunConfig,
    task: Task,
    run_id: str,
    *,
    llm: LLMClient,
    read_only: bool,
    lab_bin: Path = Path(".lab/bin"),
    trace_dir: Path = Path("traces"),
) -> RunResult:
    """Assemble tools + gate for one incident and drive it through the loop.

    Shared by `kubemend run` and the eval runner, so a sweep exercises exactly
    the wiring a human invocation does rather than a second code path that can
    drift from it. Workspace-existence policy (warn-and-degrade vs. fail fast)
    is deliberately the caller's decision, not this function's — the CLI and a
    cost-spending sweep want different answers to "no gitops workspace".
    """
    trace = TraceRecorder.open(trace_dir / f"{run_id}.jsonl")
    registry = build_read_only_registry(cfg)
    gate: VerificationGate = ReadOnlyGate()
    proposer: Proposer | None = None

    if not read_only:
        proposer, gate = build_write_path(cfg, task.scope, run_id, lab_bin.resolve())
        # Reads are registered with the write path, not with the read-only
        # tools: without a proposer there is nothing to write and no reason to
        # put chart internals into context.
        reader = GitOpsReader(
            Path(cfg.gitops.repo_path).expanduser().resolve(),
            base_branch=cfg.gitops.base_branch,
        )
        registry.register(read_gitops_file_spec(reader))
        registry.register(list_gitops_files_spec(reader))
        registry.register(propose_tool_spec(proposer))
        registry.register(validate_tool_spec(gate))

    result = run_loop(task, cfg, llm=llm, registry=registry, gate=gate, trace=trace)
    if result.success and proposer is not None:
        result.pr_ref = _open_pr(proposer, task, result, cfg)
    return result


def resolve_model_tier(cfg: RunConfig, model: str) -> RunConfig:
    """Point the main tier at the cheap model rather than teaching the loop
    about a third tier: the loop asks for "main" for agent turns and "cheap"
    for compaction and handoff, and that split stays meaningful. Same
    vocabulary the eval runner uses (`--model cheap|main`).
    """
    if model == "cheap":
        cfg.model.main = cfg.model.cheap.model_copy(
            update={"max_cost_usd_per_run": cfg.model.main.max_cost_usd_per_run}
        )
    elif model != "main":
        raise ValueError(f"--model must be 'main' or 'cheap', got {model!r}")
    return cfg


def _credential_hint(cfg: RunConfig) -> str:
    """Which env var (or AWS chain) applies depends on which provider each
    tier is actually configured for — no single hint fits every setup."""
    hints = {
        "anthropic": "ANTHROPIC_API_KEY, or `ant auth login` to store a profile",
        "openai": "OPENAI_API_KEY (or the key your OpenAI-compatible endpoint expects)",
        "bedrock": "the AWS credential chain (env vars, profile, or IMDS)",
    }
    providers = {cfg.model.main.provider, cfg.model.cheap.provider}
    return "Check: " + "; ".join(hints[p] for p in sorted(providers))


@app.command()
def run(
    task: Annotated[
        str, typer.Option("--task", help="What happened, as an on-call human would say it")
    ],
    namespace: Annotated[
        str, typer.Option("--namespace", help="Namespace the incident is scoped to")
    ],
    app_name: Annotated[str, typer.Option("--app", help="Application the incident is scoped to")],
    window: Annotated[str, typer.Option("--window", help="Time window of interest")] = "-30m",
    read_only: Annotated[
        bool, typer.Option("--read-only", help="Investigate only; register no write path")
    ] = False,
    model: Annotated[
        str, typer.Option("--model", help="Which tier drives agent turns: main | cheap")
    ] = "main",
    config: Annotated[Path, typer.Option("--config")] = Path("kubemend.yaml"),
    bin_dir: Annotated[
        Path,
        typer.Option(
            "--bin-dir",
            envvar="KUBEMEND_BIN_DIR",
            help="Directory holding pinned helm/kyverno/kubectl/argocd binaries",
        ),
    ] = Path(".lab/bin"),
) -> None:
    """Diagnose an incident and propose a fix (read-only until M3)."""
    cfg = load_config(config)
    try:
        cfg = resolve_model_tier(cfg, model)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    scope = Scope(namespace=namespace, app=app_name)
    incident = Task(statement=task, scope=scope, window=window)
    # Timestamp prefix makes traces/ and `kubemend/<run_id>` branches sortable
    # and identifiable at a glance; the hex suffix keeps same-second runs unique.
    run_id = f"{datetime.now(UTC):%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:8]}"

    workspace = Path(cfg.gitops.repo_path).expanduser()
    effective_read_only = read_only
    if read_only:
        typer.echo("read-only: no write path registered")
    elif not (workspace / ".git").exists():
        # Refusing to guess is better than silently degrading: a run that
        # cannot propose looks identical in the report to one that chose not to.
        typer.echo(
            f"no GitOps workspace at {workspace} — running read-only. "
            "Run `task lab:workspace` to create one.",
            err=True,
        )
        effective_read_only = True

    # Deliberately no API-key precondition here. Each provider's SDK resolves
    # its own credentials (env var, profile on disk, or the AWS chain for
    # Bedrock), so an unset env var does not mean there are no credentials.
    # Let the SDK decide and translate its refusal into something actionable.
    try:
        llm = make_client(cfg)
    except LLMError as exc:
        typer.echo(f"could not construct an LLM client: {exc}\n{_credential_hint(cfg)}", err=True)
        raise typer.Exit(code=1) from exc

    result = execute_incident(
        cfg, incident, run_id, llm=llm, read_only=effective_read_only, lab_bin=bin_dir
    )
    _report(result, model_name=cfg.model.main.name)


def _open_pr(proposer: Proposer, incident: Task, result: RunResult, cfg: RunConfig) -> str | None:
    """Turn a verified proposal into something a human can review.

    The body is generated from the gate's own verdict rather than from anything
    the model said about its work — that check table is the reviewer's evidence.
    """
    verdict = result.verdict
    body = render(
        "pr_body.md.j2",
        rationale=proposer.rationale,
        files=proposer.files_written,
        checks=verdict.checks if verdict else [],
        resources=(verdict.diff_summary.resources if verdict and verdict.diff_summary else []),
        scope=incident.scope,
        writable_globs=list(cfg.gitops.writable_globs),
        incident_ref=proposer.incident_ref,
    )
    if proposer.rationale:
        summary = textwrap.shorten(proposer.rationale.splitlines()[0], width=72, placeholder="…")
        title = f"kubemend: {summary}"
    else:
        title = "kubemend: proposed fix"
    pr = proposer.open_pr(title, body)
    return pr.url if pr else None


def _report(result: RunResult, *, model_name: str = "") -> None:
    if model_name:
        typer.echo(f"\nmodel:      {model_name}")
    typer.echo(f"reason:     {result.reason}")
    typer.echo(f"iterations: {result.iterations}")
    typer.echo(f"cost:       ${result.cost_usd:.4f}")
    typer.echo(f"wall:       {result.wall_seconds:.1f}s")
    typer.echo(f"trace:      {result.trace_path}")
    if result.handoff is None:
        return
    typer.echo("\n--- handoff ---")
    for hypothesis in result.handoff.root_cause_hypotheses:
        typer.echo(f"  [{hypothesis.confidence:.0%}] {hypothesis.statement}")
        for evidence in hypothesis.evidence:
            typer.echo(f"        evidence: {evidence}")
    if result.handoff.what_was_ruled_out:
        typer.echo("  ruled out:")
        for item in result.handoff.what_was_ruled_out:
            typer.echo(f"    - {item}")
    if result.handoff.suggested_next_steps:
        typer.echo("  next steps:")
        for item in result.handoff.suggested_next_steps:
            typer.echo(f"    - {item}")
    if result.handoff.blocking_reason:
        typer.echo(f"  blocked by: {result.handoff.blocking_reason}")


@trace_app.command()
def replay(
    path: Annotated[Path, typer.Argument(help="Trace file, e.g. traces/<run_id>.jsonl")],
    as_json: Annotated[
        bool, typer.Option("--json", help="Print raw JSONL instead of a one-line summary")
    ] = False,
) -> None:
    """Reconstruct a run's event sequence from its trace.

    The summary view is for eyeballing what happened; --json is for piping
    into a new unit fixture or scenario, which is the point of the round-trip
    guarantee this format keeps (CLAUDE.md: extend replay + its test together).
    """
    events = replay_events(path)
    if not events:
        typer.echo(f"no events at {path}", err=True)
        raise typer.Exit(code=1)
    if as_json:
        for event in events:
            typer.echo(json.dumps(event, sort_keys=True))
        return
    for index, event in enumerate(events):
        typer.echo(f"[{index}] {_summarize_event(event)}")


def _summarize_event(event: dict[str, Any]) -> str:
    kind = event.get("type", "?")
    if kind == "run_header":
        return f"run_header task={event['task']!r} scope={event['namespace']}/{event['app']}"
    if kind == "model_turn":
        calls = ", ".join(c["name"] for c in event.get("tool_calls", []))
        return (
            f"model_turn tier={event['tier']} model={event['model']} "
            f"cost=${event['cost_usd']:.4f} calls=[{calls}]"
        )
    if kind == "tool_call":
        return f"tool_call {event['name']} ok={event['ok']} {event['duration_ms']}ms"
    if kind == "nudge":
        return f"nudge -> {event['name']}: {event['text'][:80]}"
    if kind == "verdict":
        names = [c["name"] for c in event["checks"]]
        return f"verdict passed={event['passed']} checks={names}"
    if kind == "handoff":
        return f"handoff blocked_by={event.get('blocking_reason')}"
    if kind == "result":
        return (
            f"result success={event['success']} reason={event['reason']} "
            f"cost=${event['cost_usd']:.4f} iters={event['iterations']}"
        )
    return str(kind)
