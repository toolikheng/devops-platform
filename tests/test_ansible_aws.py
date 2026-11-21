"""Project rules for the Ansible code that configures the AWS node (playbooks/aws-node.yml).

ansible-lint checks style and syntax, and a live run checks behaviour. These tests pin the rules neither
of them knows about: Ansible reaches the node only through Session Manager, the homelab keeps its old
SSH setting, k3s is pinned and its installer checksum-verified, Secrets are encrypted at rest, and the
kubeconfig points at the same local port that `make cloud-tunnel` opens.
"""

import re
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
ANSIBLE = REPO_ROOT / "ansible"
K3S_ROLE = ANSIBLE / "roles" / "k3s"
TUNNEL_SCRIPT = REPO_ROOT / "scripts" / "cloud-tunnel.sh"


def load(path: Path) -> Any:
    return yaml.safe_load(path.read_text())


def tasks(role: Path) -> list[dict[str, Any]]:
    """Every task in a role's tasks/ folder (one level, which is all these roles use)."""
    found: list[dict[str, Any]] = []
    for path in sorted((role / "tasks").glob("*.yml")):
        found.extend(load(path) or [])
    return found


# --- How Ansible reaches the node --------------------------------------------------------------------------

def test_aws_hosts_are_reached_only_through_session_manager() -> None:
    group_vars = load(ANSIBLE / "inventory" / "group_vars" / "aws.yml")
    ssh_args = group_vars["ansible_ssh_common_args"]

    # The security group has no port 22: SSH runs inside an SSM session to the instance ID (%h).
    assert re.search(r"ProxyCommand=.*aws ssm start-session", ssh_args)
    assert "--target %h" in ssh_args
    assert "--document-name AWS-StartSSHSession" in ssh_args
    assert "portNumber=%p" in ssh_args
    assert "--region ap-southeast-1" in ssh_args
    assert "ansible_host" not in group_vars, "the host name must stay the instance ID"


def test_aws_hosts_log_in_as_ubuntu_and_never_as_root() -> None:
    group_vars = load(ANSIBLE / "inventory" / "group_vars" / "aws.yml")
    assert group_vars["ansible_user"] == "ubuntu"
    assert group_vars["ansible_become"] is True
    assert group_vars["common_permit_root_login"] == "no"


def test_aws_hosts_use_pipelining() -> None:
    # Every round trip goes through Session Manager. Measured in C3: a run with nothing to change took
    # 136 s without pipelining and 30 s with it (one SSH round trip per task instead of several).
    group_vars = load(ANSIBLE / "inventory" / "group_vars" / "aws.yml")
    assert group_vars["ansible_pipelining"] is True


def test_homelab_keeps_its_root_login_setting() -> None:
    # The Docker nodes are provisioned as root over SSH keys; C3 must not change that.
    defaults = load(ANSIBLE / "roles" / "common" / "defaults" / "main.yml")
    assert defaults["common_permit_root_login"] == "without-password"


def test_aws_playbook_configures_the_aws_group_with_the_three_roles() -> None:
    plays = load(ANSIBLE / "playbooks" / "aws-node.yml")
    assert len(plays) == 1
    play = plays[0]
    assert play["hosts"] == "aws"
    roles = [r["role"] if isinstance(r, dict) else r for r in play["roles"]]
    assert roles == ["common", "node_exporter", "k3s"]


# --- k3s ---------------------------------------------------------------------------------------------------

def test_k3s_version_is_pinned_to_a_stable_v1_36_release() -> None:
    defaults = load(K3S_ROLE / "defaults" / "main.yml")
    assert re.fullmatch(r"v1\.36\.\d+\+k3s\d+", defaults["k3s_version"])


def test_k3s_installer_is_downloaded_for_that_version_and_checksum_verified() -> None:
    defaults = load(K3S_ROLE / "defaults" / "main.yml")
    assert re.fullmatch(r"[0-9a-f]{64}", defaults["k3s_install_script_sha256"])

    downloads = [t for t in tasks(K3S_ROLE) if "ansible.builtin.get_url" in t]
    installers = [t["ansible.builtin.get_url"] for t in downloads
                  if t["ansible.builtin.get_url"]["url"].endswith("/install.sh")]
    assert len(installers) == 1
    assert "{{ k3s_version }}" in installers[0]["url"]
    assert installers[0]["checksum"] == "sha256:{{ k3s_install_script_sha256 }}"


def test_k3s_encrypts_secrets_and_keeps_its_kubeconfig_private() -> None:
    config = load(K3S_ROLE / "files" / "config.yaml")
    assert config["secrets-encryption"] is True
    assert config["write-kubeconfig-mode"] == "0600"


def test_kubeconfig_goes_to_the_owners_home_not_the_repo() -> None:
    defaults = load(K3S_ROLE / "defaults" / "main.yml")
    path = defaults["k3s_kubeconfig_path"]
    assert path.startswith("{{ lookup('env', 'HOME') }}/.kube/")
    assert path.endswith("/devops-aws.yaml")


def test_kubeconfig_port_matches_the_tunnel() -> None:
    defaults = load(K3S_ROLE / "defaults" / "main.yml")
    script = TUNNEL_SCRIPT.read_text()
    local_port = re.search(r"^LOCAL_PORT=(\d+)$", script, re.M)
    remote_port = re.search(r"^REMOTE_PORT=(\d+)$", script, re.M)
    assert local_port and remote_port, "cloud-tunnel.sh must define LOCAL_PORT and REMOTE_PORT"
    assert defaults["k3s_kubeconfig_local_port"] == int(local_port.group(1)) == 16443
    assert int(remote_port.group(1)) == 6443


# --- Check mode (make cloud-config ANSIBLE_ARGS='--check --diff') ------------------------------------------

def test_check_mode_still_waits_for_the_node_and_reads_fresh_package_lists() -> None:
    # Found live in C3: in check mode these tasks were skipped or only pretended to run, so the waits never
    # waited and apt, with the empty package lists of a fresh cloud image, could not find any package.
    # Each one only waits or refreshes the package index, so running it for real changes no configuration.
    pre_tasks = {t["name"]: t for t in load(ANSIBLE / "playbooks" / "aws-node.yml")[0]["pre_tasks"]}
    for name in ("Wait for SSH over Session Manager", "Wait for cloud-init to finish"):
        assert pre_tasks[name].get("check_mode") is False, name

    common = {t["name"]: t for t in tasks(ANSIBLE / "roles" / "common")}
    assert common["Update package cache"].get("check_mode") is False
