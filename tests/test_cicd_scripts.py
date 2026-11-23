"""Tests for the scripts the C6 workflows run (and the one Make target that steers them).

- ci/plan-summary.sh: a Terraform plan as "action address" lines, never attribute values (the repo's logs are
  public), and a hash of those lines; the apply job applies only if the hash matches the approved one.
- ci/ephemeral-stack.sh: every Terraform run of the workflows (plan, apply after approval, nightly destroy):
  what is locked, what reaches the log and the job's outputs, and that only a saved plan is applied.
- ci/nightly-skip.sh: whether tonight's destroy is skipped (repo variable NIGHTLY_DESTROY_SKIP_UNTIL); anything
  unclear means destroy.
- scripts/skip-nightly-destroy.sh (make skip-nightly-destroy): sets that variable, for at most 7 days.
- ci/deploy-render.sh: checks that PolicyPal's image for that commit exists on GHCR (a missing tag fails here,
  before the node is touched and the API goes down), then renders the aws overlay for it and validates it with
  kubeconform: the exact YAML the deploy sends.
- ci/deploy-over-ssm.sh: finds the tagged node, sends it the deploy through SSM Run Command, waits and reports.
The node side (scripts/node-deploy.sh) is in test_policypal_scripts.py with the other deploy scripts.

They run with the fake CLIs of tests/fakecli.py, so no AWS account, cluster or GitHub repo is touched.
"""

import base64
import gzip
import hashlib
import json
import os
import re
import shlex
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from conftest import REPO_ROOT, versions
from fakecli import INSTANCE_ID, MANIFEST, Fakes, flag_value, output

TAG = "16d4edb48b0508a75680831c186d435ae565bccf"
SHA = "0123456789abcdef0123456789abcdef01234567"
REPO = "toolikheng/devops-platform"
COMMAND_ID = "11111111-2222-3333-4444-555555555555"
ISO = "%Y-%m-%dT%H:%M:%SZ"
PLAN = {"resource_changes": [
    {"address": "module.network.aws_vpc.this", "change": {"actions": ["create"], "after": {"cidr_block": "10.20.0.0/16"}}},
    {"address": "module.compute.aws_security_group.node", "change": {"actions": ["update"],
     "after": {"ingress": [{"cidr_blocks": ["198.51.100.7/32"]}]}}},
    {"address": "module.compute.aws_instance.node", "change": {"actions": ["delete", "create"], "after": {}}},
    {"address": "module.compute.aws_key_pair.this", "change": {"actions": ["create", "delete"], "after": {}}},
    {"address": "module.network.aws_subnet.public", "change": {"actions": ["no-op"], "after": {}}},
    {"address": "data.aws_ami.ubuntu", "change": {"actions": ["read"], "after": {}}},
    {"address": "module.network.aws_route.internet", "change": {"actions": ["delete"], "after": None}},
]}
SUMMARY = ("replace module.compute.aws_instance.node\n"
           "replace module.compute.aws_key_pair.this\n"
           "update  module.compute.aws_security_group.node\n"
           "delete  module.network.aws_route.internet\n"
           "create  module.network.aws_vpc.this\n")


def ci(fakes: Fakes, script: str, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["bash", str(REPO_ROOT / "ci" / script), *args], cwd=REPO_ROOT,
                          env={**fakes.env(), **(env or {})}, stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, timeout=120)


# --- ci/plan-summary.sh ---------------------------------------------------------------------------------

@pytest.fixture
def plan(fakes: Fakes, tmp_path: Path) -> Path:
    fakes.tf_show(PLAN)
    path = tmp_path / "tfplan"
    path.write_text("binary plan")
    return path


def test_summary_lists_each_change_as_action_and_address(fakes: Fakes, plan: Path) -> None:
    result = ci(fakes, "plan-summary.sh", str(plan))
    assert result.returncode == 0, output(result)
    assert result.stdout == SUMMARY, "sorted by address; no-op and data reads left out; delete+create = replace"
    [show] = fakes.calls("terraform", "show")
    assert show["args"] == ["show", "-json", str(plan)]


def test_summary_never_prints_attribute_values(fakes: Fakes, plan: Path) -> None:
    # The owner's IP, IDs and every other value stay out of the public log.
    result = ci(fakes, "plan-summary.sh", str(plan))
    assert result.returncode == 0, output(result)
    text = output(result)
    assert "198.51.100.7" not in text and "10.20.0.0" not in text


