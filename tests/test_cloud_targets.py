"""Tests for the AWS session targets `make owner-ip`, `cloud-up`, `cloud-down`, `cloud-config`,
`cloud-tunnel` and `cloud-rebuild`.

They run the real Make targets with fake `terraform`, `aws`, `curl` and `ansible-playbook` commands first
on PATH, so no AWS call is made and nothing is configured. Every fake appends one line per call to a shared log: the tool name, the directory it ran
in, the AWS profile, whether an SSH public key was passed, and its arguments. That shows what ran, where,
and in which order.
"""

import os
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
EPHEMERAL_DIR = REPO_ROOT / "terraform" / "envs" / "aws"
ANSIBLE_DIR = REPO_ROOT / "ansible"
INSTANCE_ID = "i-0123456789abcdef0"
PUBLIC_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExampleExampleExampleExampleExampleExample test@example"

FAKE = r"""#!/usr/bin/env bash
printf '%s|%s|%s|%s|%s\n' "$(basename "$0")" "$PWD" "${AWS_PROFILE:-}" \
  "${TF_VAR_ssh_public_key:+key}" "$*" >> "$FAKE_LOG"
case "$(basename "$0") $1 $2" in
  "curl "*)                       cat "$FAKE_DIR/checkip" ;;
  "aws sts get-caller-identity")  echo "arn:aws:iam::111122223333:user/tester" ;;
  "aws ec2 describe-regions")     echo "ap-southeast-1" ;;
  "terraform output -raw")        printf '%s' "$(cat "$FAKE_DIR/instance_id")" ;;
esac
if [ "$(basename "$0")" = terraform ] && [ -e "$FAKE_DIR/fail-$1" ]; then
  echo "fake terraform: $1 failed" >&2
  exit 1
fi
exit 0
"""


class Fakes:
    def __init__(self, root: Path) -> None:
        self.dir = root
        self.bin = root / "bin"
        self.log = root / "calls.log"
        self.home = root / "home"
        self.bin.mkdir()
        self.log.touch()
        (self.home / ".ssh").mkdir(parents=True)
        (self.home / ".ssh" / "id_ed25519.pub").write_text(PUBLIC_KEY + "\n")
        for tool in ("terraform", "aws", "curl", "ansible-playbook"):
            path = self.bin / tool
            path.write_text(FAKE)
            path.chmod(0o755)
        self.checkip("203.0.113.10")
        self.instance_id(INSTANCE_ID)
        # Tests write the generated inventory here, never into the repo (a live one may be in use).
        self.inventory = root / "inventory" / "aws.yml"

    def instance_id(self, answer: str) -> None:
        """What `terraform output -raw instance_id` prints (nothing when the stack is down)."""
        (self.dir / "instance_id").write_text(answer)

    def checkip(self, answer: str) -> None:
        (self.dir / "checkip").write_text(answer + "\n")

    def fail(self, terraform_subcommand: str) -> None:
        (self.dir / f"fail-{terraform_subcommand}").touch()

    def calls(self, tool: str | None = None) -> list[dict[str, str]]:
        rows = []
        for line in self.log.read_text().splitlines():
            name, cwd, profile, key, args = line.split("|", 4)
            rows.append({"tool": name, "cwd": cwd, "profile": profile, "key": key, "args": args})
        return [r for r in rows if tool is None or r["tool"] == tool]

    def make(self, target: str, *overrides: str) -> subprocess.CompletedProcess[str]:
        overrides = (f"AWS_INVENTORY={self.inventory}", *overrides)
        env = {
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(self.home),
            "FAKE_LOG": str(self.log),
            "FAKE_DIR": str(self.dir),
        }
        return subprocess.run(
            ["make", "--no-print-directory", target, *overrides],
            cwd=REPO_ROOT, env=env, stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=120,
        )


@pytest.fixture
def fakes(tmp_path: Path) -> Fakes:
    return Fakes(tmp_path)


# --- make owner-ip --------------------------------------------------------------------------------------

def test_owner_ip_writes_this_machines_ip_as_a_32(fakes: Fakes, terraform_names: dict[str, str]) -> None:
    result = fakes.make("owner-ip")
    assert result.returncode == 0, result.stderr

    curl = fakes.calls("curl")
    assert len(curl) == 1 and "https://checkip.amazonaws.com" in curl[0]["args"]

    puts = [c for c in fakes.calls("aws") if c["args"].startswith("ssm put-parameter")]
    assert len(puts) == 1
    args = puts[0]["args"].split()
    assert args[args.index("--name") + 1] == terraform_names["owner_cidr_parameter"]
    assert args[args.index("--type") + 1] == "String"
    assert args[args.index("--value") + 1] == "203.0.113.10/32"
    assert "--overwrite" in args
    assert puts[0]["profile"] == "devops"


