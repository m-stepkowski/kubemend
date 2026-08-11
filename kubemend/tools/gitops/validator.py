"""Validation pipeline (ARCHITECTURE.md §5).

Four stages against the active branch's working tree: helm template, Kyverno
policy check against the project's own pack, live diff (argocd app diff, falling
back to kubectl diff --server-side), and the harness-owned scope check. An empty
diff fails as `no_effective_change` — it catches the model "fixing" a value by
rewriting it to itself.

Uses the Taskfile-pinned helm and kyverno binaries, never PATH.
"""