def test_hash_is_the_sha256_of_the_summary(fakes: Fakes, plan: Path) -> None:
    result = ci(fakes, "plan-summary.sh", "--hash", str(plan))
    assert result.returncode == 0, output(result)
    assert result.stdout.strip() == hashlib.sha256(SUMMARY.encode()).hexdigest()


def test_hash_ignores_order_but_not_actions(fakes: Fakes, plan: Path) -> None:
    first = ci(fakes, "plan-summary.sh", "--hash", str(plan)).stdout
    fakes.tf_show({"resource_changes": list(reversed(PLAN["resource_changes"]))})
    assert ci(fakes, "plan-summary.sh", "--hash", str(plan)).stdout == first
    changed = json.loads(json.dumps(PLAN))
    changed["resource_changes"][0]["change"]["actions"] = ["delete"]
    fakes.tf_show(changed)
    assert ci(fakes, "plan-summary.sh", "--hash", str(plan)).stdout != first


def test_check_passes_when_the_changes_are_the_approved_ones(fakes: Fakes, plan: Path) -> None:
    approved = hashlib.sha256(SUMMARY.encode()).hexdigest()
    result = ci(fakes, "plan-summary.sh", "--check", approved, str(plan))
    assert result.returncode == 0, output(result)
    assert result.stdout == SUMMARY, "the apply log shows what is applied"


def test_check_fails_when_the_changes_differ_from_the_approved_ones(fakes: Fakes, plan: Path) -> None:
    result = ci(fakes, "plan-summary.sh", "--check", "0" * 64, str(plan))
    assert result.returncode == 1
    assert "changed since" in output(result)


def test_no_changes_is_an_empty_summary_with_a_stable_hash(fakes: Fakes, plan: Path) -> None:
    fakes.tf_show({"resource_changes": [{"address": "a.b", "change": {"actions": ["no-op"]}}]})
    assert ci(fakes, "plan-summary.sh", str(plan)).stdout == ""
    empty = hashlib.sha256(b"").hexdigest()
    assert ci(fakes, "plan-summary.sh", "--hash", str(plan)).stdout.strip() == empty
    fakes.tf_show({})  # a plan with nothing at all has no resource_changes key
    assert ci(fakes, "plan-summary.sh", "--check", empty, str(plan)).returncode == 0


@pytest.mark.parametrize("args", [[], ["--hash"], ["--check", "abc"], ["--check", "0" * 64], ["/no/such/plan"]])
def test_summary_needs_a_plan_file(fakes: Fakes, args: list[str]) -> None:
    result = ci(fakes, "plan-summary.sh", *args)
    assert result.returncode == 2
    assert "usage" in output(result)
    assert not fakes.calls("terraform")


def test_a_plan_terraform_cannot_read_is_an_error(fakes: Fakes, plan: Path) -> None:
    fakes.fail("terraform", "show")
    result = ci(fakes, "plan-summary.sh", "--hash", str(plan))
    assert result.returncode != 0
    assert fakes.calls("terraform", "show"), "the plan was read, and the failure was not ignored"
    assert not re.fullmatch(r"[0-9a-f]{64}\n?", result.stdout), "no hash of an empty summary"


# --- ci/ephemeral-stack.sh -----------------------------------------------------------------------------

STACK_DIR = str(REPO_ROOT / "terraform" / "envs" / "aws")
FULL_PLAN = "Terraform will perform the following actions ... cidr_blocks = [\"198.51.100.7/32\"]"


@pytest.fixture
def stack(fakes: Fakes, tmp_path: Path) -> dict[str, str]:
    """A plan with the PLAN changes; terraform plan/apply print the full plan (values included), as the real
    ones do. Returns the GitHub Actions files the script may write."""
    fakes.tf_show(PLAN)
    fakes.tf_says(FULL_PLAN)
    files = {"GITHUB_OUTPUT": str(tmp_path / "output"), "GITHUB_STEP_SUMMARY": str(tmp_path / "summary")}
    for path in files.values():
        Path(path).write_text("")
    return files


def run_stack(fakes: Fakes, stack: dict[str, str], *args: str, **env: str) -> subprocess.CompletedProcess[str]:
    return ci(fakes, "ephemeral-stack.sh", *args, env={**stack, **env})


def terraform_calls(fakes: Fakes) -> list[list[str]]:
    return [c["args"] for c in fakes.calls("terraform")]


