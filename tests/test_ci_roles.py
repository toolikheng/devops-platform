"""The GitHub roles (terraform/persistent) and what uses them must agree, offline.

A job whose OIDC subject its role does not trust fails only on GitHub, with "Not authorized to perform
sts:AssumeRoleWithWebIdentity"; a cost check the nightly role may not run fails only at 02:00. Here they fail
in a second instead. Facts written in more than one place are also kept equal:
- each workflow job's subject (environment, pull request or master) is one its role trusts;
- the instance-type allowlist: the compute module's validation, the apply role's IAM condition and the
  terraform.yml input choices;
- every call scripts/cost-check.sh makes is allowed to the nightly destroy role, which runs it after the
  destroy.
The Terraform files are read as text, like tests/test_names.py does: the values tested are plain literals.
"""

import fnmatch
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from conftest import REPO_ROOT

PERSISTENT = REPO_ROOT / "terraform" / "persistent"
GITHUB_OIDC = PERSISTENT / "github_oidc.tf"
GITHUB_PERMISSIONS = PERSISTENT / "github_permissions.tf"
COMPUTE_VARIABLES = REPO_ROOT / "terraform" / "modules" / "compute" / "variables.tf"
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
COST_CHECK = REPO_ROOT / "scripts" / "cost-check.sh"
ALLOWLIST = ["t3.micro", "t3.small", "t4g.micro", "t4g.small", "c7i-flex.large", "m7i-flex.large"]
REPO = "toolikheng/devops-platform"
# AWS CLI service names whose IAM prefix differs.
IAM_PREFIX = {"elbv2": "elasticloadbalancing", "elb": "elasticloadbalancing"}


def hcl_list(text: str, name: str) -> list[str]:
    """The strings of a list literal `name = [ ... ]` in an HCL file."""
    match = re.search(rf"\b{name}\s*=\s*\[(.*?)\]", text, re.S)
    assert match, f"{name} not found"
    return re.findall(r'"([^"]+)"', match.group(1))


def role_subjects() -> dict[str, set[str]]:
    """Role name -> the GitHub subjects (after "repo:<owner>/<repo>:") its trust policy allows."""
    text = GITHUB_OIDC.read_text()
    repo = re.search(r'github_repository\s*=\s*"([^"]+)"', text)
    assert repo and repo.group(1) == REPO
    block = re.search(r"github_role_subjects\s*=\s*\{(.*?)\n\s*\}", text, re.S)
    assert block, "github_role_subjects not found"
    pairs = re.findall(r'"(gha-[a-z-]+)"\s*=\s*\[([^\]]*)\]', block.group(1))
    return {role: set(re.findall(r'"([^"]+)"', subjects)) for role, subjects in pairs}


def workflow(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text())
    data["on"] = data.pop(True, data.get("on"))
    return data


def jobs_with_roles() -> list[tuple[str, str, str, set[str]]]:
    """(workflow, job, role, subjects the job's token can carry) for every job that assumes an AWS role."""
    found = []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        data = workflow(path)
        for name, job in data["jobs"].items():
            for step in job.get("steps", []):
                if not step.get("uses", "").startswith("aws-actions/configure-aws-credentials@"):
                    continue
                role = step["with"]["role-to-assume"].rsplit("/", 1)[1]
                if "environment" in job:
                    env = job["environment"]
                    subjects = {f"environment:{env if isinstance(env, str) else env['name']}"}
                else:
                    events = set(data["on"])
                    condition = str(job.get("if", ""))
                    for event in ("pull_request", "workflow_dispatch", "schedule", "push"):
                        if f"github.event_name == '{event}'" in condition:
                            events = {event}
                    subjects = {"pull_request" if e == "pull_request" else "ref:refs/heads/master" for e in events}
                found.append((path.name, name, role, subjects))
    return found


def test_the_roles_and_their_subjects_are_found() -> None:
    # The checks below are only as good as what they read.
    assert role_subjects() == {
        "gha-tf-plan": {"pull_request", "ref:refs/heads/master"},
        "gha-tf-apply": {"environment:aws-apply"},
        "gha-tf-destroy": {"environment:aws-nightly"},
        "gha-deploy": {"environment:aws-deploy"},
    }
    assert {role for _, _, role, _ in jobs_with_roles()} == set(role_subjects())


def test_every_job_assumes_a_role_that_trusts_it() -> None:
    trusted = role_subjects()
    for path, job, role, subjects in jobs_with_roles():
        assert role in trusted, f"{path} {job}: no role {role} in Terraform"
        assert subjects <= trusted[role], f"{path} {job}: {role} does not trust {subjects - trusted[role]}"


def test_roles_that_change_aws_are_used_only_in_their_environment() -> None:
    # The environment is what makes them master-only (and, for aws-apply, owner-approved).
    assert jobs_with_roles()
    for path, job, role, subjects in jobs_with_roles():
        if role != "gha-tf-plan":
            assert len(subjects) == 1 and next(iter(subjects)).startswith("environment:"), f"{path} {job}"


def test_the_instance_type_allowlist_is_the_same_everywhere() -> None:
    compute = re.search(r"contains\(\[([^\]]+)\],\s*var\.instance_type\)", COMPUTE_VARIABLES.read_text())
    assert compute, "the compute module's allowlist validation"
    assert re.findall(r'"([^"]+)"', compute.group(1)) == ALLOWLIST
    assert hcl_list(GITHUB_PERMISSIONS.read_text(), "instance_type_allowlist") == ALLOWLIST
    inputs = workflow(WORKFLOWS / "terraform.yml")["on"]["workflow_dispatch"]["inputs"]
    assert inputs["instance_type"]["options"] == ALLOWLIST


def cost_check_actions() -> set[str]:
    """IAM actions for the AWS CLI calls scripts/cost-check.sh makes (lower case, as IAM compares them)."""
    text = COST_CHECK.read_text()
    calls = re.findall(r'\bcheck "[^"]+" "\$region" ([a-z0-9]+) ([a-z0-9-]+)', text)
    calls += re.findall(r"\baws ([a-z0-9]+) ([a-z0-9-]+)\b", text)
    actions = set()
    for service, operation in calls:
        if service in ("login", "sts"):  # `aws login` is advice in a message; GetCallerIdentity needs no permission
            continue
        actions.add(f"{IAM_PREFIX.get(service, service)}:{operation.replace('-', '')}".lower())
    return actions


@pytest.mark.parametrize("action", sorted(cost_check_actions()))
def test_the_nightly_role_may_run_every_cost_check(action: str) -> None:
    allowed = [a.lower() for a in hcl_list(GITHUB_PERMISSIONS.read_text(), "cost_check_reads")]
    assert any(fnmatch.fnmatchcase(action, pattern) for pattern in allowed), action


def test_cost_check_calls_are_found() -> None:
    actions = cost_check_actions()
    assert {"ec2:describeinstances", "ec2:describeregions", "kms:describekey", "sagemaker:listendpoints",
            "elasticloadbalancing:describeloadbalancers"} <= actions
    assert len(actions) >= 20
