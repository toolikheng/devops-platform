"""Tests for the homelab Make targets that create passwords in the local k3d cluster.

- make secrets-init: the restic password of the v1 backup CronJob (Secret devops/backup-credentials).
- make monitoring-up: Grafana's admin login (Secret monitoring/grafana-admin).

A generated password reaches kubectl on stdin, never as a command-line argument: other users of the machine
can read every process's arguments (ps), the same rule as scripts/policypal-secrets.sh. A Secret that already
exists is left alone, so a running Grafana or an existing backup repository keeps its password. They run with
the fake CLIs of tests/fakecli.py, so no cluster is touched.
"""

import re

import pytest

from fakecli import Fakes, output

# target, namespace, Secret, its fixed keys, the generated key
SECRETS = [
    ("secrets-init", "devops", "backup-credentials", {}, "password"),
    ("monitoring-up", "monitoring", "grafana-admin", {"admin-user": "admin"}, "admin-password"),
]


@pytest.mark.parametrize("target, namespace, name, fixed, generated", SECRETS)
def test_password_reaches_kubectl_on_stdin_only(fakes: Fakes, target: str, namespace: str, name: str,
                                                fixed: dict[str, str], generated: str) -> None:
    result = fakes.make(target)
    assert result.returncode == 0, output(result)
    created = fakes.created_secret(name)
    assert set(created) == {*fixed, generated}, f"{name} must get its keys on stdin, found {sorted(created)}"
    assert {k: created[k] for k in fixed} == fixed
    password = created[generated]
    assert re.fullmatch(r"[0-9a-f]{48}", password), "48 random hex characters (24 bytes)"
    [create] = [c for c in fakes.calls("kubectl") if c["args"][:4] == ["create", "secret", "generic", name]]
    assert create["ns"] == namespace
    for call in fakes.calls():
        assert not any(password in a for a in call["args"]), f"the password is an argument of {call['tool']}"
        assert not any(a.startswith("--from-literal") for a in call["args"]), call["args"]
    assert password not in output(result), "never printed"


@pytest.mark.parametrize("target, namespace, name, fixed, generated", SECRETS)
def test_an_existing_secret_is_left_alone(fakes: Fakes, target: str, namespace: str, name: str,
                                          fixed: dict[str, str], generated: str) -> None:
    fakes.secret_exists(name)
    result = fakes.make(target)
    assert result.returncode == 0, output(result)
    assert not [c for c in fakes.calls("kubectl") if c["args"][:2] == ["create", "secret"]]
    assert (fakes.dir / f"secret-{name}").read_text() == "existing", "the existing Secret must be unchanged"
