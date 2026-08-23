"""Kubemend — a GitOps-native Kubernetes remediation agent.

Diagnoses incidents from Prometheus/Loki/Kubernetes state and remediates only by
proposing Git changes to an Argo CD + Helm repository. See ARCHITECTURE.md for
the full design; every module below carries a pointer to its section.
"""

__version__ = "0.8.0"
