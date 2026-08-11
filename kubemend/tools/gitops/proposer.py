"""propose_git_change executor (ARCHITECTURE.md §4, tool contract §propose).

Enforces the `writable_globs` path policy before anything is written: a path
outside `apps/**/values*.yaml` yields a structured `path_not_writable` error and
no file is touched. Files are YAML-parsed up front as a cheap pre-gate that
saves render cycles.

One active branch per run — the first call opens `kubemend/<run_id>` off the
base branch, later calls amend it.
"""
