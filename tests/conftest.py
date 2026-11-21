"""Shared fixtures for the repo-level tests."""

import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
NAMES_MODULE = REPO_ROOT / "terraform" / "modules" / "names" / "outputs.tf"
RENDER = REPO_ROOT / "scripts" / "render-k8s.sh"
OVERLAYS = ("local", "aws")


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