def test_plan_reads_only_and_never_takes_the_lock(fakes: Fakes, stack: dict[str, str]) -> None:
    result = run_stack(fakes, stack, "plan")
    assert result.returncode == 0, output(result)
    calls = fakes.calls("terraform")
    assert {c["cwd"] for c in calls} == {STACK_DIR}
    assert [c["args"][0] for c in calls] == ["init", "plan", "show", "show"]
    plan = calls[1]["args"]
    assert "-lock=false" in plan and "-destroy" not in plan, "gha-tf-plan can't write the lockfile"
    [out] = [Path(calls[1]["cwd"]) / a.split("=", 1)[1] for a in plan if a.startswith("-out=")]
    assert STACK_DIR not in str(out.resolve().parent) and not out.exists(), "a temporary plan file, removed afterwards"
    assert not [a for a in plan if a == "-var"], "a pull request plans the stack's defaults"


def test_plan_shows_the_changes_and_hash_in_the_log_summary_and_outputs(fakes: Fakes, stack: dict[str, str]) -> None:
    # The log too: GitHub's API can't read job summaries (C6 live #2).
    result = run_stack(fakes, stack, "plan")
    digest = hashlib.sha256(SUMMARY.encode()).hexdigest()
    for text in (result.stdout, Path(stack["GITHUB_STEP_SUMMARY"]).read_text()):
        assert SUMMARY.strip() in text and digest in text
    out = Path(stack["GITHUB_OUTPUT"]).read_text()
    assert f"hash={digest}\n" in out
    assert re.search(r"summary<<(\w+)\n" + re.escape(SUMMARY.strip()) + r"\n\1\n", out), "for the PR comment"


def test_the_full_plan_never_reaches_the_log(fakes: Fakes, stack: dict[str, str]) -> None:
    # The public log: addresses, actions and the hash only. The real terraform prints values (the owner's IP).
    for args in (["plan"], ["nightly"]):
        result = run_stack(fakes, stack, *args)
        assert result.returncode == 0 and "198.51.100.7" not in output(result), args


def test_a_manual_destroy_and_the_inputs_reach_the_plan(fakes: Fakes, stack: dict[str, str]) -> None:
    result = run_stack(fakes, stack, "plan", ACTION="destroy", INSTANCE_TYPE="t3.micro", USE_SPOT="false")
    assert result.returncode == 0, output(result)
    [plan] = [a for a in terraform_calls(fakes) if a[0] == "plan"]
    assert "-destroy" in plan
    assert [plan[i + 1] for i, a in enumerate(plan) if a == "-var"] == ["instance_type=t3.micro", "use_spot=false"]


@pytest.mark.parametrize("args, env", [([], {}), (["deploy"], {}), (["plan", "extra"], {}), (["apply"], {}),
                                       (["apply", "abc"], {}), (["plan"], {"ACTION": "recreate"})])
def test_the_stack_script_takes_only_its_three_verbs(fakes: Fakes, stack: dict[str, str], args: list[str],
                                                      env: dict[str, str]) -> None:
    result = run_stack(fakes, stack, *args, **env)
    assert result.returncode == 2 and "usage" in output(result)
    assert not fakes.calls("terraform")


def test_apply_plans_again_and_applies_the_saved_plan_when_the_hash_matches(fakes: Fakes, stack: dict[str, str]) -> None:
    # The hash comes from the plan job, as in terraform.yml.
    assert run_stack(fakes, stack, "plan").returncode == 0
    approved = re.search(r"^hash=(\w+)$", Path(stack["GITHUB_OUTPUT"]).read_text(), re.M).group(1)
    fakes.log.write_text("")
    result = run_stack(fakes, stack, "apply", approved, ACTION="destroy")
    assert result.returncode == 0, output(result)
    calls = terraform_calls(fakes)
    assert [a[0] for a in calls] == ["init", "plan", "show", "apply"]
    plan, apply = calls[1], calls[3]
    assert "-destroy" in plan and "-lock=false" not in plan and any(a.startswith("-lock-timeout=") for a in plan)
    [out] = [a.split("=", 1)[1] for a in plan if a.startswith("-out=")]
    assert apply == ["apply", "-input=false", out], "the saved plan, nothing else"
    assert SUMMARY.strip() in result.stdout, "the log shows what is applied"


def test_apply_stops_when_the_changes_are_not_the_approved_ones(fakes: Fakes, stack: dict[str, str]) -> None:
    result = run_stack(fakes, stack, "apply", "0" * 64)
    assert result.returncode == 1
    assert "changed since" in output(result)
    assert not [a for a in terraform_calls(fakes) if a[0] == "apply"]


