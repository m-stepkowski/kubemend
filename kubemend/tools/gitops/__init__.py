"""GitOps module (ARCHITECTURE.md §4).

The agent's only actuator. It writes Helm values files on a branch and opens a
draft PR; Argo CD is the single actor that writes to the cluster, and only after
a human merges.
"""
