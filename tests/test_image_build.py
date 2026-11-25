"""The app image always gets Debian's current security updates.

The runtime stage of app/Dockerfile runs `apt-get upgrade`, but a cached layer keeps the packages that were current
when it was first built. CI's GitHub Actions cache kept serving one from before Debian fixed libzstd1
(CVE-2026-124851), so the Trivy gate failed once the advisory appeared, and master's published image had the same
package. The runtime stage is therefore named and never taken from a cache: not in the build CI scans, and not in
ci/build.sh (`make build`). The builder stage (the Python packages) stays cached.
"""

import re
from typing import Any

import yaml

from conftest import REPO_ROOT
from fakecli import Fakes, output

DOCKERFILE = REPO_ROOT / "app" / "Dockerfile"
CI = REPO_ROOT / ".github" / "workflows" / "ci.yml"
BUILD_SH = REPO_ROOT / "ci" / "build.sh"


def build_steps() -> dict[str, dict[str, Any]]:
    return {s["name"]: s for s in yaml.safe_load(CI.read_text())["jobs"]["build"]["steps"] if "name" in s}


def test_the_runtime_stage_is_named_and_upgrades_debian_packages() -> None:
    stages = re.findall(r"^FROM (\S+)(?: AS (\S+))?$", DOCKERFILE.read_text(), re.M)
    assert [name for _, name in stages] == ["builder", "runtime"], stages
    runtime = DOCKERFILE.read_text().split(" AS runtime", 1)[1]
    assert "apt-get update && apt-get upgrade -y" in runtime


def test_ci_scans_an_image_whose_runtime_stage_was_built_now() -> None:
    steps = build_steps()
    build = steps["Build image (local)"]["with"]
    gate = steps["Fail on fixable HIGH/CRITICAL vulnerabilities (gate)"]["with"]
    assert build["no-cache-filters"] == "runtime"
    assert gate["image-ref"] == build["tags"], "the gate scans the image built without a cached runtime stage"


def test_the_local_build_never_reuses_a_cached_runtime_stage(fakes: Fakes) -> None:
    result = fakes.script(str(BUILD_SH), "devops-app:test")
    assert result.returncode == 0, output(result)
    [build] = [c["args"] for c in fakes.calls("docker", "build")]
    assert build[build.index("--no-cache-filter") + 1] == "runtime"


def test_no_build_skips_the_cache_for_a_stage_that_does_not_exist() -> None:
    # A filter naming a stage the Dockerfile doesn't have would silently cache everything again.
    names = {name for _, name in re.findall(r"^FROM (\S+)(?: AS (\S+))?$", DOCKERFILE.read_text(), re.M)}
    filters = re.findall(r"--no-cache-filter (\S+)", BUILD_SH.read_text())
    filters += [f for f in [build_steps()["Build image (local)"]["with"].get("no-cache-filters")] if f]
    assert len(filters) == 2 and all(f in names - {""} for f in filters), (filters, names)