def test_nightly_plans_the_destroy_and_applies_that_plan(fakes: Fakes, stack: dict[str, str]) -> None:
    result = run_stack(fakes, stack, "nightly", ACTION="apply", INSTANCE_TYPE="c7i-flex.large")
    assert result.returncode == 0, output(result)
    calls = terraform_calls(fakes)
    assert [a[0] for a in calls] == ["init", "plan", "show", "apply"]
    plan = calls[1]
    assert "-destroy" in plan and "-var" not in plan, "always a destroy, whatever the env"
    assert any(a.startswith("-lock-timeout=") for a in plan), "waits for a laptop run instead of failing"
    [out] = [a.split("=", 1)[1] for a in plan if a.startswith("-out=")]
    assert calls[3] == ["apply", "-input=false", out]
    for text in (result.stdout, Path(stack["GITHUB_STEP_SUMMARY"]).read_text()):
        assert SUMMARY.strip() in text


@pytest.mark.parametrize("verb", ["plan", "nightly"])
def test_a_failed_plan_stops_everything(fakes: Fakes, stack: dict[str, str], verb: str) -> None:
    fakes.fail("terraform", "plan")
    result = run_stack(fakes, stack, verb)
    assert result.returncode != 0
    assert [a[0] for a in terraform_calls(fakes)] == ["init", "plan"]
    assert Path(stack["GITHUB_OUTPUT"]).read_text() == ""


def test_nothing_is_ever_auto_approved(fakes: Fakes, stack: dict[str, str]) -> None:
    run_stack(fakes, stack, "nightly")
    assert terraform_calls(fakes) and not [a for a in terraform_calls(fakes) if "-auto-approve" in a]
    assert "-auto-approve" not in (REPO_ROOT / "ci" / "ephemeral-stack.sh").read_text()


# --- ci/nightly-skip.sh ---------------------------------------------------------------------------------

def skip(fakes: Fakes, tmp_path: Path, until: str | None) -> tuple[subprocess.CompletedProcess[str], str]:
    out = tmp_path / "github-output"
    out.write_text("")
    env = {"GITHUB_OUTPUT": str(out)}
    if until is not None:
        env["SKIP_UNTIL"] = until
    result = ci(fakes, "nightly-skip.sh", env=env)
    return result, out.read_text()


def at(hours: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).strftime(ISO)


@pytest.mark.parametrize("until", [None, ""])
def test_no_skip_set_means_destroy(fakes: Fakes, tmp_path: Path, until: str | None) -> None:
    result, out = skip(fakes, tmp_path, until)
    assert result.returncode == 0, output(result)
    assert out == "skip=false\n"


def test_a_skip_time_in_the_future_skips_tonight(fakes: Fakes, tmp_path: Path) -> None:
    until = at(5)
    result, out = skip(fakes, tmp_path, until)
    assert result.returncode == 0 and out == "skip=true\n"
    assert until in output(result), "the log says until when"


def test_a_skip_time_that_has_passed_means_destroy(fakes: Fakes, tmp_path: Path) -> None:
    result, out = skip(fakes, tmp_path, at(-1))
    assert result.returncode == 0 and out == "skip=false\n"


@pytest.mark.parametrize("until", ["tomorrow", "2099-01-01", "2099-01-01 00:00:00", "next week", "2099-13-01T00:00:00Z"])
def test_an_unclear_skip_time_means_destroy_with_a_warning(fakes: Fakes, tmp_path: Path, until: str) -> None:
    # date -d would read "tomorrow" or "next week" happily; only one exact UTC format counts.
    result, out = skip(fakes, tmp_path, until)
    assert result.returncode == 0 and out == "skip=false\n"
    assert "::warning::" in output(result)


def test_the_decision_is_printed_without_github_output(fakes: Fakes) -> None:
    result = ci(fakes, "nightly-skip.sh", env={"SKIP_UNTIL": at(5)})
    assert result.returncode == 0 and "skip" in output(result).lower()


# --- scripts/skip-nightly-destroy.sh, make skip-nightly-destroy -----------------------------------------

def variable_sets(fakes: Fakes) -> list[list[str]]:
    return [c["args"] for c in fakes.calls("gh", "variable", "set")]


