"""Tests for the AWS session targets `make owner-ip`, `cloud-up`, `cloud-down`, `cloud-config`,
`cloud-tunnel` and `cloud-rebuild`.

They run the real Make targets with the fake CLIs of tests/fakecli.py first on PATH (terraform, aws, curl,
ansible-playbook among them), so no AWS call is made and nothing is configured. Every call is logged with the
directory it ran in, the AWS profile and whether Terraform got the SSH public key, which shows what ran,
where, and in which order.
"""

import pytest
import yaml

from conftest import REPO_ROOT
from fakecli import CHECKIP, INSTANCE_ID, PUBLIC_KEY, Fakes, flag_value

EPHEMERAL_DIR = REPO_ROOT / "terraform" / "envs" / "aws"
ANSIBLE_DIR = REPO_ROOT / "ansible"


# --- make owner-ip --------------------------------------------------------------------------------------

def test_owner_ip_writes_this_machines_ip_as_a_32(fakes: Fakes, terraform_names: dict[str, str]) -> None:
    result = fakes.make("owner-ip")
    assert result.returncode == 0, result.stderr

    curl = fakes.calls("curl")
    assert len(curl) == 1 and CHECKIP in curl[0]["args"]

    puts = fakes.calls("aws", "ssm", "put-parameter")
    assert len(puts) == 1
    args = puts[0]["args"]
    assert flag_value(args, "--name") == terraform_names["owner_cidr_parameter"]
    assert flag_value(args, "--type") == "String"
    assert flag_value(args, "--value") == "203.0.113.10/32"
    assert "--overwrite" in args
    assert puts[0]["profile"] == "devops"


@pytest.mark.parametrize("answer", ["", "<html>error</html>", "203.0.113.300", "2001:db8::1", "1.2.3"])
def test_owner_ip_refuses_anything_but_an_ipv4_address(fakes: Fakes, answer: str) -> None:
    fakes.http(CHECKIP, answer + "\n")
    result = fakes.make("owner-ip")
    assert len(fakes.calls("curl")) == 1, "the target must run and ask checkip"
    assert result.returncode != 0
    assert not fakes.calls("aws", "ssm", "put-parameter")


# --- make cloud-up --------------------------------------------------------------------------------------

def test_cloud_up_applies_the_ephemeral_stack_interactively(fakes: Fakes) -> None:
    result = fakes.make("cloud-up")
    assert result.returncode == 0, result.stderr

    tf = fakes.calls("terraform")
    assert [c["args"][0] for c in tf] == ["init", "apply"]
    assert all(c["cwd"] == str(EPHEMERAL_DIR) for c in tf)
    assert all(c["profile"] == "devops" for c in tf)
    assert tf[1]["ssh_key"], "the SSH public key must reach Terraform"


def test_cloud_up_needs_the_ssh_public_key(fakes: Fakes) -> None:
    (fakes.home / ".ssh" / "id_ed25519.pub").unlink()
    result = fakes.make("cloud-up")
    assert result.returncode != 0
    assert not fakes.calls("terraform")
    assert "id_ed25519.pub" in result.stdout + result.stderr


def test_cloud_up_passes_extra_terraform_arguments(fakes: Fakes) -> None:
    result = fakes.make("cloud-up", "CLOUD_ARGS=-var use_spot=true")
    assert result.returncode == 0, result.stderr
    assert fakes.calls("terraform")[-1]["args"] == ["apply", "-var", "use_spot=true"]


def test_cloud_up_can_use_another_public_key(fakes: Fakes) -> None:
    other = fakes.home / "other.pub"
    other.write_text(PUBLIC_KEY + "\n")
    (fakes.home / ".ssh" / "id_ed25519.pub").unlink()
    result = fakes.make("cloud-up", f"SSH_PUBLIC_KEY_FILE={other}")
    assert result.returncode == 0, result.stderr
    assert fakes.calls("terraform")[-1]["ssh_key"]


def test_cloud_up_stores_the_current_ip_before_applying(fakes: Fakes) -> None:
    result = fakes.make("cloud-up")
    assert result.returncode == 0, result.stderr
    calls = fakes.calls()
    put = next(i for i, c in enumerate(calls) if c["tool"] == "aws" and c["args"][:2] == ["ssm", "put-parameter"])
    apply = next(i for i, c in enumerate(calls) if c["tool"] == "terraform" and c["args"][0] == "apply")
    assert put < apply, "the IP must be stored before Terraform reads it"


