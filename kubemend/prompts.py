"""Prompt template loader.

CLAUDE.md forbids inline prompt strings in Python: prompts are versioned and
reviewed like code, so they live in `prompts/*.j2` and are rendered here.

ARCHITECTURE.md §9 puts that directory at the repo root, which is fine for a
checkout but invisible to an installed wheel. The build force-includes it into
the package (see pyproject.toml), so the installed copy sits next to this module
and the repo-root copy is the development fallback.

`StrictUndefined` is deliberate: a typo in a template variable must fail loudly
at render time rather than silently producing a prompt with a hole in it.
"""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

_PACKAGED = Path(__file__).resolve().parent / "prompts"
_REPO_ROOT = Path(__file__).resolve().parent.parent / "prompts"

_env = Environment(
    loader=FileSystemLoader([str(_PACKAGED), str(_REPO_ROOT)]),
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
