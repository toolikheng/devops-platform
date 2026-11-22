"""Rules for the GitHub Actions workflows (.github/workflows). The repo is public, so its logs are public.

C5: the secret scan. It runs on every pull request, weekly and on demand, can only read the repository,
needs no secrets, and scans the full history (a shallow checkout would scan one commit). The repo-level
tests need the full history too: tests/test_secret_scanning.py proves the gitleaks rule on commit 7ca581f.

C6: the three workflows that reach AWS, each job with its own short-lived OIDC role and no stored key.
- terraform.yml: a read-only plan of the ephemeral stack on pull requests (resource addresses and actions
  only, as a PR comment); a manual apply or destroy that waits for the owner's approval (environment
  aws-apply), plans again and applies only if the changes are the ones approved.
- deploy.yml: renders and checks the manifests for one PolicyPal commit, then runs the deploy on the node
  through SSM Run Command (environment aws-deploy). The node rolls back if the smoke test fails.
- nightly-destroy.yml: destroys the ephemeral stack at 02:00 MYT unless a skip time is set, then runs the
  cost check (environment aws-nightly).
The roles' side (trust and permissions) is in terraform/persistent/tests; tests/test_ci_roles.py checks
that the two sides agree.
"""

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"
SECRET_SCAN = WORKFLOWS / "secret-scan.yml"
CI = WORKFLOWS / "ci.yml"
TERRAFORM = WORKFLOWS / "terraform.yml"
DEPLOY = WORKFLOWS / "deploy.yml"
NIGHTLY = WORKFLOWS / "nightly-destroy.yml"
AWS_WORKFLOWS = [TERRAFORM, DEPLOY, NIGHTLY]
CONFIGURE_AWS = "aws-actions/configure-aws-credentials@"
ROLE_ARN = re.compile(r"arn:aws:iam::\$\{\{ secrets\.AWS_ACCOUNT_ID \}\}:role/(gha-[a-z-]+)")
ALLOWLIST = ["t3.micro", "t3.small", "t4g.micro", "t4g.small", "c7i-flex.large", "m7i-flex.large"]


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


# --- C6: the AWS workflows ------------------------------------------------------------------------------

def jobs_of(path: Path) -> dict[str, dict[str, Any]]:
    return load(path)["jobs"]


def aws_step(job: dict[str, Any]) -> dict[str, Any] | None:
    found = [s for s in steps(job) if s.get("uses", "").startswith(CONFIGURE_AWS)]
    assert len(found) <= 1
    return found[0] if found else None


def role_of(job: dict[str, Any]) -> str:
    step = aws_step(job)
    assert step, "job does not assume an AWS role"
    match = ROLE_ARN.fullmatch(step["with"]["role-to-assume"])
    assert match, step["with"]["role-to-assume"]
    return match.group(1)


def runs(job: dict[str, Any]) -> list[str]:
    """The shell commands of a job's run steps, one entry per line, continuation lines joined."""
    lines = []
    for step in steps(job):
        lines += step.get("run", "").replace("\\\n", " ").splitlines()
    return [line.strip() for line in lines if line.strip()]


def index_of(job: dict[str, Any], predicate: Any) -> int:
    return next(i for i, step in enumerate(steps(job)) if predicate(step))


@pytest.mark.parametrize("path", AWS_WORKFLOWS, ids=lambda p: p.name)
def test_aws_workflows_grant_nothing_by_default(path: Path) -> None:
    # Each job asks for exactly what it needs; GITHUB_TOKEN gets no write access by default.
    workflow = load(path)
    assert workflow["permissions"] == {}
    for name, job in workflow["jobs"].items():
        assert "permissions" in job, f"{name}: no permissions block"
        assert "write-all" not in str(job["permissions"]) and job["permissions"] != "read-all", name