def test_cloud_up_stops_before_terraform_if_the_ip_cannot_be_read(fakes: Fakes) -> None:
    fakes.http(CHECKIP, "<html>error</html>\n")
    result = fakes.make("cloud-up")
    assert len(fakes.calls("curl")) == 1, "cloud-up must ask checkip"
    assert result.returncode != 0
    assert not fakes.calls("terraform")


def test_owner_ip_writes_to_the_stacks_region_whatever_aws_region_says(fakes: Fakes) -> None:
    # The stacks fix their region in code, so a parameter written anywhere else would never be read.
    result = fakes.make("owner-ip", "AWS_REGION=eu-west-1")
    assert result.returncode == 0, result.stderr
    [put] = fakes.calls("aws", "ssm", "put-parameter")
    assert flag_value(put["args"], "--region") == "ap-southeast-1"


# --- make cloud-down ------------------------------------------------------------------------------------

def test_cloud_down_destroys_then_runs_the_cost_check(fakes: Fakes) -> None:
    result = fakes.make("cloud-down")
    assert result.returncode == 0, result.stdout + result.stderr

    calls = fakes.calls()
    tf = [c for c in calls if c["tool"] == "terraform"]
    assert [c["args"][0] for c in tf] == ["init", "destroy"]
    assert all(c["cwd"] == str(EPHEMERAL_DIR) for c in tf)
    assert tf[1]["ssh_key"], "destroy needs every required variable too"

    destroy_at = calls.index(tf[1])
    assert any(c["tool"] == "aws" for c in calls[destroy_at + 1:]), "cost-check must run after destroy"
    assert "No billable leftovers" in result.stdout


def test_cloud_down_checks_the_stacks_region_whatever_aws_region_says(fakes: Fakes) -> None:
    # The destroy is always in ap-southeast-1; the cost check after it must scan that region in full.
    result = fakes.make("cloud-down", "AWS_REGION=us-east-1")
    assert result.returncode == 0, result.stdout + result.stderr
    assert {flag_value(c["args"], "--region") for c in fakes.calls("aws")} == {"ap-southeast-1"}


def test_cloud_down_does_not_touch_the_owner_ip(fakes: Fakes) -> None:
    result = fakes.make("cloud-down")
    assert result.returncode == 0, result.stdout + result.stderr
    assert not fakes.calls("curl")
    assert not fakes.calls("aws", "ssm", "put-parameter")


def test_cloud_down_fails_when_destroy_fails(fakes: Fakes) -> None:
    fakes.fail("terraform", "destroy")
    result = fakes.make("cloud-down")
    assert [c["args"][0] for c in fakes.calls("terraform")][-1] == "destroy"
    assert result.returncode != 0


# --- make cloud-config ----------------------------------------------------------------------------------

def test_cloud_config_writes_the_inventory_from_terraform_output(fakes: Fakes) -> None:
    result = fakes.make("cloud-config")
    assert result.returncode == 0, result.stdout + result.stderr

    tf = fakes.calls("terraform")
    assert [c["args"] for c in tf if c["args"][0] != "init"] == [["output", "-raw", "instance_id"]]
    assert all(c["cwd"] == str(EPHEMERAL_DIR) and c["profile"] == "devops" for c in tf)

    # The host name is the instance ID: SSH hands it to Session Manager as the target (%h).
    assert yaml.safe_load(fakes.inventory.read_text()) == {"aws": {"hosts": {INSTANCE_ID: None}}}


def test_cloud_config_runs_the_aws_playbook_on_that_inventory(fakes: Fakes) -> None:
    result = fakes.make("cloud-config")
    assert result.returncode == 0, result.stdout + result.stderr

    runs = fakes.calls("ansible-playbook")
    assert len(runs) == 1
    assert runs[0]["cwd"] == str(ANSIBLE_DIR)
    assert runs[0]["profile"] == "devops", "the SSH ProxyCommand calls aws ssm with this profile"
    assert runs[0]["args"] == ["-i", str(fakes.inventory), "playbooks/aws-node.yml"]

    calls = fakes.calls()
    assert calls.index(runs[0]) > max(i for i, c in enumerate(calls) if c["tool"] == "terraform")


