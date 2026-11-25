"""Tests for scripts/ssm-run.sh: one command on the AWS node through SSM Run Command, and its result.

    ssm-run.sh INSTANCE COMMAND...   send the COMMAND lines (one /bin/sh script, AWS-RunShellScript) to INSTANCE,
                                     wait for them to finish, print the node's output on stdout and its errors on
                                     stderr, exit 0 only if they succeeded on the node

Right after SendCommand, GetCommandInvocation can answer InvocationDoesNotExist for a moment, so that is waited out.
Any other error (an expired login, AccessDenied) stops it at once with AWS's own message: it used to be read as
"still pending" in the restore test, which then waited 20 minutes and said nothing about AWS. ci/deploy-over-ssm.sh
(deploy.yml) and scripts/restore-test.sh both use it. It runs with the fake CLIs of tests/fakecli.py.
"""

import json
import subprocess

import pytest

from conftest import REPO_ROOT
from fakecli import INSTANCE_ID, Fakes, flag_value, output

COMMAND_ID = "11111111-2222-3333-4444-555555555555"
ACCESS_DENIED = ("An error occurred (AccessDeniedException) when calling the GetCommandInvocation operation: "
                 "User: arn:aws:sts::111122223333:assumed-role/x is not authorized\n")
NOT_YET = "An error occurred (InvocationDoesNotExist) when calling the GetCommandInvocation operation\n"


def invocation(status: str, out: str = "", err: str = "") -> tuple[int, str]:
    return 0, json.dumps({"Status": status, "StandardOutputContent": out, "StandardErrorContent": err}) + "\n"


@pytest.fixture
def ssm(fakes: Fakes) -> Fakes:
    fakes.aws("ssm.send-command", COMMAND_ID)
    fakes.aws_sequence("ssm.get-command-invocation", [invocation("Pending"), invocation("InProgress"),
                                                      invocation("Success", "it ran\n")])
    return fakes


def run(fakes: Fakes, *commands: str, **env: str) -> subprocess.CompletedProcess[str]:
    return fakes.script("ssm-run.sh", INSTANCE_ID, *commands, env={"POLL_SECONDS": "0", **env})


def polls(fakes: Fakes) -> list[list[str]]:
    return [c["args"] for c in fakes.calls("aws", "ssm", "get-command-invocation")]


def test_sends_the_commands_as_one_shell_script_to_that_node(ssm: Fakes) -> None:
    assert run(ssm, "set -eu", "echo one").returncode == 0
    [send] = [c["args"] for c in ssm.calls("aws", "ssm", "send-command")]
    assert flag_value(send, "--region") == "ap-southeast-1"
    assert flag_value(send, "--instance-ids") == INSTANCE_ID
    assert flag_value(send, "--document-name") == "AWS-RunShellScript"
    assert json.loads(flag_value(send, "--parameters")) == {"commands": ["set -eu", "echo one"]}
    assert "--comment" not in send


def test_a_comment_and_an_execution_timeout_are_passed_when_given(ssm: Fakes) -> None:
    assert run(ssm, "true", SSM_COMMENT="deploy abc", SSM_EXECUTION_TIMEOUT="1800").returncode == 0
    [send] = [c["args"] for c in ssm.calls("aws", "ssm", "send-command")]
    assert flag_value(send, "--comment") == "deploy abc"
    assert json.loads(flag_value(send, "--parameters"))["executionTimeout"] == ["1800"]


def test_waits_for_the_result_and_prints_only_the_nodes_output_on_stdout(ssm: Fakes) -> None:
    result = run(ssm, "true")
    assert result.returncode == 0, output(result)
    assert result.stdout == "it ran\n", "callers parse it"
    assert len(polls(ssm)) == 3
    assert all(flag_value(p, "--command-id") == COMMAND_ID and flag_value(p, "--instance-id") == INSTANCE_ID
               for p in polls(ssm))


def test_waits_while_the_invocation_is_not_registered_yet(ssm: Fakes) -> None:
    ssm.aws_sequence("ssm.get-command-invocation", [(254, NOT_YET), (254, NOT_YET), invocation("Success", "ok\n")])
    result = run(ssm, "true")
    assert result.returncode == 0, output(result)
    assert len(polls(ssm)) == 3


def test_any_other_aws_error_fails_at_once_and_shows_it(ssm: Fakes) -> None:
    ssm.aws_sequence("ssm.get-command-invocation", [(254, ACCESS_DENIED)])
    result = run(ssm, "true", MAX_WAIT_SECONDS="5")  # bounded even if the error were waited on
    assert result.returncode == 1
    assert len(polls(ssm)) == 1, "no waiting on an error that won't go away"
    assert "AccessDeniedException" in result.stderr


@pytest.mark.parametrize("status", ["Failed", "TimedOut", "Cancelled"])
def test_a_command_that_failed_on_the_node_fails_and_shows_why(ssm: Fakes, status: str) -> None:
    ssm.aws_sequence("ssm.get-command-invocation", [invocation(status, "rolling back\n", "smoke test failed\n")])
    result = run(ssm, "true")
    assert result.returncode == 1
    assert result.stdout == "rolling back\n"
    assert "smoke test failed" in result.stderr and status in result.stderr


def test_the_nodes_errors_go_to_stderr_even_when_it_succeeded(ssm: Fakes) -> None:
    ssm.aws_sequence("ssm.get-command-invocation", [invocation("Success", "done\n", "a warning\n")])
    result = run(ssm, "true")
    assert result.returncode == 0 and result.stdout == "done\n" and "a warning" in result.stderr


def test_gives_up_after_its_time_limit_and_says_how_to_check(ssm: Fakes) -> None:
    ssm.aws_sequence("ssm.get-command-invocation", [invocation("InProgress")])
    result = run(ssm, "true", MAX_WAIT_SECONDS="0")
    assert result.returncode == 1
    assert "gave up" in result.stderr and COMMAND_ID in result.stderr


@pytest.mark.parametrize("args", [[], [INSTANCE_ID]])
def test_usage(fakes: Fakes, args: list[str]) -> None:
    result = fakes.script("ssm-run.sh", *args)
    assert result.returncode == 2 and "usage:" in result.stderr
    assert not fakes.calls("aws")


@pytest.mark.parametrize("script", ["ci/deploy-over-ssm.sh", "scripts/restore-test.sh"])
def test_every_run_command_goes_through_it(script: str) -> None:
    # One place decides what an SSM error means: no second send-and-poll loop.
    text = (REPO_ROOT / script).read_text()
    assert "ssm-run.sh" in text
    assert "send-command" not in text and "get-command-invocation" not in text
