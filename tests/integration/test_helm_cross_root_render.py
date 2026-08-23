"""M11 design doc §11 risk 2 — does `helm template <chart-dir> --values
<other-root/file>` actually work when the chart directory and the values file
live under completely unrelated filesystem roots?

This is the split-mode render shape `Validator._render` needs once chart and
values live in separate checkouts (M11 §6) — verified here against the real
pinned helm binary, independent of any harness code, before validator.py is
touched. Real external process, no cluster — `integration`, not `lab`, so this
runs as part of `task test` when the pinned binary happens to be present, and
skips cleanly on a checkout that hasn't run `task lab:tools` yet.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

HELM_BIN = Path(".lab/bin/helm")

CHART_YAML = """\
apiVersion: v2
name: split-mode-probe
version: 0.1.0
"""

DEPLOYMENT_TEMPLATE = """\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ .Release.Name }}
  namespace: {{ .Values.namespace }}
spec:
  replicas: {{ .Values.replicas }}
  selector:
    matchLabels:
      app: {{ .Release.Name }}
  template:
    metadata:
      labels:
        app: {{ .Release.Name }}
    spec:
      containers:
        - name: app
          image: {{ .Values.image }}
"""

VALUES_YAML = """\
namespace: shop
replicas: 3
image: nginx:1.27-alpine
"""


def _helm_available() -> bool:
    return HELM_BIN.is_file()


@pytest.mark.skipif(not _helm_available(), reason=f"{HELM_BIN} not present; run `task lab:tools`")
def test_helm_template_accepts_a_values_file_on_a_different_root(tmp_path: Path) -> None:
    # Two roots with no relation to each other on disk, standing in for a
    # chart-repo checkout and a values-repo checkout in split mode.
    chart_root = tmp_path / "chart-workspace" / "shop-api"
    values_root = tmp_path / "values-workspace" / "apps" / "shop-api"
    chart_root.joinpath("templates").mkdir(parents=True)
    values_root.mkdir(parents=True)

    chart_root.joinpath("Chart.yaml").write_text(CHART_YAML)
    chart_root.joinpath("templates", "deployment.yaml").write_text(DEPLOYMENT_TEMPLATE)
    values_file = values_root / "values.yaml"
    values_file.write_text(VALUES_YAML)

    result = subprocess.run(
        [
            str(HELM_BIN.resolve()),
            "template",
            "shop-api",
            str(chart_root),
            "--values",
            str(values_file),
            "--namespace",
            "shop",
            "--kube-version",
            "1.31.2",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "replicas: 3" in result.stdout
    assert "image: nginx:1.27-alpine" in result.stdout
    assert "namespace: shop" in result.stdout