def test_cloud_config_passes_extra_ansible_arguments(fakes: Fakes) -> None:
    result = fakes.make("cloud-config", "ANSIBLE_ARGS=--check --diff")
    assert result.returncode == 0, result.stdout + result.stderr
    assert fakes.calls("ansible-playbook")[0]["args"][-3:] == ["playbooks/aws-node.yml", "--check", "--diff"]


@pytest.mark.parametrize("answer", ["", "No outputs found", "i-xyz", "i-0123", "i-0123456789abcdef0 extra"])
def test_cloud_config_stops_without_a_valid_instance_id(fakes: Fakes, answer: str) -> None:
    fakes.tf_output("instance_id", answer)
    result = fakes.make("cloud-config")
    assert fakes.calls("terraform", "output"), "it must ask Terraform"
    assert result.returncode != 0
    assert not fakes.inventory.exists()
    assert not fakes.calls("ansible-playbook")
    assert "make cloud-up" in result.stdout + result.stderr


# --- make cloud-tunnel ----------------------------------------------------------------------------------

def test_cloud_tunnel_forwards_the_kubernetes_api_to_local_port_16443(fakes: Fakes) -> None:
    result = fakes.make("cloud-tunnel")
    assert result.returncode == 0, result.stdout + result.stderr

    sessions = fakes.calls("aws", "ssm", "start-session")
    assert len(sessions) == 1
    args = sessions[0]["args"]
    assert flag_value(args, "--target") == INSTANCE_ID
    assert flag_value(args, "--document-name") == "AWS-StartPortForwardingSession"
    assert flag_value(args, "--parameters") == "portNumber=6443,localPortNumber=16443"
    assert flag_value(args, "--region") == "ap-southeast-1"
    assert sessions[0]["profile"] == "devops"


def test_cloud_tunnel_stops_without_a_running_node(fakes: Fakes) -> None:
    fakes.tf_output("instance_id", "")
    result = fakes.make("cloud-tunnel")
    assert fakes.calls("terraform"), "it must ask Terraform"
    assert result.returncode != 0
    assert not fakes.calls("aws", "ssm", "start-session")


# --- make cloud-rebuild ---------------------------------------------------------------------------------

def test_cloud_rebuild_applies_then_configures(fakes: Fakes) -> None:
    result = fakes.make("cloud-rebuild")
    assert result.returncode == 0, result.stdout + result.stderr

    steps = [c["args"][0] for c in fakes.calls() if c["tool"] in ("terraform", "ansible-playbook")
             and c["args"][0] != "init"]
    assert steps == ["apply", "output", "-i"]


def test_cloud_rebuild_does_not_configure_after_a_failed_apply(fakes: Fakes) -> None:
    fakes.fail("terraform", "apply")
    result = fakes.make("cloud-rebuild")
    assert fakes.calls("terraform", "apply"), "it must try to apply"
    assert result.returncode != 0
    assert not fakes.calls("ansible-playbook")


# --- Rules for every AWS session target -----------------------------------------------------------------

@pytest.mark.parametrize("target", ["cloud-config", "cloud-tunnel"])
def test_session_helpers_never_change_the_stack(fakes: Fakes, target: str) -> None:
    result = fakes.make(target)
    assert result.returncode == 0, result.stdout + result.stderr
    assert fakes.calls("terraform"), "the target must run Terraform"
    for call in fakes.calls("terraform"):
        assert call["args"][0] in ("init", "output")
    assert not fakes.calls("aws", "ssm", "put-parameter")


@pytest.mark.parametrize("target", ["cloud-up", "cloud-down", "cloud-rebuild"])
def test_terraform_always_asks_for_confirmation(fakes: Fakes, target: str) -> None:
    result = fakes.make(target)
    assert result.returncode == 0, result.stdout + result.stderr
    assert fakes.calls("terraform"), "the target must run Terraform"
    for call in fakes.calls("terraform"):
        assert not any("-auto-approve" in a for a in call["args"])
        assert not any("-lock=false" in a for a in call["args"])


@pytest.mark.parametrize("target", ["owner-ip", "cloud-up", "cloud-down", "cloud-config", "cloud-tunnel",
                                    "cloud-rebuild"])
def test_no_cost_explorer_calls(fakes: Fakes, target: str) -> None:
    result = fakes.make(target)
    assert result.returncode == 0, result.stdout + result.stderr
    assert fakes.calls(), "the target must run"
    assert not fakes.calls("aws", "ce")
