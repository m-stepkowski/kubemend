# syntax=docker/dockerfile:1
#
# Multi-arch image for `kubemend` (ARCHITECTURE.md §8, M8a).
#
# The pinned tool versions (helm/kyverno/kubectl/argocd) below are copied by
# hand from Taskfile.yaml's `vars:` block — CLAUDE.md's "never rely on
# system PATH versions" applies here too, and the same real bug class
# (a PATH helm a major version ahead of pinned, rendering different
# manifests) is exactly what pinning here avoids. These two pin sites can
# drift; there is no automated cross-check yet (see docs/decisions.md).

ARG PYTHON_VERSION=3.12-slim

# -- builder: install the project into a venv, no dev deps -------------------
FROM python:${PYTHON_VERSION} AS builder
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /src
COPY pyproject.toml uv.lock README.md ./
COPY kubemend ./kubemend
RUN uv sync --locked --no-dev --no-editable

# -- binaries: pinned helm/kyverno/kubectl/argocd, arch-aware ----------------
FROM python:${PYTHON_VERSION} AS binaries
ARG TARGETOS
ARG TARGETARCH
ARG HELM_VERSION=v3.16.3
ARG KYVERNO_VERSION=v1.13.2
ARG KUBECTL_VERSION=v1.31.3
ARG ARGOCD_VERSION=v2.13.1
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /bin-out
RUN set -eu; \
    tmp=$(mktemp -d); \
    curl -fsSL "https://get.helm.sh/helm-${HELM_VERSION}-${TARGETOS}-${TARGETARCH}.tar.gz" \
      | tar -xz -C "$tmp"; \
    mv "$tmp/${TARGETOS}-${TARGETARCH}/helm" ./helm; \
    rm -rf "$tmp"
# kyverno's release assets use x86_64/arm64 (uname-style), not Docker's own
# amd64/arm64 TARGETARCH convention the other three tools share — arm64
# happens to match either way, which is why this only breaks on amd64 and
# was never caught building locally on an arm64 (Apple Silicon) machine.
RUN set -eu; \
    tmp=$(mktemp -d); \
    kyverno_arch="${TARGETARCH}"; \
    [ "$kyverno_arch" = "amd64" ] && kyverno_arch="x86_64"; \
    curl -fsSL "https://github.com/kyverno/kyverno/releases/download/${KYVERNO_VERSION}/kyverno-cli_${KYVERNO_VERSION}_${TARGETOS}_${kyverno_arch}.tar.gz" \
      | tar -xz -C "$tmp"; \
    mv "$tmp/kyverno" ./kyverno; \
    rm -rf "$tmp"
RUN curl -fsSL -o ./kubectl \
      "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/${TARGETOS}/${TARGETARCH}/kubectl"
RUN curl -fsSL -o ./argocd \
      "https://github.com/argoproj/argo-cd/releases/download/${ARGOCD_VERSION}/argocd-${TARGETOS}-${TARGETARCH}"
RUN chmod +x ./helm ./kyverno ./kubectl ./argocd

# -- runtime: slim, non-root, entrypoint decides the role via subcommand -----
FROM python:${PYTHON_VERSION} AS runtime
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    KUBEMEND_BIN_DIR=/usr/local/lib/kubemend-tools \
    KUBEMEND_MODEL__PRICING_TABLE=/usr/local/lib/kubemend-tools/pricing.yaml \
    KUBEMEND_POLICIES_DIR=/usr/local/lib/kubemend-tools/policies \
    KUBEMEND_OPERATOR__CHART_DIR=/usr/local/lib/kubemend-tools/chart \
    PATH="/src/.venv/bin:$PATH"

RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
# GitPython checks for a real `git` executable at import time (kubemend.tools.gitops
# imports it eagerly), not lazily — a slim base with no git binary fails on
# every `kubemend` invocation, not just ones that touch a repo.

RUN groupadd --system kubemend && useradd --system --gid kubemend --create-home kubemend

COPY --from=builder /src/.venv /src/.venv
COPY --from=binaries /bin-out/ /usr/local/lib/kubemend-tools/
# config/pricing.yaml and policies/ both live at the repo top level, like
# evals/ — outside the kubemend/ package hatchling ships, so each needs its
# own COPY + a baked-in default path here. Missing pricing.yaml doesn't crash
# (load_pricing() falls back to FALLBACK_PRICE for every model per
# cost.py's docstring), it just silently reports wrong costs. Missing
# policies/ is worse: the verification gate's Kyverno check either fails
# outright or reports `no_policies_applied`, silently degrading I1 for
# every containerized run — found by actually running a live incident
# through a Job, not by review.
COPY config/pricing.yaml /usr/local/lib/kubemend-tools/pricing.yaml
COPY policies /usr/local/lib/kubemend-tools/policies
# The operator (M8b) shells out to `helm template` against this chart to
# create incident Jobs, reusing the exact same Job shape and escape hatches
# (extraInitContainers, env/envFrom) the manual helm-install path already
# has, instead of duplicating them as a hand-built Job manifest.
COPY charts/kubemend /usr/local/lib/kubemend-tools/chart

WORKDIR /workspace
RUN chown kubemend:kubemend /workspace
USER kubemend

ENTRYPOINT ["kubemend"]
