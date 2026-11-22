"""Tests for scripts/check-state-secrets.sh and make check-state-secrets.

The script pulls the persistent stack's state, reads the live value of each SecureString it lists from SSM,
and counts the state lines that contain that value. It prints names and counts only, and fails when a count
is above 0 or there is nothing to check. A value is never printed and never a command argument (visible in
ps).

They run with the fake CLIs of tests/fakecli.py: terraform answers `state pull` with a state the test
writes, aws answers `ssm get-parameter` with random values made at run time, and the real grep runs through
a spy, so its arguments are in the log too.
"""

import json
import re
import secrets
import subprocess
from typing import Any

import pytest

from conftest import REPO_ROOT
from fakecli import Fakes, flag_value, output

DB_PARAM = "/policypal/aws/db-password"
ADMIN_PARAM = "/policypal/aws/admin-token"


def parameter_state(name: str, kind: str, **attributes: Any) -> dict[str, Any]:
    return {"mode": "managed", "type": "aws_ssm_parameter", "name": name.rsplit("/", 1)[-1],
            "instances": [{"attributes": {"name": name, "type": kind, **attributes}}]}


def persistent_state(*, extra: list[dict[str, Any]] | None = None, secure: bool = True) -> str:
    resources = [parameter_state("/devops-platform/owner-cidr", "String", value="198.51.100.7/32")]
    if secure:
        resources += [parameter_state(n, "SecureString", value=None, value_wo=None, value_wo_version=1)
                      for n in (DB_PARAM, ADMIN_PARAM)]
    return json.dumps({"version": 4, "resources": resources + (extra or [])}, indent=2)


@pytest.fixture
def live_values(fakes: Fakes) -> dict[str, str]:
    """Random values for the two parameters, made at run time, served by the fake aws."""
    values = {DB_PARAM: secrets.token_hex(16), ADMIN_PARAM: secrets.token_hex(16)}
    for name, value in values.items():
        fakes.parameter(name, value)
    fakes.spy("grep")
    return values


def assert_never_shown(fakes: Fakes, result: subprocess.CompletedProcess[str], values: dict[str, str]) -> None:
    # grep runs through the spy, so its arguments are in the log too (they would be visible in ps).
    assert fakes.calls("grep"), "the values must have been compared with grep"
    for value in values.values():
        assert value not in output(result), "a value was printed"
        assert all(value not in " ".join(c["args"]) for c in fakes.calls()), "a value was a command argument"


def test_state_check_reports_zero_for_each_secret_without_showing_it(fakes: Fakes, live_values: dict) -> None:
    fakes.tf_state(persistent_state())
    result = fakes.script("check-state-secrets.sh")
    assert result.returncode == 0, output(result)
    for name in live_values:
        assert re.search(rf"^{re.escape(name)}: 0$", result.stdout, re.M), result.stdout
    read = sorted(flag_value(c["args"], "--name") for c in fakes.calls("aws"))
    assert read == sorted(live_values), "reads exactly the SecureStrings in the state"
    for call in fakes.calls("aws"):
        assert call["args"][:2] == ["ssm", "get-parameter"] and "--with-decryption" in call["args"]
        assert flag_value(call["args"], "--region") == "ap-southeast-1"
    assert [c["args"] for c in fakes.calls("terraform")] == [["state", "pull"]]
    assert_never_shown(fakes, result, live_values)


def test_state_check_fails_when_a_value_is_in_the_state(fakes: Fakes, live_values: dict) -> None:
    leak = {"mode": "managed", "type": "random_password", "name": "token",
            "instances": [{"attributes": {"result": live_values[ADMIN_PARAM]}}]}
    fakes.tf_state(persistent_state(extra=[leak]))
    result = fakes.script("check-state-secrets.sh")
    assert result.returncode == 1
    assert re.search(rf"^{re.escape(ADMIN_PARAM)}: 1$", result.stdout, re.M), result.stdout
    assert re.search(rf"^{re.escape(DB_PARAM)}: 0$", result.stdout, re.M), result.stdout
    assert_never_shown(fakes, result, live_values)


def test_state_check_fails_when_it_has_nothing_to_check(fakes: Fakes, live_values: dict) -> None:
    fakes.tf_state(persistent_state(secure=False))
    result = fakes.script("check-state-secrets.sh")
    assert result.returncode != 0
    assert "SecureString" in output(result)
    assert fakes.calls("terraform"), "the state must have been read"


def test_make_check_state_secrets_reads_the_persistent_stack_as_devops(fakes: Fakes, live_values: dict) -> None:
    fakes.tf_state(persistent_state())
    result = fakes.make("check-state-secrets")
    assert result.returncode == 0, output(result)
    [pull] = fakes.calls("terraform")
    assert pull["cwd"] == str(REPO_ROOT / "terraform" / "persistent")
    assert {c["profile"] for c in fakes.calls("terraform") + fakes.calls("aws")} == {"devops"}
