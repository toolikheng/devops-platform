"""Tests for the scripts deploy.yml runs: a PolicyPal commit deployed on the AWS node.

- ci/deploy-render.sh: the aws overlay for one PolicyPal commit, validated with kubeconform: the exact YAML
  the deploy sends.
- ci/deploy-over-ssm.sh: finds the tagged node, sends it the deploy through SSM Run Command, waits and reports.
The node side (scripts/node-deploy.sh) is in test_policypal_scripts.py with the other deploy scripts.

They run with the fake CLIs of tests/fakecli.py, so no AWS account or cluster is touched.
"""

import base64
import gzip
import json
import os
import re
import shlex
import subprocess
from pathlib import Path

import pytest
import yaml

from conftest import REPO_ROOT, versions
from fakecli import INSTANCE_ID, MANIFEST, Fakes, flag_value, output

TAG = "16d4edb48b0508a75680831c186d435ae565bccf"
SHA = "0123456789abcdef0123456789abcdef01234567"
REPO = "toolikheng/devops-platform"
COMMAND_ID = "11111111-2222-3333-4444-555555555555"


def ci(fakes: Fakes, script: str, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["bash", str(REPO_ROOT / "ci" / script), *args], cwd=REPO_ROOT,
                          env={**fakes.env(), **(env or {})}, stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, timeout=120)


# --- ci/deploy-render.sh --------------------------------------------------------------------------------

@pytest.mark.parametrize("tag", ["", "latest", TAG[:39], TAG.upper(), TAG + "0", "16d4edb4;rm -rf /"])
def test_render_takes_only_a_full_lowercase_commit(fakes: Fakes, tmp_path: Path, tag: str) -> None:
    result = ci(fakes, "deploy-render.sh", tag, str(tmp_path / "policypal.yaml"))
    assert result.returncode == 2
    assert not fakes.calls("docker")


def test_render_writes_the_aws_overlay_and_validates_that_exact_file(fakes: Fakes, tmp_path: Path) -> None:
    out = tmp_path / "policypal.yaml"
    result = ci(fakes, "deploy-render.sh", TAG, str(out))
    assert result.returncode == 0, output(result)
    assert out.read_text() == MANIFEST
    docker = fakes.calls("docker")
    [render] = [c for c in docker if any("kustomize" in a for a in c["args"])]
    assert render["args"][-1].endswith("/policypal/overlays/aws")
    [validation] = [c for c in docker if any("kubeconform" in a for a in c["args"])]
    assert "-strict" in validation["args"]
    assert flag_value(validation["args"], "-kubernetes-version") == versions()["KUBERNETES_VERSION"]
    assert validation["stdin"] == out.read_text(), "kubeconform checks the file that is deployed"


def test_render_fails_when_kubeconform_does(fakes: Fakes, tmp_path: Path) -> None:
    fakes.fail("docker", "run", "--rm", "-i")
    assert ci(fakes, "deploy-render.sh", TAG, str(tmp_path / "policypal.yaml")).returncode != 0
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