@pytest.mark.parametrize("path", sorted(WORKFLOWS.glob("*.yml")), ids=lambda p: p.name)
def test_only_jobs_that_assume_a_role_can_ask_for_an_oidc_token(path: Path) -> None:
    workflow = load(path)
    assert "id-token" not in str(workflow.get("permissions", {}))
    for name, job in workflow["jobs"].items():
        wants_token = (job.get("permissions") or {}).get("id-token") == "write"
        assert wants_token == (aws_step(job) is not None), name


@pytest.mark.parametrize("path", AWS_WORKFLOWS, ids=lambda p: p.name)
def test_roles_come_from_the_account_id_secret_and_the_account_id_stays_masked(path: Path) -> None:
    # A role ARN in a plain variable would print the account ID in the public log ("with:" inputs are logged);
    # the secret is masked everywhere, and the action masks the ID in its own output too.
    for name, job in jobs_of(path).items():
        step = aws_step(job)
        if step is None:
            continue
        role_of(job)
        assert step["with"]["aws-region"] == "ap-southeast-1", name
        assert str(step["with"].get("mask-aws-account-id")).lower() == "true", name
        assert not ({"aws-access-key-id", "aws-secret-access-key", "aws-session-token"} & set(step["with"])), name


@pytest.mark.parametrize("path", sorted(WORKFLOWS.glob("*.yml")), ids=lambda p: p.name)
def test_no_workflow_reads_aws_keys(path: Path) -> None:
    names = set(re.findall(r"secrets\.([A-Za-z0-9_]+)", path.read_text()))
    assert names <= {"AWS_ACCOUNT_ID", "GITHUB_TOKEN"}, names
    assert not [n for n in names if re.search(r"ACCESS_KEY|SECRET_ACCESS|SESSION_TOKEN", n)]


@pytest.mark.parametrize("path", sorted(WORKFLOWS.glob("*.yml")), ids=lambda p: p.name)
def test_run_steps_never_expand_expressions_inline(path: Path) -> None:
    # An input or event field pasted into a script is a shell injection; through env it is only data.
    for name, job in load(path)["jobs"].items():
        for step in steps(job):
            assert "${{" not in step.get("run", ""), f"{name}: {step.get('name', step['run'][:40])}"


@pytest.mark.parametrize("path", AWS_WORKFLOWS, ids=lambda p: p.name)
def test_checkouts_keep_no_credentials(path: Path) -> None:
    for name, job in jobs_of(path).items():
        for step in steps(job):
            if step.get("uses", "").startswith("actions/checkout@"):
                assert step.get("with", {}).get("persist-credentials") is False, name


@pytest.mark.parametrize("path", sorted(WORKFLOWS.glob("*.yml")), ids=lambda p: p.name)
def test_nothing_is_applied_without_a_plan_to_approve(path: Path) -> None:
    assert "-auto-approve" not in path.read_text()


@pytest.mark.parametrize("path", [TERRAFORM, NIGHTLY], ids=lambda p: p.name)
def test_terraform_runs_only_through_the_stack_script(path: Path) -> None:
    # One shellchecked, tested script holds every Terraform command the workflows run (ci/ephemeral-stack.sh,
    # tested in test_cicd_scripts.py): what is locked, what is printed, what is applied.
    ci_version = next(s["with"]["terraform_version"] for job in jobs_of(CI).values() for s in steps(job)
                      if s.get("uses", "").startswith("hashicorp/setup-terraform@"))
    users = 0
    for name, job in jobs_of(path).items():
        assert not [line for line in runs(job) if re.match(r"terraform\b", line)], f"{name}: Terraform outside the script"
        if not any("ci/ephemeral-stack.sh" in line for line in runs(job)):
            continue
        users += 1
        [setup] = [s for s in steps(job) if s.get("uses", "").startswith("hashicorp/setup-terraform@")]
        assert setup["with"]["terraform_version"] == ci_version, name
        assert setup["with"].get("terraform_wrapper") is False, f"{name}: the wrapper echoes every output"
        assert job.get("env", {}).get("TF_VAR_ssh_public_key") == "${{ vars.SSH_PUBLIC_KEY }}", name
    assert users, "the script is used"


