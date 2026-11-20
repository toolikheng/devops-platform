"""The Terraform stacks read shared names from terraform/modules/names. Scripts can't, so their copies are
checked against the module here: a rename that misses one fails offline instead of in AWS."""

import re

from conftest import REPO_ROOT


def test_names_module_defines_the_shared_names(terraform_names: dict[str, str]) -> None:
    assert set(terraform_names) == {"node", "owner_cidr_parameter"}


def test_owner_ip_script_writes_the_parameter_the_stacks_read(terraform_names: dict[str, str]) -> None:
    script = (REPO_ROOT / "scripts" / "owner-ip.sh").read_text()
    match = re.search(r'^PARAMETER="([^"]+)"$', script, re.M)
    assert match, "owner-ip.sh must set PARAMETER"
    assert match.group(1) == terraform_names["owner_cidr_parameter"]
