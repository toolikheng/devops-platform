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
PLATFORM = REPO_ROOT / "k8s" / "platform"
VERSIONS_ENV = REPO_ROOT / "ci" / "versions.env"
SECRET_CATALOG = REPO_ROOT / "terraform" / "persistent" / "secret-catalog.yaml"
RESTIC_BACKUP_TASKS = REPO_ROOT / "ansible" / "roles" / "restic_backup" / "tasks" / "main.yml"
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


def _env_file(path: Path) -> dict[str, str]:
    """The KEY=value lines of a file the scripts source."""
    return dict(re.findall(r"^([A-Z_]+)=(\S+)$", path.read_text(), re.M))


def chart_env(name: str) -> dict[str, str]:
    """k8s/platform/NAME/chart.env: the add-on's pinned chart, which scripts/addon.sh installs and renders."""
    return _env_file(PLATFORM / name / "chart.env")


def addon_envs() -> list[tuple[str, str]]:
    """(add-on, environment) for every values file under k8s/platform: where each add-on runs."""
    return sorted((p.parent.name, p.stem.removeprefix("values-")) for p in PLATFORM.glob("*/values-*.yaml"))


def addon_manifests(name: str) -> list[Path]:
    """The files addon.sh applies after the add-on's chart (MANIFESTS in its chart.env)."""
    return [PLATFORM / name / m for m in chart_env(name).get("MANIFESTS", "").split()]


def secret_catalog() -> dict[str, dict[str, str]]:
    """terraform/persistent/secret-catalog.yaml: every secret Terraform keeps in SSM Parameter Store, by its name
    under /policypal/aws/, with the namespace whose ExternalSecret reads it (no namespace: the node reads it)."""
    return yaml.safe_load(SECRET_CATALOG.read_text())


def versions() -> dict[str, str]:
    """ci/versions.env: the tool versions more than one script uses."""
    return _env_file(VERSIONS_ENV)


def backup_timer_tasks() -> list[dict[str, Any]]:
    """The tasks of the AWS node's restic_backup role that do anything with policypal-backup.timer other than
    install its unit file: any of them would decide whether a node fresh from `make cloud-config` backs up hourly."""
    tasks = yaml.safe_load(RESTIC_BACKUP_TASKS.read_text())
    return [t for t in tasks if "policypal-backup.timer" in yaml.safe_dump(t) and "ansible.builtin.template" not in t]


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
