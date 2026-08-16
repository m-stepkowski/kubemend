"""Live Kubernetes API adapter for the reader (ARCHITECTURE.md §3.2).

Implements `ResourceClient` against the real cluster using the generated
read-only kubeconfig. Kept separate from reader.py so all the shaping and
redaction logic stays testable without a cluster — this file is the only part
of the read path that needs one.

Uses the dynamic client so one code path covers every allow-listed kind; the
alternative is a typed API object per group and a switch statement that has to
grow whenever the allow-list does.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from kubernetes import client as k8s_client
from kubernetes import config as k8s_config
from kubernetes.dynamic import DynamicClient
from kubernetes.dynamic.exceptions import (
    ForbiddenError,
    NotFoundError,
    ResourceNotFoundError,
)
from kubernetes.dynamic.resource import Resource

from kubemend.tools.base import ClientError, TransportError
from kubemend.tools.kubernetes.reader import ALLOWED_KINDS


class KubeApiClient:
    def __init__(self, kubeconfig: Path | str, context: str | None = None) -> None:
        api = k8s_config.new_client_from_config(
            config_file=str(Path(kubeconfig).expanduser()), context=context
        )
        self._dynamic = DynamicClient(api)

    @classmethod
    def in_cluster(cls) -> KubeApiClient:
        """Alternate constructor for running as a Job/Pod (ARCHITECTURE.md §8).

        In-cluster auth is defined entirely by the projected ServiceAccount
        token Kubernetes mounts automatically — there is no "context" concept
        to pass, unlike the kubeconfig-file path `__init__` covers.
        """
        k8s_config.load_incluster_config()
        self = cls.__new__(cls)
        self._dynamic = DynamicClient(k8s_client.ApiClient())
        return self

    def _resource(self, kind: str) -> Resource:
        api_version, kube_kind = ALLOWED_KINDS[kind]
        try:
            return self._dynamic.resources.get(api_version=api_version, kind=kube_kind)
        except ResourceNotFoundError as exc:
            raise ClientError(f"{kube_kind} is not served by this cluster") from exc

    def list_resource(
        self, kind: str, namespace: str, selector: str | None = None
    ) -> list[dict[str, Any]]:
        try:
            result = self._resource(kind).get(namespace=namespace, label_selector=selector)
        except ForbiddenError as exc:
            # RBAC said no. That is a permanent answer for this identity, so it
            # must not be retried (I2) — surface it as a client-class error.
            raise ClientError(f"not permitted to list {kind} in {namespace}") from exc
        except Exception as exc:
            raise TransportError(f"kubernetes API error listing {kind}: {exc}") from exc
        return [item.to_dict() for item in getattr(result, "items", [])]

    def get_resource(self, kind: str, namespace: str, name: str) -> dict[str, Any]:
        try:
            obj = self._resource(kind).get(namespace=namespace, name=name)
        except NotFoundError as exc:
            raise ClientError(f"{kind}/{name} not found in namespace {namespace}") from exc
        except ForbiddenError as exc:
            raise ClientError(f"not permitted to read {kind}/{name} in {namespace}") from exc
        except Exception as exc:
            raise TransportError(f"kubernetes API error reading {kind}/{name}: {exc}") from exc
        result: dict[str, Any] = obj.to_dict()
        return result

    def list_events(self, namespace: str, involved: str | None = None) -> list[dict[str, Any]]:
        field_selector = f"involvedObject.name={involved}" if involved else None
        try:
            core = k8s_client.CoreV1Api(self._dynamic.client)
            result = core.list_namespaced_event(
                namespace=namespace, field_selector=field_selector, limit=200
            )
        except Exception as exc:
            raise TransportError(f"kubernetes API error listing events: {exc}") from exc
        return [
            k8s_client.ApiClient().sanitize_for_serialization(item) for item in result.items or []
        ]
