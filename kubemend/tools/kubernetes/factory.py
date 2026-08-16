"""KubeApiClient construction (ARCHITECTURE.md §8).

`cfg.kubernetes.in_cluster` selects the credential source — the only place in
the codebase that branches on it. Everything else only ever sees a
`KubeApiClient`. Mirrors `llm/factory.py`'s one-branch-point pattern.
"""

from __future__ import annotations

from kubemend.config import KubernetesConfig
from kubemend.tools.kubernetes.api import KubeApiClient


def build_kube_client(cfg: KubernetesConfig) -> KubeApiClient:
    if cfg.in_cluster:
        return KubeApiClient.in_cluster()
    return KubeApiClient(cfg.kubeconfig, context=cfg.context or None)
