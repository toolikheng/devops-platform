"""Shared fixtures for the repo-level tests."""

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
NAMES_MODULE = REPO_ROOT / "terraform" / "modules" / "names" / "outputs.tf"


@pytest.fixture(scope="session")
def terraform_names() -> dict[str, str]:
    """The names both Terraform stacks share, read from terraform/modules/names (output name -> value)."""
    text = NAMES_MODULE.read_text()
    return dict(re.findall(r'output "(\w+)" \{.*?value\s*=\s*"([^"]+)"', text, re.S))
