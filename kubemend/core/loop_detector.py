"""Repeated-tool-call detector (ARCHITECTURE.md §2.5).

Signature is `(name, canonical_json(arguments))`. Two consecutive identical
signatures inject a nudge and skip execution; three abort to handoff. The
signature memory is held out-of-band rather than in context, so it survives
compaction — which matters because compaction is itself a common cause of the
model re-issuing a query it has already run.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from kubemend.core.model import ToolCall

NUDGE = (
    "You have already called {name} with exactly these arguments and the result "
    "is in the conversation above. It was not re-run. Use what you have, or "
    "change the query — a different time range, a tighter selector, or a "
    "different tool."
)


# Free-text arguments that explain a call without changing what it does.
# Excluded from the signature because a model re-wording its reasoning is not
# a different action — and including them made the detector trivially
# defeatable: in the M14 re-baseline a run proposed byte-identical file
# content nine times running, varying only `rationale`, and spun until its
# iteration budget died without the detector ever firing
# (evals/reports/cheap-baseline/diagnosis.md).
#
# Name-based rather than declared per-tool on ToolSpec: the rule is general,
# and threading a per-tool mapping would mean widening `run_loop`'s signature
# for it. If a tool ever needs prose to be identity-bearing, that is the
# moment to move this onto ToolSpec, not before.
NON_IDENTITY_ARGS = frozenset({"rationale", "incident_ref"})


def signature(call: ToolCall) -> tuple[str, str]:
    """Canonical JSON keeps key order from disguising a repeat.

    Prose arguments are dropped first, so the signature reflects what the call
    *does* rather than how the model narrated it.
    """
    effectful = {k: v for k, v in call.arguments.items() if k not in NON_IDENTITY_ARGS}
    return call.name, json.dumps(effectful, sort_keys=True, separators=(",", ":"))


@dataclass
class LoopDetector:
    warn_after: int = 2
    abort_after: int = 3
    _last: tuple[str, str] | None = field(default=None, repr=False)
    _streak: int = field(default=0, repr=False)

    def observe(self, call: ToolCall) -> str | None:
        """Record a call; return a nudge if it repeats the previous one.

        A returned nudge means the caller must NOT execute the call — re-running
        it would spend budget to reproduce a payload already in context.
        """
        sig = signature(call)
        if sig == self._last:
            self._streak += 1
        else:
            self._last = sig
            self._streak = 1
        if self._streak >= self.warn_after:
            return NUDGE.format(name=call.name)
        return None

    def should_abort(self) -> bool:
        return self._streak >= self.abort_after