def test_terraform_workflow_plans_pull_requests_and_applies_on_demand() -> None:
    on = load(TERRAFORM)["on"]
    assert "terraform/**" in on["pull_request"]["paths"]
    inputs = on["workflow_dispatch"]["inputs"]
    assert inputs["action"]["type"] == "choice" and inputs["action"]["options"] == ["apply", "destroy"]
    assert inputs["action"]["required"] is True
    assert inputs["instance_type"]["type"] == "choice" and inputs["instance_type"]["options"] == ALLOWLIST
    assert inputs["instance_type"]["default"] == "c7i-flex.large"
    assert inputs["use_spot"]["type"] == "boolean" and inputs["use_spot"]["default"] is True
    assert "concurrency" not in load(TERRAFORM), "a pending PR plan would cancel a pending apply or nightly destroy"


def test_terraform_workflow_jobs_run_in_order_with_their_own_roles() -> None:
    jobs = jobs_of(TERRAFORM)
    assert set(jobs) == {"plan", "comment", "apply"}
    assert "needs" not in jobs["plan"] and jobs["comment"]["needs"] == "plan" and jobs["apply"]["needs"] == "plan"
    assert role_of(jobs["plan"]) == "gha-tf-plan" and "environment" not in jobs["plan"]
    assert role_of(jobs["apply"]) == "gha-tf-apply" and jobs["apply"]["environment"] == "aws-apply"
    assert aws_step(jobs["comment"]) is None
    assert "pull_request" in jobs["comment"]["if"] and "workflow_dispatch" in jobs["apply"]["if"]
    assert "bash ci/ephemeral-stack.sh plan" in runs(jobs["plan"])


def test_the_static_checks_run_once_in_ci() -> None:
    # terraform.yml had its own static job running the same ci/terraform-checks.sh as ci.yml on every PR that
    # touched terraform/ (removed after the C6 review). ci.yml runs them on every PR.
    for path in AWS_WORKFLOWS:
        assert "terraform-checks.sh" not in path.read_text(), path.name
    assert "pull_request" in load(CI)["on"]
    assert "bash ci/terraform-checks.sh" in runs(jobs_of(CI)["infra-checks"])


def test_only_the_comment_job_writes_to_the_pull_request() -> None:
    # The job holding AWS credentials cannot write to GitHub; the job writing the comment has no AWS access.
    jobs = jobs_of(TERRAFORM)
    assert jobs["comment"]["permissions"] == {"pull-requests": "write"}
    for name in ("plan", "apply"):
        assert "pull-requests" not in jobs[name]["permissions"], name
    assert any("gh pr comment" in line for line in runs(jobs["comment"]))


def test_apply_plans_again_and_applies_only_the_approved_changes() -> None:
    # The plan file cannot travel from the plan job: it holds the owner's IP in plain text, and artifacts
    # of a public repo can be downloaded. So the apply job plans again and compares a hash of the changes
    # (address + action) with the one the owner saw before approving (ci/ephemeral-stack.sh apply).
    jobs = jobs_of(TERRAFORM)
    assert jobs["plan"]["outputs"]["hash"] == "${{ steps.plan.outputs.hash }}"
    assert [s.get("id") for s in steps(jobs["plan"]) if "ci/ephemeral-stack.sh" in s.get("run", "")] == ["plan"]
    assert jobs["apply"]["env"]["APPROVED_HASH"] == "${{ needs.plan.outputs.hash }}", "the hash the owner approved"
    assert 'bash ci/ephemeral-stack.sh apply "$APPROVED_HASH"' in runs(jobs["apply"])
    for name in ("plan", "apply"):
        env = jobs[name]["env"]
        assert (env["ACTION"], env["INSTANCE_TYPE"], env["USE_SPOT"]) == (
            "${{ inputs.action }}", "${{ inputs.instance_type }}", "${{ inputs.use_spot }}"), f"{name}: same inputs"