@pytest.mark.parametrize("answer", ["", "<html>error</html>", "203.0.113.300", "2001:db8::1", "1.2.3"])
def test_owner_ip_refuses_anything_but_an_ipv4_address(fakes: Fakes, answer: str) -> None:
    fakes.checkip(answer)
    result = fakes.make("owner-ip")
    assert len(fakes.calls("curl")) == 1, "the target must run and ask checkip"
    assert result.returncode != 0
    assert not [c for c in fakes.calls("aws") if "put-parameter" in c["args"]]


# --- make cloud-up --------------------------------------------------------------------------------------

def test_cloud_up_applies_the_ephemeral_stack_interactively(fakes: Fakes) -> None:
    result = fakes.make("cloud-up")
    assert result.returncode == 0, result.stderr

    tf = fakes.calls("terraform")
    assert [c["args"].split()[0] for c in tf] == ["init", "apply"]
    assert all(c["cwd"] == str(EPHEMERAL_DIR) for c in tf)
    assert all(c["profile"] == "devops" for c in tf)
    assert tf[1]["key"] == "key", "the SSH public key must reach Terraform"


def test_cloud_up_needs_the_ssh_public_key(fakes: Fakes) -> None:
    (fakes.home / ".ssh" / "id_ed25519.pub").unlink()
    result = fakes.make("cloud-up")
    assert result.returncode != 0
    assert not fakes.calls("terraform")
    assert "id_ed25519.pub" in result.stdout + result.stderr


def test_cloud_up_passes_extra_terraform_arguments(fakes: Fakes) -> None:
    result = fakes.make("cloud-up", "CLOUD_ARGS=-var use_spot=true")
    assert result.returncode == 0, result.stderr
    assert fakes.calls("terraform")[-1]["args"] == "apply -var use_spot=true"


def test_cloud_up_can_use_another_public_key(fakes: Fakes) -> None:
    other = fakes.home / "other.pub"
    other.write_text(PUBLIC_KEY + "\n")
    (fakes.home / ".ssh" / "id_ed25519.pub").unlink()
    result = fakes.make("cloud-up", f"SSH_PUBLIC_KEY_FILE={other}")
    assert result.returncode == 0, result.stderr
    assert fakes.calls("terraform")[-1]["key"] == "key"


def test_cloud_up_stores_the_current_ip_before_applying(fakes: Fakes) -> None:
    result = fakes.make("cloud-up")
    assert result.returncode == 0, result.stderr
    calls = fakes.calls()
    put = next(i for i, c in enumerate(calls) if c["args"].startswith("ssm put-parameter"))
    apply = next(i for i, c in enumerate(calls) if c["tool"] == "terraform" and c["args"].startswith("apply"))
    assert put < apply, "the IP must be stored before Terraform reads it"


def test_cloud_up_stops_before_terraform_if_the_ip_cannot_be_read(fakes: Fakes) -> None:
    fakes.checkip("<html>error</html>")
    result = fakes.make("cloud-up")
    assert len(fakes.calls("curl")) == 1, "cloud-up must ask checkip"
    assert result.returncode != 0
    assert not fakes.calls("terraform")


def test_owner_ip_writes_to_the_stacks_region_whatever_aws_region_says(fakes: Fakes) -> None:
    # The stacks fix their region in code, so a parameter written anywhere else would never be read.
    result = fakes.make("owner-ip", "AWS_REGION=eu-west-1")
    assert result.returncode == 0, result.stderr
    put = next(c["args"].split() for c in fakes.calls("aws") if c["args"].startswith("ssm put-parameter"))
    assert put[put.index("--region") + 1] == "ap-southeast-1"


# --- make cloud-down ------------------------------------------------------------------------------------

def test_cloud_down_destroys_then_runs_the_cost_check(fakes: Fakes) -> None:
    result = fakes.make("cloud-down")
    assert result.returncode == 0, result.stdout + result.stderr

    calls = fakes.calls()
    tf = [c for c in calls if c["tool"] == "terraform"]
    assert [c["args"].split()[0] for c in tf] == ["init", "destroy"]
    assert all(c["cwd"] == str(EPHEMERAL_DIR) for c in tf)
    assert tf[1]["key"] == "key", "destroy needs every required variable too"

    destroy_at = calls.index(tf[1])
    assert any(c["tool"] == "aws" for c in calls[destroy_at + 1:]), "cost-check must run after destroy"
    assert "No billable leftovers" in result.stdout


def test_cloud_down_does_not_touch_the_owner_ip(fakes: Fakes) -> None:
    result = fakes.make("cloud-down")
    assert result.returncode == 0, result.stdout + result.stderr
    assert not fakes.calls("curl")
    assert not [c for c in fakes.calls("aws") if "put-parameter" in c["args"]]


def test_cloud_down_fails_when_destroy_fails(fakes: Fakes) -> None:
    fakes.fail("destroy")
    result = fakes.make("cloud-down")
    assert [c["args"].split()[0] for c in fakes.calls("terraform")][-1] == "destroy"
    assert result.returncode != 0


# --- make cloud-config ----------------------------------------------------------------------------------

