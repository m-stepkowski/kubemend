"""Command-line entrypoint (ARCHITECTURE.md §9).

Exposes `kubemend run | evals | trace replay`. The commands arrive with the
milestones that give them something to do: `run` in M2, `evals` in M4, `trace
replay` alongside the replay round-trip test.

Until then each one fails loudly rather than silently succeeding, and the
signatures stay bare — the eval runner's option surface is M4's to design
against real scenarios, not something to guess at here.
"""

import typer

app = typer.Typer(
    name="kubemend",
    help="Diagnose Kubernetes incidents and propose GitOps fixes as draft PRs.",
    no_args_is_help=True,
)

trace_app = typer.Typer(help="Inspect the JSONL trace a run writes.", no_args_is_help=True)
app.add_typer(trace_app, name="trace")


def _not_yet(command: str, milestone: str) -> None:
    typer.echo(
        f"`kubemend {command}` is implemented in {milestone} (see IMPLEMENTATION_PLAN.md).",
        err=True,
    )
    raise typer.Exit(code=1)


@app.command()
def run() -> None:
    """Diagnose an incident and propose a fix."""
    _not_yet("run", "M2")


@app.command()
def evals() -> None:
    """Run scenario sweeps and emit the pass-rate report."""
    _not_yet("evals", "M4")


@trace_app.command()
def replay() -> None:
    """Reconstruct a run's event sequence from its trace."""
    _not_yet("trace replay", "M1")