def test_skip_defaults_to_the_next_8_hours(fakes: Fakes) -> None:
    # One working session. A longer default set in the morning would also skip that night's destroy, and a
    # forgotten stack would keep billing; anything longer is asked for with UNTIL.
    result = fakes.script("skip-nightly-destroy.sh")
    assert result.returncode == 0, output(result)
    [args] = variable_sets(fakes)
    assert args[:3] == ["variable", "set", "NIGHTLY_DESTROY_SKIP_UNTIL"]
    until = datetime.strptime(flag_value(args, "--body"), ISO).replace(tzinfo=timezone.utc)
    assert abs(until - (datetime.now(timezone.utc) + timedelta(hours=8))) < timedelta(minutes=5)
    assert "make cloud-down" in output(result), "reminds the owner that the stack keeps billing"


def test_skip_until_a_given_time(fakes: Fakes) -> None:
    until = at(30)
    result = fakes.script("skip-nightly-destroy.sh", until)
    assert result.returncode == 0, output(result)
    assert flag_value(variable_sets(fakes)[0], "--body") == until


@pytest.mark.parametrize("until", ["tomorrow", "2099-01-01", "past", "long"])
def test_skip_refuses_unclear_past_or_long_skips(fakes: Fakes, until: str) -> None:
    # More than 7 days could leave a forgotten node billing for a week.
    value = {"past": at(-2), "long": at(24 * 8)}.get(until, until)
    result = fakes.script("skip-nightly-destroy.sh", value)
    assert result.returncode == 2
    assert not fakes.calls("gh")


def test_skip_fails_when_github_refuses(fakes: Fakes) -> None:
    fakes.fail("gh", "variable", "set")
    assert fakes.script("skip-nightly-destroy.sh").returncode != 0
    assert variable_sets(fakes), "it tried"


def test_make_skip_nightly_destroy(fakes: Fakes) -> None:
    until = at(12)
    result = fakes.make("skip-nightly-destroy", f"UNTIL={until}")
    assert result.returncode == 0, output(result)
    assert flag_value(variable_sets(fakes)[0], "--body") == until
    assert not fakes.calls("aws") and not fakes.calls("terraform"), "a GitHub setting, not an AWS change"


def test_the_workflow_reads_the_variable_the_script_sets() -> None:
    script = (REPO_ROOT / "scripts" / "skip-nightly-destroy.sh").read_text()
    workflow = (REPO_ROOT / ".github" / "workflows" / "nightly-destroy.yml").read_text()
    assert "NIGHTLY_DESTROY_SKIP_UNTIL" in script and "vars.NIGHTLY_DESTROY_SKIP_UNTIL" in workflow


# --- ci/deploy-render.sh --------------------------------------------------------------------------------

GHCR_TOKEN_URL = "https://ghcr.io/token?scope=repository:toolikheng/policypal:pull"
GHCR_TOKEN = "registry-token-for-this-test"
AWS_OVERLAY = REPO_ROOT / "k8s" / "policypal" / "overlays" / "aws" / "kustomization.yaml"


def manifest_url(tag: str) -> str:
    return f"https://ghcr.io/v2/toolikheng/policypal/manifests/{tag}"


def ghcr(fakes: Fakes, tag: str = TAG, status: int = 200) -> None:
    """GHCR as an anonymous client sees it: a pull token, then the tag's manifest (200) or not (404)."""
    fakes.http(GHCR_TOKEN_URL, json.dumps({"token": GHCR_TOKEN}))
    fakes.http(manifest_url(tag), "", status=status)


def render(fakes: Fakes, out: Path, tag: str = TAG) -> subprocess.CompletedProcess[str]:
    return ci(fakes, "deploy-render.sh", tag, str(out))


@pytest.mark.parametrize("tag", ["", "latest", TAG[:39], TAG.upper(), TAG + "0", "16d4edb4;rm -rf /"])
def test_render_takes_only_a_full_lowercase_commit(fakes: Fakes, tmp_path: Path, tag: str) -> None:
    result = ci(fakes, "deploy-render.sh", tag, str(tmp_path / "policypal.yaml"))
    assert result.returncode == 2
    assert not fakes.calls("docker") and not fakes.calls("curl")


