"""Secrets never reach Terraform state: rules over every .tf file under terraform/.

terraform/persistent/tests/policypal_parameters.tftest.hcl checks the catalogued parameters in detail.
These text checks cover every stack and module, including parameters added later (C7):

- a random value is only ever an `ephemeral` resource: a `resource "random_password"` keeps its result in
  state, even when it only feeds a write-only argument;
- every SecureString parameter is written with value_wo, never value or insecure_value (both are stored),
  and value_wo comes from an ephemeral resource: a literal there would put the secret in Git. (A Terraform
  test can't check this: value_wo always reads as null, whatever the configuration sets.)
"""

import re
from pathlib import Path

from conftest import secret_catalog

REPO_ROOT = Path(__file__).resolve().parent.parent
TF_FILES = sorted(p for p in (REPO_ROOT / "terraform").rglob("*.tf") if ".terraform" not in p.parts)


def blocks(text: str, kind: str, rtype: str) -> list[str]:
    """Bodies of `<kind> "<rtype>" "<name>" {` blocks, up to the closing brace at column 0 (terraform fmt)."""
    return re.findall(rf'^{kind} "{rtype}" "[^"]+" \{{\n(.*?)^\}}', text, re.M | re.S)


def test_random_values_are_only_ephemeral() -> None:
    stored = [f"{p.relative_to(REPO_ROOT)}" for p in TF_FILES if re.search(r'^resource "random_', p.read_text(), re.M)]
    assert stored == [], "use ephemeral \"random_password\": a random resource keeps its result in state"


def test_every_securestring_is_written_with_value_wo() -> None:
    found = 0
    for path in TF_FILES:
        for body in blocks(path.read_text(), "resource", "aws_ssm_parameter"):
            if '"SecureString"' not in body:
                continue
            found += 1
            assert re.search(r"^\s*value_wo\s*=\s*ephemeral\.", body, re.M), f"{path}: value_wo from an ephemeral"
            assert re.search(r"^\s*value_wo_version\s*=", body, re.M), path
            assert not re.search(r"^\s*(insecure_)?value\s*=", body, re.M), path
    assert found >= 1, "the catalogued parameters (one for_each block) must be among them"


PERSISTENT = REPO_ROOT / "terraform" / "persistent"


def test_the_secret_catalog_names_each_secret_and_who_reads_it() -> None:
    # terraform/persistent/secret-catalog.yaml: Terraform makes one parameter per entry, the checks read who
    # reads it. A namespace means an ExternalSecret there; none means the node reads it itself.
    catalog = secret_catalog()
    assert catalog, "the catalog lists the secrets"
    for name, entry in catalog.items():
        assert re.fullmatch(r"[a-z][a-z0-9-]*", name), f"{name}: a plain parameter name"
        assert set(entry) <= {"description", "namespace"} and entry.get("description"), name
        assert re.fullmatch(r"[a-z][a-z0-9-]*", entry.get("namespace", "node-reads-it")), name


def test_the_parameters_made_before_the_catalog_are_moved_not_replaced() -> None:
    # Without these, Terraform would destroy the two live parameters and create new ones with new values.
    text = (PERSISTENT / "policypal_secrets.tf").read_text()
    moved = set(re.findall(r'moved \{\s*from\s*=\s*([\w.]+)\s*to\s*=\s*([\w.]+\["[^"]+"\])\s*\}', text))
    assert moved == {("aws_ssm_parameter.db_password", 'aws_ssm_parameter.secret["db-password"]'),
                     ("aws_ssm_parameter.admin_token", 'aws_ssm_parameter.secret["admin-token"]')}
