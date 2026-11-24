"""Project rules for kube-prometheus-stack (k8s/platform/kube-prometheus-stack).

Checked on the chart as it is installed: `scripts/addon.sh render kube-prometheus-stack ENV` renders the
pinned chart with that environment's values, the same chart and values `addon.sh install` uses.

The alert rules (monitoring/rules) are unit-tested by ci/rules-test.sh with promtool from a pinned Prometheus
image. That must be the Prometheus this chart runs, or the tests prove the rules on another version.
"""

import re
import subprocess
from typing import Any

import pytest
import yaml

from conftest import REPO_ROOT

ADDON = REPO_ROOT / "scripts" / "addon.sh"
RULES_TEST = REPO_ROOT / "ci" / "rules-test.sh"
# The environments kube-prometheus-stack runs in (test_addon_scripts.py checks the full list).
ENVIRONMENTS = ["local"]


@pytest.fixture(scope="module", params=ENVIRONMENTS)
def kps(request: pytest.FixtureRequest) -> list[dict[str, Any]]:
    """The chart rendered with one environment's values. A render that fails is a test failure, not a skip."""
    result = subprocess.run(["bash", str(ADDON), "render", "kube-prometheus-stack", request.param],
                            capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, f"addon.sh render kube-prometheus-stack {request.param} failed:\n{result.stderr}"
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def test_rules_are_tested_with_the_prometheus_the_chart_runs(kps: list) -> None:
    # The chart's own image, registry and variant included: it carries promtool too.
    [prometheus] = [d for d in kps if d["kind"] == "Prometheus"]
    runs = prometheus["spec"]["image"]
    found = re.search(r'^PROMETHEUS_IMAGE="([^"]+)"', RULES_TEST.read_text(), re.M)
    assert found, "ci/rules-test.sh must name its Prometheus image in PROMETHEUS_IMAGE"
    assert found.group(1) == runs, f"the rules are tested with {found.group(1)}, the chart runs {runs}"
