"""The committed .gitignore keeps Terraform state, plans, variable files, kubeconfigs, keys and .env
out of git, still lets the files that must be committed through, and lists no local-only paths.

The rules are checked in a throwaway repository that contains only the committed .gitignore, so
local excludes (.git/info/exclude, a global excludes file) can't make the test pass by accident.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
GITIGNORE = REPO_ROOT / ".gitignore"

MUST_IGNORE = [
    "terraform/envs/aws/terraform.tfstate",
    "terraform/envs/aws/terraform.tfstate.backup",
    "terraform/envs/aws/.terraform/providers/registry.terraform.io/x",
    "terraform/envs/aws/tfplan",
    "terraform/envs/aws/destroy.tfplan",
    "terraform/persistent/terraform.tfvars",
    "terraform/persistent/dev.auto.tfvars",
    "terraform/persistent/terraform.tfvars.json",
    "terraform/persistent/crash.log",
    "terraform/persistent/crash.1700000000.log",
    "terraform/persistent/override.tf",
    "terraform/persistent/backend_override.tf",
    "kubeconfig",
    "devops-aws.kubeconfig",
    "kubeconfig-aws.yaml",
    ".env",
    "deploy-key.pem",
    "id_ed25519",
    "id_rsa",
    # ansible-lint's cache of the collections in ansible/requirements.yml.
    "ansible/.ansible/collections/ansible_collections/community/general/MANIFEST.json",
]

MUST_KEEP = [
    "terraform/persistent/.terraform.lock.hcl",
    "terraform/persistent/terraform.tfvars.example",
    "terraform/modules/network/main.tf",
    "k8s/policypal/overlays/aws/externalsecret-db.yaml",
    "scripts/cost-check.sh",
    "scripts/check-state-secrets.sh",
    "tests/test_cost_check.py",
    ".env.example",
]

LOCAL_ONLY = ["CLAUDE.md", "CLAUDE.local.md", "planning", ".local-archive", ".claude", ".agents",
              "skills-lock.json"]


@pytest.fixture(scope="module")
def ignored(tmp_path_factory: pytest.TempPathFactory) -> set[str]:
    """Paths from MUST_IGNORE + MUST_KEEP that the committed .gitignore alone ignores."""
    repo = tmp_path_factory.mktemp("gitignore-repo")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    shutil.copy(GITIGNORE, repo / ".gitignore")
    result = subprocess.run(
        ["git", "-c", "core.excludesFile=/dev/null", "check-ignore", "--no-index", "--stdin"],
        cwd=repo, input="\n".join(MUST_IGNORE + MUST_KEEP) + "\n",
        capture_output=True, text=True,
    )
    assert result.returncode in (0, 1), result.stderr  # 1 = nothing ignored
    return set(result.stdout.split())


@pytest.mark.parametrize("path", MUST_IGNORE)
def test_sensitive_file_is_ignored(ignored: set[str], path: str) -> None:
    assert path in ignored


@pytest.mark.parametrize("path", MUST_KEEP)
def test_file_that_must_be_committed_is_not_ignored(ignored: set[str], path: str) -> None:
    assert path not in ignored


def test_no_local_only_paths_in_committed_gitignore() -> None:
    patterns = {
        line.strip().strip("/")
        for line in GITIGNORE.read_text().splitlines()
        if line.strip() and not line.startswith("#")
    }
    assert not patterns & set(LOCAL_ONLY)
