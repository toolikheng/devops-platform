"""Tests for the AWS session targets `make owner-ip`, `make cloud-up` and `make cloud-down`.

They run the real Make targets with fake `terraform`, `aws` and `curl` commands first on PATH, so no AWS
call is made. Every fake appends one line per call to a shared log: the tool name, the directory it ran
in, the AWS profile, whether an SSH public key was passed, and its arguments. That shows what ran, where,
and in which order.
"""

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
EPHEMERAL_DIR = REPO_ROOT / "terraform" / "envs" / "aws"
PUBLIC_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExampleExampleExampleExampleExampleExample test@example"

FAKE = r"""#!/usr/bin/env bash
printf '%s|%s|%s|%s|%s\n' "$(basename "$0")" "$PWD" "${AWS_PROFILE:-}" \
  "${TF_VAR_ssh_public_key:+key}" "$*" >> "$FAKE_LOG"
case "$(basename "$0") $1 $2" in
  "curl "*)                       cat "$FAKE_DIR/checkip" ;;
  "aws sts get-caller-identity")  echo "arn:aws:iam::111122223333:user/tester" ;;
  "aws ec2 describe-regions")     echo "ap-southeast-1" ;;
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
        for tool in ("terraform", "aws", "curl"):
            path = self.bin / tool
            path.write_text(FAKE)
            path.chmod(0o755)
        self.checkip("203.0.113.10")

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

    def make(self, target: str) -> subprocess.CompletedProcess[str]:
        env = {
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(self.home),
            "FAKE_LOG": str(self.log),
            "FAKE_DIR": str(self.dir),
        }
        return subprocess.run(
            ["make", "--no-print-directory", target],
            cwd=REPO_ROOT, env=env, stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=120,
        )


@pytest.fixture
def fakes(tmp_path: Path) -> Fakes:
    return Fakes(tmp_path)


# --- make owner-ip --------------------------------------------------------------------------------------

def test_owner_ip_writes_this_machines_ip_as_a_32(fakes: Fakes) -> None:
    result = fakes.make("owner-ip")
    assert result.returncode == 0, result.stderr

    curl = fakes.calls("curl")
    assert len(curl) == 1 and "https://checkip.amazonaws.com" in curl[0]["args"]

    puts = [c for c in fakes.calls("aws") if c["args"].startswith("ssm put-parameter")]
    assert len(puts) == 1
    args = puts[0]["args"].split()
    assert args[args.index("--name") + 1] == "/devops-platform/owner-cidr"
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


def test_cloud_down_fails_when_destroy_fails(fakes: Fakes) -> None:
    fakes.fail("destroy")
    result = fakes.make("cloud-down")
    assert [c["args"].split()[0] for c in fakes.calls("terraform")][-1] == "destroy"
    assert result.returncode != 0


# --- Rules for every AWS session target -----------------------------------------------------------------

@pytest.mark.parametrize("target", ["cloud-up", "cloud-down"])
def test_terraform_always_asks_for_confirmation(fakes: Fakes, target: str) -> None:
    result = fakes.make(target)
    assert result.returncode == 0, result.stdout + result.stderr
    assert fakes.calls("terraform"), "the target must run Terraform"
    for call in fakes.calls("terraform"):
        assert "-auto-approve" not in call["args"]
        assert "-lock=false" not in call["args"]


@pytest.mark.parametrize("target", ["owner-ip", "cloud-up", "cloud-down"])
def test_no_cost_explorer_calls(fakes: Fakes, target: str) -> None:
    result = fakes.make(target)
    assert result.returncode == 0, result.stdout + result.stderr
    assert fakes.calls(), "the target must run"
    assert not [c for c in fakes.calls("aws") if c["args"].split()[0] == "ce"]
