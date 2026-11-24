"""The node's backup of PolicyPal to S3: the Ansible role restic_backup (playbooks/aws-node.yml).

- restic is pinned (ci/versions.env, shared with the homelab backup's image) and its download is checked against a
  recorded sha256.
- A systemd timer runs policypal-backup.service every hour. The service runs files/policypal-backup.sh, which backs
  up the Postgres dump first, then the uploads directory, then applies restic's retention. Any failure exits
  non-zero, so systemd marks the run failed.
- The repository password comes from SSM Parameter Store at run time through RESTIC_PASSWORD_COMMAND: never on
  disk, in a log or on a command line.

The script runs here with the fake CLIs of tests/fakecli.py (kubectl, restic), so nothing is installed or sent.
"""

import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from conftest import REPO_ROOT, secret_catalog, versions
from fakecli import Fakes, output

ROLE = REPO_ROOT / "ansible" / "roles" / "restic_backup"
SCRIPT = ROLE / "files" / "policypal-backup.sh"
SSM_HELPER = ROLE / "files" / "ssm-parameter"
SERVICE = ROLE / "templates" / "policypal-backup.service.j2"
TIMER = ROLE / "templates" / "policypal-backup.timer.j2"
AWS_PLAYBOOK = REPO_ROOT / "ansible" / "playbooks" / "aws-node.yml"
SITE_PLAYBOOK = REPO_ROOT / "ansible" / "playbooks" / "site.yml"
NAMES = REPO_ROOT / "terraform" / "modules" / "names" / "outputs.tf"

REPOSITORY = "s3:s3.ap-southeast-1.amazonaws.com/toolikheng-devops-platform-backups/restic"
PASSWORD_COMMAND = "/usr/local/sbin/ssm-parameter /policypal/aws/restic-password"
VOLUME = "pvc-1234abcd-0000-4000-8000-123456789abc"


def defaults() -> dict[str, Any]:
    return yaml.safe_load((ROLE / "defaults" / "main.yml").read_text())


def tasks() -> list[dict[str, Any]]:
    return yaml.safe_load((ROLE / "tasks" / "main.yml").read_text())


# --- wiring and pins ---------------------------------------------------------------------------------------

def test_the_aws_playbook_installs_the_backup_after_k3s_and_the_homelab_does_not() -> None:
    [play] = yaml.safe_load(AWS_PLAYBOOK.read_text())
    roles = [r["role"] if isinstance(r, dict) else r for r in play["roles"]]
    assert roles[-2:] == ["k3s", "restic_backup"], "the backup reaches Postgres through k3s"
    assert "restic_backup" not in SITE_PLAYBOOK.read_text()


def test_restic_is_the_shared_pinned_version_with_a_recorded_checksum() -> None:
    pinned = versions()["RESTIC_VERSION"]
    assert defaults()["restic_backup_version"] == pinned
    assert re.fullmatch(r"[0-9a-f]{64}", defaults()["restic_backup_sha256"])
    # The homelab backup's image runs the same restic, so both read the same repository format.
    image = re.search(r"image: restic/restic:(\S+)", (REPO_ROOT / "backup" / "cronjob.yaml").read_text())
    assert image and image.group(1) == pinned


def test_restic_is_downloaded_for_that_version_and_checksum_verified() -> None:
    [download] = [t for t in tasks() if "ansible.builtin.get_url" in t]
    args = download["ansible.builtin.get_url"]
    assert args["url"] == ("https://github.com/restic/restic/releases/download/v{{ restic_backup_version }}/"
                           "restic_{{ restic_backup_version }}_linux_amd64.bz2")
    assert args["checksum"] == "sha256:{{ restic_backup_sha256 }}"
    # The binary is unpacked next to its version, so a bump installs a new file instead of trusting an old one.
    assert "{{ restic_backup_version }}" in args["dest"]


def test_the_backup_writes_to_the_bucket_and_password_terraform_made() -> None:
    names = dict(re.findall(r'output "(\w+)" \{.*?value\s*=\s*"([^"]+)"', NAMES.read_text(), re.S))
    assert defaults()["restic_backup_bucket"] == names["backups_bucket"]
    name = defaults()["restic_backup_password_parameter"].removeprefix("/policypal/aws/")
    assert name in secret_catalog() and "namespace" not in secret_catalog()[name], \
        "read by the node itself, so no ExternalSecret and no namespace"


# --- the systemd units --------------------------------------------------------------------------------------