def test_cloud_config_writes_the_inventory_from_terraform_output(fakes: Fakes) -> None:
    result = fakes.make("cloud-config")
    assert result.returncode == 0, result.stdout + result.stderr

    tf = fakes.calls("terraform")
    assert [c["args"] for c in tf if not c["args"].startswith("init")] == ["output -raw instance_id"]
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
    assert runs[0]["args"] == f"-i {fakes.inventory} playbooks/aws-node.yml"

    calls = fakes.calls()
    assert calls.index(runs[0]) > max(i for i, c in enumerate(calls) if c["tool"] == "terraform")


def test_cloud_config_passes_extra_ansible_arguments(fakes: Fakes) -> None:
    result = fakes.make("cloud-config", "ANSIBLE_ARGS=--check --diff")
    assert result.returncode == 0, result.stdout + result.stderr
    assert fakes.calls("ansible-playbook")[0]["args"].endswith("playbooks/aws-node.yml --check --diff")


@pytest.mark.parametrize("answer", ["", "No outputs found", "i-xyz", "i-0123", "i-0123456789abcdef0 extra"])
def test_cloud_config_stops_without_a_valid_instance_id(fakes: Fakes, answer: str) -> None:
    fakes.instance_id(answer)
    result = fakes.make("cloud-config")
    assert any(c["args"].startswith("output") for c in fakes.calls("terraform")), "it must ask Terraform"
    assert result.returncode != 0
    assert not fakes.inventory.exists()
    assert not fakes.calls("ansible-playbook")
    assert "make cloud-up" in result.stdout + result.stderr


# --- make cloud-tunnel ----------------------------------------------------------------------------------

def test_cloud_tunnel_forwards_the_kubernetes_api_to_local_port_16443(fakes: Fakes) -> None:
    result = fakes.make("cloud-tunnel")
    assert result.returncode == 0, result.stdout + result.stderr

    sessions = [c for c in fakes.calls("aws") if c["args"].startswith("ssm start-session")]
    assert len(sessions) == 1
    args = sessions[0]["args"].split()
    assert args[args.index("--target") + 1] == INSTANCE_ID
    assert args[args.index("--document-name") + 1] == "AWS-StartPortForwardingSession"
    assert args[args.index("--parameters") + 1] == "portNumber=6443,localPortNumber=16443"
    assert args[args.index("--region") + 1] == "ap-southeast-1"
    assert sessions[0]["profile"] == "devops"


def test_cloud_tunnel_stops_without_a_running_node(fakes: Fakes) -> None:
    fakes.instance_id("")
    result = fakes.make("cloud-tunnel")
    assert fakes.calls("terraform"), "it must ask Terraform"
    assert result.returncode != 0
    assert not [c for c in fakes.calls("aws") if c["args"].startswith("ssm start-session")]


# --- make cloud-rebuild ---------------------------------------------------------------------------------

def test_cloud_rebuild_applies_then_configures(fakes: Fakes) -> None:
    result = fakes.make("cloud-rebuild")
    assert result.returncode == 0, result.stdout + result.stderr

    steps = [c["args"].split()[0] for c in fakes.calls() if c["tool"] in ("terraform", "ansible-playbook")
             and not c["args"].startswith("init")]
    assert steps == ["apply", "output", "-i"]


def test_cloud_rebuild_does_not_configure_after_a_failed_apply(fakes: Fakes) -> None:
    fakes.fail("apply")
    result = fakes.make("cloud-rebuild")
    assert "apply" in [c["args"].split()[0] for c in fakes.calls("terraform")], "it must try to apply"
    assert result.returncode != 0
    assert not fakes.calls("ansible-playbook")


# --- Rules for every AWS session target -----------------------------------------------------------------

@pytest.mark.parametrize("target", ["cloud-config", "cloud-tunnel"])
def test_session_helpers_never_change_the_stack(fakes: Fakes, target: str) -> None:
    result = fakes.make(target)
    assert result.returncode == 0, result.stdout + result.stderr
    assert fakes.calls("terraform"), "the target must run Terraform"
    for call in fakes.calls("terraform"):
        assert call["args"].split()[0] in ("init", "output")
    assert not [c for c in fakes.calls("aws") if "put-parameter" in c["args"]]


@pytest.mark.parametrize("target", ["cloud-up", "cloud-down", "cloud-rebuild"])
def test_terraform_always_asks_for_confirmation(fakes: Fakes, target: str) -> None:
    result = fakes.make(target)
    assert result.returncode == 0, result.stdout + result.stderr
    assert fakes.calls("terraform"), "the target must run Terraform"
    for call in fakes.calls("terraform"):
        assert "-auto-approve" not in call["args"]
        assert "-lock=false" not in call["args"]


@pytest.mark.parametrize("target", ["owner-ip", "cloud-up", "cloud-down", "cloud-config", "cloud-tunnel",
                                    "cloud-rebuild"])
def test_no_cost_explorer_calls(fakes: Fakes, target: str) -> None:
    result = fakes.make(target)
    assert result.returncode == 0, result.stdout + result.stderr
    assert fakes.calls(), "the target must run"
    assert not [c for c in fakes.calls("aws") if c["args"].split()[0] == "ce"]
