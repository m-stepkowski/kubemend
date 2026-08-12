"""Read-only cluster reader (ARCHITECTURE.md §3.2, tool contract `get_k8s_state`).

Runs against the generated read-only kubeconfig. Non-allow-listed kinds return a
`forbidden_kind` error; ConfigMaps return key names and value lengths only;
Secret values are never fetched. Strips managedFields and last-applied noise,
and caps events at 30 sorted by recency.

The allow-list here is defence in depth, not the security boundary: the
ServiceAccount in lab/bootstrap/rbac.yaml has no verbs on secrets at all, so a
bug in this file cannot turn into a credential leak. It exists because a clear
`forbidden_kind` error teaches the model what it may ask for, where an RBAC
rejection reads like a broken cluster.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from kubemend.tools.base import ClientError, ToolSpec
from kubemend.tools.redact import redact_env_list

# Canonical kind -> (apiVersion, Kind). The tool contract exposes the short
# lowercase names; the API adapter needs the real group/version pair.
ALLOWED_KINDS: dict[str, tuple[str, str]] = {
    "pod": ("v1", "Pod"),
    "deployment": ("apps/v1", "Deployment"),
    "statefulset": ("apps/v1", "StatefulSet"),
    "service": ("v1", "Service"),
    "configmap": ("v1", "ConfigMap"),
    "event": ("v1", "Event"),
    "hpa": ("autoscaling/v2", "HorizontalPodAutoscaler"),
    "resourcequota": ("v1", "ResourceQuota"),
    "ingress": ("networking.k8s.io/v1", "Ingress"),
}

MAX_EVENTS = 30

# Fields that are pure control-plane bookkeeping. They can be kilobytes per
# object and say nothing about why a pod is unhealthy, so they are dropped
# before the payload is ever measured against the truncation cap.
_NOISE_METADATA = ("managedFields", "ownerReferences", "finalizers", "generation")
_NOISE_ANNOTATIONS = (
    "kubectl.kubernetes.io/last-applied-configuration",
    "kubectl.kubernetes.io/restartedAt",
)


class ResourceClient(Protocol):
    """The narrow slice of the Kubernetes API the reader needs.

    A Protocol rather than the kubernetes client directly, so the shaping logic
    below is unit-testable against fixtures without a cluster or a mock library.
    """

    def list_resource(
        self, kind: str, namespace: str, selector: str | None = None
    ) -> list[dict[str, Any]]: ...

    def get_resource(self, kind: str, namespace: str, name: str) -> dict[str, Any]: ...

    def list_events(self, namespace: str, involved: str | None = None) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class K8sQuery:
    kind: str
    namespace: str
    name: str | None = None
    selector: str | None = None
    include_events: bool = True


def strip_noise(obj: dict[str, Any]) -> dict[str, Any]:
    shaped = dict(obj)
    metadata = dict(shaped.get("metadata", {}))
    for key in _NOISE_METADATA:
        metadata.pop(key, None)
    annotations = {
        k: v for k, v in (metadata.get("annotations") or {}).items() if k not in _NOISE_ANNOTATIONS
    }
    if annotations:
        metadata["annotations"] = annotations
    else:
        metadata.pop("annotations", None)
    shaped["metadata"] = metadata
    return shaped


def shape_configmap(obj: dict[str, Any]) -> dict[str, Any]:
    """Key names and value sizes only.

    `missing-configmap-key` needs to know which keys exist, never what they
    contain — and a ConfigMap is exactly where someone eventually puts a
    credential that does not belong in one.
    """
    shaped = strip_noise(obj)
    data = shaped.pop("data", {}) or {}
    binary = shaped.pop("binaryData", {}) or {}
    shaped["keys"] = sorted(data)
    shaped["value_bytes"] = {k: len(str(v)) for k, v in sorted(data.items())}
    if binary:
        shaped["binary_keys"] = sorted(binary)
    return shaped


def shape_pod(obj: dict[str, Any]) -> dict[str, Any]:
    shaped = strip_noise(obj)
    spec = dict(shaped.get("spec", {}))
    for field in ("containers", "initContainers"):
        containers = spec.get(field)
        if not isinstance(containers, list):
            continue
        shaped_containers = []
        for container in containers:
            if not isinstance(container, dict):
                shaped_containers.append(container)
                continue
            entry = dict(container)
            if isinstance(entry.get("env"), list):
                entry["env"] = redact_env_list(entry["env"])
            shaped_containers.append(entry)
        spec[field] = shaped_containers
    shaped["spec"] = spec
    return shaped


def cap_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Most recent first, capped. Events are unbounded and mostly repetitive."""

    def recency(event: dict[str, Any]) -> str:
        return str(
            event.get("lastTimestamp")
            or event.get("eventTime")
            or event.get("firstTimestamp")
            or ""
        )

    return sorted(events, key=recency, reverse=True)[:MAX_EVENTS]


def shape(kind: str, obj: dict[str, Any]) -> dict[str, Any]:
    if kind == "configmap":
        return shape_configmap(obj)
    if kind == "pod":
        return shape_pod(obj)
    return strip_noise(obj)


class KubernetesReader:
    def __init__(self, client: ResourceClient) -> None:
        self._client = client

    def get_state(self, query: K8sQuery) -> dict[str, Any]:
        kind = query.kind.lower()
        if kind not in ALLOWED_KINDS:
            raise ForbiddenKind(
                f"kind '{query.kind}' is not readable; allowed kinds are "
                f"{', '.join(sorted(ALLOWED_KINDS))}"
            )
        if query.name:
            items = [self._client.get_resource(kind, query.namespace, query.name)]
        else:
            items = self._client.list_resource(kind, query.namespace, query.selector)

        payload: dict[str, Any] = {
            "kind": kind,
            "namespace": query.namespace,
            "items": [shape(kind, item) for item in items],
        }
        if not items:
            payload["hint"] = (
                f"no {kind} found in namespace '{query.namespace}'"
                f"{f' matching selector {query.selector}' if query.selector else ''}"
            )

        if query.include_events and kind != "event":
            events = self._client.list_events(query.namespace, query.name)
            payload["events"] = [strip_noise(e) for e in cap_events(events)]
        return payload


class ForbiddenKind(ClientError):
    """Requested a kind outside the allow-list. Never retried — the model must
    ask for something else."""

    error_type = "forbidden_kind"


def k8s_tool_spec(reader: KubernetesReader) -> ToolSpec:
    """`get_k8s_state` as the model sees it (docs/knowledge/tool-contracts.md)."""

    def _execute(args: dict[str, Any]) -> dict[str, Any]:
        return reader.get_state(
            K8sQuery(
                kind=str(args["kind"]),
                namespace=str(args["namespace"]),
                name=str(args["name"]) if args.get("name") else None,
                selector=str(args["selector"]) if args.get("selector") else None,
                include_events=bool(args.get("include_events", True)),
            )
        )

    return ToolSpec(
        name="get_k8s_state",
        description=(
            "Read Kubernetes state (read-only). Allowed kinds: pod, deployment, "
            "statefulset, service, configmap (keys only), event, hpa, resourcequota, "
            "ingress. Secrets are never readable. Provide name OR selector."
        ),
        parameters={
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": sorted(ALLOWED_KINDS)},
                "namespace": {"type": "string"},
                "name": {"type": "string"},
                "selector": {"type": "string", "description": "label selector"},
                "include_events": {"type": "boolean", "default": True},
            },
            "required": ["kind", "namespace"],
        },
        executor=_execute,
        tier="read",
        timeout_s=15.0,
    )