def unit(path: Path) -> dict[str, list[str]]:
    """Key -> values of a unit file (Environment= can repeat)."""
    found: dict[str, list[str]] = {}
    for line in path.read_text().splitlines():
        if "=" in line and not line.startswith(("#", "[")):
            key, value = line.split("=", 1)
            found.setdefault(key.strip(), []).append(value.strip())
    return found


def test_the_service_runs_the_script_once_as_root_with_the_password_command() -> None:
    service = unit(SERVICE)
    assert service["Type"] == ["oneshot"]
    assert service["ExecStart"] == ["/usr/local/sbin/policypal-backup"]
    env = dict(v.split("=", 1) for v in service["Environment"])
    assert env["RESTIC_REPOSITORY"] == "s3:s3.{{ restic_backup_region }}.amazonaws.com/{{ restic_backup_bucket }}/restic"
    assert env["RESTIC_PASSWORD_COMMAND"] == "/usr/local/sbin/ssm-parameter {{ restic_backup_password_parameter }}"
    assert env["AWS_DEFAULT_REGION"] == "{{ restic_backup_region }}"
    assert env["RESTIC_HOST"] == "policypal", "one host name, so snapshots from every rebuilt node form one group"
    assert env["KUBECONFIG"] == "/etc/rancher/k3s/k3s.yaml"
    assert not {"RESTIC_PASSWORD", "RESTIC_PASSWORD_FILE"} & set(env)


def test_the_timer_starts_the_service_every_hour_and_is_enabled() -> None:
    timer = unit(TIMER)
    assert timer["OnCalendar"] == ["hourly"] and timer["Unit"] == ["policypal-backup.service"]
    [enable] = [t for t in tasks() if t.get("ansible.builtin.systemd_service", {}).get("name") == "policypal-backup.timer"]
    assert enable["ansible.builtin.systemd_service"]["enabled"] is True
    assert enable["ansible.builtin.systemd_service"]["state"] == "started"


def test_the_ssm_helper_prints_only_the_decrypted_value(tmp_path: Path) -> None:
    # A stand-in boto3 records the call; the helper must ask for decryption and print the value and nothing else.
    fake = tmp_path / "boto3"
    fake.mkdir()
    (fake / "__init__.py").write_text(
        "import json, os\n"
        "class _SSM:\n"
        "    def get_parameter(self, **kw):\n"
        "        open(os.environ['CALLS'], 'w').write(json.dumps(kw))\n"
        "        return {'Parameter': {'Value': 'the-value'}}\n"
        "def client(service, **kw):\n"
        "    assert service == 'ssm'\n"
        "    return _SSM()\n")
    calls = tmp_path / "calls.json"
    result = subprocess.run(["python3", str(SSM_HELPER), "/policypal/aws/restic-password"], capture_output=True,
                            text=True, env={"PYTHONPATH": str(tmp_path), "CALLS": str(calls), "PATH": "/usr/bin:/bin"})
    assert result.returncode == 0, result.stderr
    assert result.stdout == "the-value\n" and result.stderr == ""
    assert calls.read_text() == '{"Name": "/policypal/aws/restic-password", "WithDecryption": true}'


# --- the backup script --------------------------------------------------------------------------------------

@pytest.fixture
def node(fakes: Fakes, tmp_path: Path) -> tuple[Fakes, Path]:
    uploads = tmp_path / "storage" / f"{VOLUME}_policypal_policypal-uploads"
    uploads.mkdir(parents=True)
    fakes.pvc_volume(VOLUME)
    fakes.pv_path(str(uploads))
    return fakes, uploads


def backup(fakes: Fakes) -> subprocess.CompletedProcess[str]:
    return fakes.script(str(SCRIPT), env={"RESTIC_REPOSITORY": REPOSITORY, "RESTIC_PASSWORD_COMMAND": PASSWORD_COMMAND,
                                          "RESTIC_HOST": "policypal", "KUBECONFIG": "/etc/rancher/k3s/k3s.yaml"})


def test_the_unit_sets_everything_the_script_requires() -> None:
    # The script refuses to run without these (": ${VAR:?}"), and the service is where they are set.
    required = set(re.findall(r'"\$\{(\w+):\?', SCRIPT.read_text()))
    env = dict(v.split("=", 1) for v in unit(SERVICE)["Environment"])
    assert required and required <= set(env), required - set(env)


def restic(fakes: Fakes) -> list[list[str]]:
    return [c["args"] for c in fakes.calls("restic")]


