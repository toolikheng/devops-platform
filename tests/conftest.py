"""Shared fixtures for the repo-level tests."""

import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from fakecli import Fakes

REPO_ROOT = Path(__file__).resolve().parent.parent
NAMES_MODULE = REPO_ROOT / "terraform" / "modules" / "names" / "outputs.tf"
RENDER = REPO_ROOT / "scripts" / "render-k8s.sh"
ESO_CHART_ENV = REPO_ROOT / "k8s" / "platform" / "external-secrets" / "chart.env"
OVERLAYS = ("local", "aws")


def secret_refs(docs: list[dict[str, Any]]) -> set[tuple[str, str]]:
    """(Secret, key) for every secretKeyRef in rendered workloads, init containers included: what the
    cluster must hold before the pods can start."""
    refs = set()
    for doc in docs:
        if doc["kind"] in ("Deployment", "StatefulSet"):
            spec = doc["spec"]["template"]["spec"]
            for c in spec["containers"] + spec.get("initContainers", []):
                for e in c.get("env", []):
                    ref = e.get("valueFrom", {}).get("secretKeyRef")
                    if ref:
                        refs.add((ref["name"], ref["key"]))
    return refs


def chart_env() -> dict[str, str]:
    """KEY=value lines of chart.env: the External Secrets Operator chart that install-eso.sh installs and
    render-eso.sh renders for the checks."""
    return dict(re.findall(r"^([A-Z_]+)=(\S+)$", ESO_CHART_ENV.read_text(), re.M))


@pytest.fixture
def fakes(tmp_path: Path) -> Fakes:
    """Fake CLIs first on PATH, in this test's own folder (tests/fakecli.py)."""
    return Fakes(tmp_path)


@pytest.fixture(scope="session")
def terraform_names() -> dict[str, str]:
    """The names both Terraform stacks share, read from terraform/modules/names (output name -> value)."""
    text = NAMES_MODULE.read_text()
    return dict(re.findall(r'output "(\w+)" \{.*?value\s*=\s*"([^"]+)"', text, re.S))


@pytest.fixture(scope="session")
def rendered() -> dict[str, list[dict[str, Any]]]:
    """The PolicyPal manifests of each overlay (local, aws), rendered by scripts/render-k8s.sh: the same
    pinned kustomize the deploy uses. A render that fails is a test failure, not a skip."""
    out: dict[str, list[dict[str, Any]]] = {}
    for overlay in OVERLAYS:
        result = subprocess.run(["bash", str(RENDER), overlay], capture_output=True, text=True, timeout=120)
        assert result.returncode == 0, f"render-k8s.sh {overlay} failed:\n{result.stderr}"
        out[overlay] = [doc for doc in yaml.safe_load_all(result.stdout) if doc]
    return out
