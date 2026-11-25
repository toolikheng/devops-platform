"""Tool versions that more than one script uses are pinned once, in ci/versions.env.

The scripts source that file. What can't (the k3s role, the GitHub Actions inputs) must match it, and no
script may keep its own copy of a pinned version: a bump is one edit, and a missed copy fails here.
"""

from pathlib import Path

import pytest
import yaml

from conftest import REPO_ROOT, VERSIONS_ENV, versions
from fakecli import Fakes, output

K3S_DEFAULTS = REPO_ROOT / "ansible" / "roles" / "k3s" / "defaults" / "main.yml"
CI = REPO_ROOT / ".github" / "workflows" / "ci.yml"
SCRIPTS = sorted([*(REPO_ROOT / "scripts").glob("*.sh"), *(REPO_ROOT / "ci").glob("*.sh"), REPO_ROOT / "Makefile"])


def test_kubernetes_pin_is_the_nodes_k3s_version() -> None:
    # kubeconform validates against this version, and Helm renders the ESO chart for it.
    k3s = yaml.safe_load(K3S_DEFAULTS.read_text())["k3s_version"]  # e.g. v1.36.6+k3s1
    assert k3s == f"v{versions()['KUBERNETES_VERSION']}+k3s1"


def test_ci_installs_the_pinned_trivy_version() -> None:
    # The image scan in CI must judge the image with the same Trivy as ci/scan.sh does locally.
    steps = [s for job in yaml.safe_load(CI.read_text())["jobs"].values() for s in job["steps"]
             if s.get("uses", "").startswith("aquasecurity/trivy-action@")]
    assert steps and all(s["with"]["version"] == f"v{versions()['TRIVY_VERSION']}" for s in steps)


@pytest.mark.parametrize("name", ["KUBERNETES_VERSION", "TRIVY_VERSION", "RESTIC_VERSION"])
def test_only_the_pin_file_writes_a_pinned_version(name: str) -> None:
    value = versions()[name]
    copies = [str(p.relative_to(REPO_ROOT)) for p in SCRIPTS if value in p.read_text()]
    assert copies == [], f"{name}={value} is written in {copies}; source {VERSIONS_ENV.name} instead"


@pytest.mark.parametrize("script", ["ci/scan.sh", "ci/terraform-checks.sh"])
def test_trivy_runs_at_the_pinned_version(fakes: Fakes, script: str) -> None:
    result = fakes.script(str(REPO_ROOT / script))
    assert result.returncode == 0, output(result)
    images = {a for c in fakes.calls("docker") for a in c["args"] if a.startswith("aquasec/trivy:")}
    assert images == {f"aquasec/trivy:{versions()['TRIVY_VERSION']}"}


def test_scripts_are_found() -> None:
    # The copy rule above is only as good as the list it reads.
    names = {Path(p).name for p in SCRIPTS}
    assert {"addon.sh", "k8s-checks.sh", "scan.sh", "terraform-checks.sh", "Makefile"} <= names