def test_backup_dumps_the_database_first_then_the_uploads_then_applies_retention(node: tuple[Fakes, Path]) -> None:
    fakes, uploads = node
    result = backup(fakes)
    assert result.returncode == 0, output(result)
    calls = restic(fakes)
    assert [c[:2] for c in calls] == [["cat", "config"], ["backup", "--stdin-from-command"], ["backup", "--tag"],
                                      ["forget", "--tag"], ["forget", "--tag"], ["prune"], ["check"]]
    db, files = calls[1], calls[2]
    command = db[db.index("--") + 1:]
    assert command[:5] == ["kubectl", "-n", "policypal", "exec", "statefulset/postgres"]
    assert "pg_dump" in command[-1] and "-Fc" in command[-1], "Postgres's custom format, which pg_restore reads"
    assert db[db.index("--stdin-filename") + 1] == "policypal.dump"
    assert files[-1] == str(uploads)
    run_tags = {t for c in calls[1:3] for t in c[c.index("--tag") + 1].split(",") if t.startswith("run-")}
    assert len(run_tags) == 1, "the dump and the uploads of one run carry the same run tag, to restore them together"
    for forget in calls[3:5]:
        assert forget[forget.index("--keep-daily") + 1] == "7" and forget[forget.index("--keep-weekly") + 1] == "4"
        assert forget[forget.index("--keep-monthly") + 1] == "12"


def test_backup_finds_the_uploads_from_the_volume_not_a_fixed_path(node: tuple[Fakes, Path]) -> None:
    fakes, _ = node
    assert backup(fakes).returncode == 0
    pvc, pv = [c["args"] for c in fakes.calls("kubectl") if c["args"][:1] == ["get"]]
    assert pvc[:3] == ["get", "pvc", "policypal-uploads"] and pv[:3] == ["get", "pv", VOLUME]


def test_restic_gets_the_password_command_and_never_a_password(node: tuple[Fakes, Path]) -> None:
    fakes, _ = node
    assert backup(fakes).returncode == 0
    for call in fakes.calls("restic"):
        assert call["restic_password_command"] == PASSWORD_COMMAND and not call["restic_password_set"]
        assert call["restic_repository"] == REPOSITORY
        assert not any(a.startswith(("--password", "-p")) for a in call["args"]), call["args"]


def test_backup_creates_the_repository_only_when_there_is_none(node: tuple[Fakes, Path]) -> None:
    fakes, _ = node
    fakes.fail("restic", "cat", "config", rc=10)  # restic's exit code for "repository does not exist"
    result = backup(fakes)
    assert result.returncode == 0, output(result)
    assert [c[:1] for c in restic(fakes)][:3] == [["cat"], ["init"], ["backup"]]


@pytest.mark.parametrize("rc", [1, 11, 12])  # an error, a lock held elsewhere, a wrong password
def test_backup_stops_when_the_repository_cant_be_opened(node: tuple[Fakes, Path], rc: int) -> None:
    fakes, _ = node
    fakes.fail("restic", "cat", "config", rc=rc)
    result = backup(fakes)
    assert result.returncode != 0
    assert [c[:1] for c in restic(fakes)] == [["cat"]], "no init over an existing repository, no backup"


def test_backup_fails_before_any_backup_when_the_uploads_directory_is_missing(node: tuple[Fakes, Path]) -> None:
    fakes, uploads = node
    uploads.rmdir()
    result = backup(fakes)
    assert result.returncode != 0
    assert "uploads" in output(result) and str(uploads) in output(result)
    assert not [c for c in restic(fakes) if c[:1] == ["backup"]]


def test_backup_fails_when_no_volume_is_bound(node: tuple[Fakes, Path]) -> None:
    fakes, _ = node
    fakes.pvc_volume("")
    result = backup(fakes)
    assert result.returncode != 0 and "policypal-uploads" in output(result)
    assert not fakes.calls("restic")


@pytest.mark.parametrize("which", ["--stdin-from-command", "--tag"])  # the dump, the uploads
def test_a_failed_backup_exits_non_zero_and_applies_no_retention(node: tuple[Fakes, Path], which: str) -> None:
    # A failed dump (restic keeps no snapshot when its command fails) or a failed uploads backup: the unit must
    # show failed, and old snapshots must not be thinned out after a run that saved nothing.
    fakes, _ = node
    fakes.fail("restic", "backup", which)
    result = backup(fakes)
    assert result.returncode == 1, "restic's own exit code, not a missing script (127)"
    assert [c for c in restic(fakes) if c[:2] == ["backup", which]], "the failing backup must have run"
    assert not [c for c in restic(fakes) if c[:1] in (["forget"], ["prune"], ["check"])]
    if which == "--stdin-from-command":
        assert not [c for c in restic(fakes) if c[:2] == ["backup", "--tag"]], "no uploads without their dump"