def test_render_checks_the_image_exists_on_ghcr_before_anything_else(fakes: Fakes, tmp_path: Path) -> None:
    ghcr(fakes)
    result = render(fakes, tmp_path / "policypal.yaml")
    assert result.returncode == 0, output(result)
    calls = fakes.calls()
    token = next(i for i, c in enumerate(calls) if c["tool"] == "curl" and GHCR_TOKEN_URL in c["args"])
    head = next(i for i, c in enumerate(calls) if c["tool"] == "curl" and manifest_url(TAG) in c["args"])
    first_docker = next(i for i, c in enumerate(calls) if c["tool"] == "docker")
    assert token < head < first_docker, "check first, render after"
    args = calls[head]["args"]
    assert "-I" in args, "a HEAD request: no image is downloaded"
    headers = [args[i + 1] for i, a in enumerate(args) if a == "-H"]
    assert f"Authorization: Bearer {GHCR_TOKEN}" in headers
    accept = next(h for h in headers if h.startswith("Accept:"))
    for media_type in ("application/vnd.oci.image.index.v1+json", "application/vnd.docker.distribution.manifest.list.v2+json",
                       "application/vnd.oci.image.manifest.v1+json", "application/vnd.docker.distribution.manifest.v2+json"):
        assert media_type in accept, f"PolicyPal's CI could publish either format: {media_type}"


def test_a_tag_missing_on_ghcr_stops_before_anything_is_rendered(fakes: Fakes, tmp_path: Path) -> None:
    # The broken-commit deploy (live run 45040160686) took the API down for 15 minutes before the rollback.
    # A tag that doesn't exist now fails here, in seconds, and nothing reaches the node.
    zeros = "0" * 40
    ghcr(fakes, zeros, status=404)
    out = tmp_path / "policypal.yaml"
    result = render(fakes, out, zeros)
    assert result.returncode == 1
    assert "not on GHCR" in output(result) and zeros in output(result)
    assert not fakes.calls("docker") and not out.exists()


@pytest.mark.parametrize("trouble", ["token", "registry error", "unreachable"])
def test_render_stops_when_ghcr_cannot_be_asked(fakes: Fakes, tmp_path: Path, trouble: str) -> None:
    # Fail closed: an unknown answer is not a yes.
    ghcr(fakes)
    if trouble == "token":
        fakes.http(GHCR_TOKEN_URL, "", rc=22)
    elif trouble == "registry error":
        fakes.http(manifest_url(TAG), "", status=503)
    else:
        fakes.http(manifest_url(TAG), None)
    result = render(fakes, tmp_path / "policypal.yaml")
    assert result.returncode == 1
    assert "could not check" in output(result)
    assert not fakes.calls("docker")


def test_the_registry_token_is_never_printed(fakes: Fakes, tmp_path: Path) -> None:
    ghcr(fakes)
    result = render(fakes, tmp_path / "policypal.yaml")
    assert result.returncode == 0 and fakes.calls("curl"), "the token was fetched and used"
    assert GHCR_TOKEN not in output(result)


@pytest.mark.parametrize("script", ["ci/deploy-render.sh", "scripts/render-k8s.sh"])
def test_the_scripts_name_the_image_the_overlay_deploys(script: str) -> None:
    # deploy-render.sh asks GHCR about this image and render-k8s.sh sets its tag; the overlay names it.
    [image] = [i["name"] for i in yaml.safe_load(AWS_OVERLAY.read_text())["images"]]
    match = re.search(r'^IMAGE="([^"]+)"$', (REPO_ROOT / script).read_text(), re.M)
    assert match and match.group(1) == image


def test_render_writes_the_aws_overlay_and_validates_that_exact_file(fakes: Fakes, tmp_path: Path) -> None:
    ghcr(fakes)
    out = tmp_path / "policypal.yaml"
    result = render(fakes, out)
    assert result.returncode == 0, output(result)
    assert out.read_text() == MANIFEST
    docker = fakes.calls("docker")
    [render_call] = [c for c in docker if any("kustomize" in a for a in c["args"])]
    # The commit's tag goes in through render-k8s.sh's wrapper; test_policypal_scripts.py renders it for real
    # and compares it with the aws overlay.
    assert render_call["args"][-1] == "/tag" and f"{REPO_ROOT / 'k8s'}:/k8s:ro" in render_call["args"]
    [validation] = [c for c in docker if any("kubeconform" in a for a in c["args"])]
    assert "-strict" in validation["args"]
    assert flag_value(validation["args"], "-kubernetes-version") == versions()["KUBERNETES_VERSION"]
    assert validation["stdin"] == out.read_text(), "kubeconform checks the file that is deployed"


