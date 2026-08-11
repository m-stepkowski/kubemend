"""Kubernetes access (ARCHITECTURE.md §3.2).

Read-only by construction: a generated ServiceAccount kubeconfig with no secrets
verbs, plus a kind allow-list in the executor.
"""
