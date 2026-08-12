"""Prompt templates and their loader.

CLAUDE.md forbids inline prompt strings in Python: prompts are versioned and
reviewed like code, so they live as `*.j2` files beside this module.

They sit *inside* the package rather than at the repo root (ARCHITECTURE.md §9's
original layout) because a root-level directory is invisible to an installed
wheel. The obvious workaround — a build-time `force-include` into the package —
is worse: it materialises a partial `kubemend/` directory into site-packages,
which then shadows the real package for anything whose `sys.path[0]` is not the
working directory. The console script hit exactly that and failed with
`No module named 'kubemend.cli'`.

Keeping the templates here means one canonical location, correct behaviour in
both an editable install and a wheel, and an unchanged `from kubemend.prompts
import render` import path.

`StrictUndefined` is deliberate: a typo in a template variable must fail loudly
at render time rather than silently producing a prompt with a hole in it.
"""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

TEMPLATE_DIR = Path(__file__).resolve().parent

_env = Environment(
    loader=FileSystemLoader(str(TEMPLATE_DIR)),
    undefined=StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
    autoescape=False,
)


def render(template: str, **variables: object) -> str:
    """Render a prompt template.

    The result must be byte-stable for identical inputs — the pinned system
    block is a prompt-cache prefix, and a timestamp or counter in here quietly
    costs 3-5x on input tokens (§2.7).
    """
    return _env.get_template(template).render(**variables).strip()
