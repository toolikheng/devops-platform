"""Rules for the GitHub Actions workflows (.github/workflows). The repo is public, so its logs are public.

C5: the secret scan. It runs on every pull request, weekly and on demand, can only read the repository,
needs no secrets, and scans the full history (a shallow checkout would scan one commit). The repo-level
tests need the full history too: tests/test_secret_scanning.py proves the gitleaks rule on commit 7ca581f.
"""

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"
SECRET_SCAN = WORKFLOWS / "secret-scan.yml"
CI = WORKFLOWS / "ci.yml"


def load(path: Path) -> dict[str, Any]:
    workflow = yaml.safe_load(path.read_text())
    workflow["on"] = workflow.pop(True, workflow.get("on"))  # YAML 1.1 reads the key `on` as true
    return workflow


def steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    return job["steps"]


def checkout(job: dict[str, Any]) -> dict[str, Any]:
    found = [s for s in steps(job) if s.get("uses", "").startswith("actions/checkout@")]
    assert len(found) == 1
    return found[0]


def test_secret_scan_runs_on_pull_requests_weekly_and_on_demand() -> None:
    on = load(SECRET_SCAN)["on"]
    assert {"pull_request", "schedule", "workflow_dispatch"} <= set(on)
    [schedule] = on["schedule"]
    minute, hour, day, month, weekday = schedule["cron"].split()
    assert (day, month) == ("*", "*") and re.fullmatch(r"[0-6]", weekday), "once a week"


def test_secret_scan_can_only_read_the_repository() -> None:
    workflow = load(SECRET_SCAN)
    assert workflow["permissions"] == {}
    for name, job in workflow["jobs"].items():
        assert job.get("permissions") == {"contents": "read"}, name
    assert "secrets." not in SECRET_SCAN.read_text(), "no secret is needed to scan for secrets"


def test_secret_scan_scans_the_full_history_with_the_repos_script() -> None:
    [job] = load(SECRET_SCAN)["jobs"].values()
    with_ = checkout(job).get("with", {})
    assert with_.get("fetch-depth") == 0, "a shallow clone would scan one commit"
    assert with_.get("persist-credentials") is False, "the job never pushes"
    assert [s["run"] for s in steps(job) if "run" in s] == ["bash ci/gitleaks.sh"]


def test_repo_tests_get_the_full_history() -> None:
    job = load(CI)["jobs"]["infra-checks"]
    assert checkout(job).get("with", {}).get("fetch-depth") == 0, "test_secret_scanning.py needs commit 7ca581f"


@pytest.mark.parametrize("path", sorted(WORKFLOWS.glob("*.yml")), ids=lambda p: p.name)
def test_actions_are_pinned(path: Path) -> None:
    uses = re.findall(r"^\s*(?:-\s*)?uses:\s*(\S+)", path.read_text(), re.M)
    for ref in uses:
        assert re.search(r"@(v\d+(\.\d+){0,2}|[0-9a-f]{40})$", ref), f"{path.name}: {ref}"