def test_render_fails_when_kubeconform_does(fakes: Fakes, tmp_path: Path) -> None:
    ghcr(fakes)
    fakes.fail("docker", "run", "--rm", "-i")
    assert render(fakes, tmp_path / "policypal.yaml").returncode != 0
    assert [c for c in fakes.calls("docker") if any("kubeconform" in a for a in c["args"])], "it validated"


# --- ci/deploy-over-ssm.sh ------------------------------------------------------------------------------

def invocation(status: str, out: str = "", err: str = "") -> tuple[int, str]:
    return 0, json.dumps({"Status": status, "StandardOutputContent": out, "StandardErrorContent": err}) + "\n"


@pytest.fixture
def node(fakes: Fakes, tmp_path: Path) -> Path:
    """One running node; the deploy on it succeeds after two polls. Returns the manifest file to send."""
    fakes.aws("ec2.describe-instances", INSTANCE_ID)
    fakes.aws("ssm.send-command", COMMAND_ID)
    fakes.aws_sequence("ssm.get-command-invocation", [
        invocation("InProgress"), invocation("InProgress"),
        invocation("Success", "deploy-policypal: aws deployed and smoke-tested\n")])
    manifests = tmp_path / "policypal.yaml"
    manifests.write_text(MANIFEST + "---\n# a second document\n")
    return manifests


def deploy(fakes: Fakes, manifests: Path, **env: str) -> subprocess.CompletedProcess[str]:
    return ci(fakes, "deploy-over-ssm.sh", str(manifests),
              env={"GITHUB_REPOSITORY": REPO, "GITHUB_SHA": SHA, "POLL_SECONDS": "0", **env})


def sent_commands(fakes: Fakes) -> list[str]:
    [send] = fakes.calls("aws", "ssm", "send-command")
    return json.loads(flag_value(send["args"], "--parameters"))["commands"]


def test_deploy_finds_the_one_running_node_by_its_tags(fakes: Fakes, node: Path) -> None:
    result = deploy(fakes, node)
    assert result.returncode == 0, output(result)
    [find] = fakes.calls("aws", "ec2", "describe-instances")
    args = find["args"]
    assert flag_value(args, "--region") == "ap-southeast-1"
    filters = args[args.index("--filters") + 1:]
    for wanted in ("Name=tag:Project,Values=devops-platform", "Name=tag:Stack,Values=ephemeral",
                   "Name=instance-state-name,Values=running"):
        assert wanted in filters


@pytest.mark.parametrize("found", ["", f"{INSTANCE_ID}\ti-0fedcba9876543210"])
def test_deploy_stops_unless_exactly_one_node_runs(fakes: Fakes, node: Path, found: str) -> None:
    fakes.aws("ec2.describe-instances", found)
    result = deploy(fakes, node)
    assert result.returncode == 1
    assert "node" in output(result)
    assert not fakes.calls("aws", "ssm")


def test_deploy_runs_a_shell_script_on_that_node_only(fakes: Fakes, node: Path) -> None:
    deploy(fakes, node)
    [send] = fakes.calls("aws", "ssm", "send-command")
    args = send["args"]
    assert flag_value(args, "--region") == "ap-southeast-1"
    assert flag_value(args, "--instance-ids") == INSTANCE_ID
    assert flag_value(args, "--document-name") == "AWS-RunShellScript"
    assert len(flag_value(args, "--comment")) <= 100, "SendCommand's limit"
    parameters = json.loads(flag_value(args, "--parameters"))
    assert parameters["executionTimeout"] == ["1800"], "longer than the 15-minute rollout wait"


def test_the_node_gets_this_commit_of_the_repo_and_exactly_the_checked_yaml(fakes: Fakes, node: Path) -> None:
    result = deploy(fakes, node)
    assert result.returncode == 0, output(result)
    text = "\n".join(sent_commands(fakes))
    assert f"https://github.com/{REPO}.git" in text
    assert SHA in text
    [payload] = re.findall(r"'([A-Za-z0-9+/=]{40,})'", text)
    assert gzip.decompress(base64.b64decode(payload)) == node.read_bytes(), "the very bytes kubeconform checked"
    assert re.search(r'bash "?\S*/scripts/node-deploy\.sh"? \S+', text)


def test_the_node_script_is_plain_sh(fakes: Fakes, node: Path) -> None:
    # AWS-RunShellScript runs the commands with sh (dash on Ubuntu): no pipefail, no [[ ]], and HOME set
    # (the agent's environment has none, and git needs it).
    deploy(fakes, node)
    commands = sent_commands(fakes)
    assert commands[0] == "set -eu"
    text = "\n".join(commands)
    assert "pipefail" not in text and "[[" not in text
    assert re.search(r"export HOME=", text)
    check = subprocess.run(["sh", "-n"], input=text, capture_output=True, text=True)
    assert check.returncode == 0, check.stderr