def test_apply_and_the_nightly_destroy_never_run_at_the_same_time() -> None:
    # Same group: one waits for the other. Never cancelled midway: a half-applied stack keeps billing.
    for job in (jobs_of(TERRAFORM)["apply"], jobs_of(NIGHTLY)["destroy"]):
        assert job["concurrency"] == {"group": "aws-ephemeral-state", "cancel-in-progress": False}
    for name in ("plan", "comment"):
        assert "concurrency" not in jobs_of(TERRAFORM)[name], name


def test_deploy_takes_a_full_policypal_commit_and_checks_it_before_any_aws_access() -> None:
    workflow = load(DEPLOY)
    tag = workflow["on"]["workflow_dispatch"]["inputs"]["policypal_tag"]
    assert tag["required"] is True and tag["type"] == "string"
    jobs = workflow["jobs"]
    assert set(jobs) == {"render", "deploy"}
    render, deploy = jobs["render"], jobs["deploy"]
    assert aws_step(render) is None and render["permissions"] == {"contents": "read"}
    assert render["env"]["POLICYPAL_TAG"] == "${{ inputs.policypal_tag }}"
    assert any(line.startswith("bash ci/deploy-render.sh") for line in runs(render))
    assert "manifests" in render["outputs"]
    assert deploy["needs"] == "render" and deploy["environment"] == "aws-deploy"
    assert role_of(deploy) == "gha-deploy"
    assert deploy["concurrency"] == {"group": "aws-deploy", "cancel-in-progress": False}
    assert any(line.startswith("bash ci/deploy-over-ssm.sh") for line in runs(deploy))


def test_the_manifests_are_only_in_the_step_that_sends_them() -> None:
    # GitHub prints a job's env in every step's header: as job env, the encoded YAML filled the log 4 times
    # (live run 45039639528). Not secret (the public repo's manifests), but noise.
    deploy = jobs_of(DEPLOY)["deploy"]
    assert "MANIFESTS" not in deploy.get("env", {})
    holders = [s for s in steps(deploy) if "MANIFESTS" in s.get("env", {})]
    assert len(holders) == 1, "exactly one step gets the manifests"
    [step] = holders
    assert step["env"]["MANIFESTS"] == "${{ needs.render.outputs.manifests }}", "exactly the YAML the render job checked"
    assert "bash ci/deploy-over-ssm.sh policypal.yaml" in step["run"]


def test_nightly_destroy_runs_at_two_in_the_morning_malaysia_time_and_on_demand() -> None:
    on = load(NIGHTLY)["on"]
    assert on["schedule"] == [{"cron": "0 18 * * *"}], "18:00 UTC = 02:00 MYT"
    assert "workflow_dispatch" in on


def test_nightly_destroy_checks_the_skip_switch_before_touching_aws() -> None:
    job = jobs_of(NIGHTLY)["destroy"]
    assert job["environment"] == "aws-nightly" and role_of(job) == "gha-tf-destroy"
    skip = index_of(job, lambda s: "ci/nightly-skip.sh" in s.get("run", ""))
    assert steps(job)[skip]["id"] == "skip"
    assert steps(job)[skip]["env"]["SKIP_UNTIL"] == "${{ vars.NIGHTLY_DESTROY_SKIP_UNTIL }}"
    credentials = index_of(job, lambda s: s.get("uses", "").startswith(CONFIGURE_AWS))
    assert skip < credentials
    for step in steps(job)[skip + 1:]:
        assert step.get("if") == "steps.skip.outputs.skip != 'true'", step.get("name")


def test_nightly_destroy_applies_a_saved_destroy_plan_then_runs_the_cost_check() -> None:
    lines = runs(jobs_of(NIGHTLY)["destroy"])
    destroy = lines.index("bash ci/ephemeral-stack.sh nightly")
    cost = lines.index("bash scripts/cost-check.sh")
    assert destroy < cost