def test_deploy_waits_for_the_result_and_shows_the_nodes_output(fakes: Fakes, node: Path) -> None:
    result = deploy(fakes, node)
    assert result.returncode == 0, output(result)
    polls = fakes.calls("aws", "ssm", "get-command-invocation")
    assert len(polls) == 3
    assert all(flag_value(p["args"], "--command-id") == COMMAND_ID and
               flag_value(p["args"], "--instance-id") == INSTANCE_ID for p in polls)
    assert "aws deployed and smoke-tested" in output(result)


def test_deploy_waits_while_the_command_is_not_registered_yet(fakes: Fakes, node: Path) -> None:
    # Right after SendCommand, GetCommandInvocation can answer InvocationDoesNotExist for a moment.
    fakes.aws_sequence("ssm.get-command-invocation", [
        (254, "An error occurred (InvocationDoesNotExist) when calling the GetCommandInvocation operation\n"),
        invocation("Success", "ok\n")])
    assert deploy(fakes, node).returncode == 0


@pytest.mark.parametrize("status", ["Failed", "TimedOut", "Cancelled"])
def test_a_failed_deploy_on_the_node_fails_the_job_and_shows_why(fakes: Fakes, node: Path, status: str) -> None:
    fakes.aws_sequence("ssm.get-command-invocation", [
        invocation(status, "rolling back\n", "deploy-policypal: smoke test failed\n")])
    result = deploy(fakes, node)
    assert result.returncode == 1
    assert "smoke test failed" in output(result) and "rolling back" in output(result)


def test_deploy_gives_up_after_its_time_limit(fakes: Fakes, node: Path) -> None:
    fakes.aws_sequence("ssm.get-command-invocation", [invocation("InProgress")])
    result = deploy(fakes, node, MAX_WAIT_SECONDS="0")
    assert result.returncode == 1
    assert "gave up" in output(result)


@pytest.mark.parametrize("env", [
    {"GITHUB_SHA": "master"}, {"GITHUB_SHA": SHA[:39]}, {"GITHUB_SHA": ""},
    {"GITHUB_REPOSITORY": "toolikheng/devops-platform;reboot"}, {"GITHUB_REPOSITORY": ""},
])
def test_deploy_refuses_anything_but_a_repo_name_and_a_full_commit(fakes: Fakes, node: Path, env: dict) -> None:
    # Both end up in the node's shell script.
    assert deploy(fakes, node, **env).returncode == 2
    assert not fakes.calls("aws")


def test_deploy_needs_the_manifest_file(fakes: Fakes, node: Path, tmp_path: Path) -> None:
    assert deploy(fakes, tmp_path / "missing.yaml").returncode == 2
    assert not fakes.calls("aws")


def test_deploy_refuses_a_payload_too_large_for_run_command(fakes: Fakes, node: Path) -> None:
    # No AWS quota is documented for Run Command parameters, so the script keeps its own ceiling (half the
    # 64 KB document limit) and says what to do instead of failing in AWS.
    node.write_bytes(base64.b64encode(os.urandom(30000)))  # does not compress: about 40 KB encoded
    result = deploy(fakes, node)
    assert result.returncode == 2
    assert "too large" in output(result)
    assert not fakes.calls("aws")


def test_the_real_payload_fits_easily(rendered: dict) -> None:
    # The aws overlay as it is today: about 2.4 KB encoded against a 32 KB ceiling.
    text = yaml.safe_dump_all(rendered["aws"])
    assert len(base64.b64encode(gzip.compress(text.encode(), 9))) < 32768 // 4


def test_deploy_never_prints_the_payload(fakes: Fakes, node: Path) -> None:
    text = output(deploy(fakes, node))
    [payload] = re.findall(r"'([A-Za-z0-9+/=]{40,})'", "\n".join(sent_commands(fakes)))
    assert payload[:40] not in text


def test_deploy_command_quotes_survive_the_shell(fakes: Fakes, node: Path) -> None:
    # Every value in the commands is a fixed pattern (validated) or single-quoted base64: shlex can split them.
    deploy(fakes, node)
    for line in sent_commands(fakes):
        shlex.split(line)
