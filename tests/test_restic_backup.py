"""The node's backup of PolicyPal to S3: the Ansible role restic_backup (playbooks/aws-node.yml).

- restic is pinned (ci/versions.env, shared with the homelab backup's image) and its download is checked against a
  recorded sha256.
- /usr/local/sbin/policypal-backup (files/policypal-backup.sh) has four verbs. `backup`, run every hour by a
  systemd timer: the Postgres dump first, then the uploads directory, then restic's retention; any failure exits
  non-zero, so systemd marks the run failed, and a success writes the time for node_exporter's textfile collector.
  `show RUN|latest`: a run's two snapshots. `restore RUN|latest`: the database and the uploads of one run, with the
  API stopped meanwhile, then the hourly timer on. `enable`: the hourly timer on, for a node with nothing to restore.
- A new node leaves the timer off: its first backup would push the last good one out of the repository
  (test_backup_retention.py shows it with restic itself).
- The settings (no secrets) are in /etc/policypal-backup.env, read by the service and by a restore by hand. The
  repository password comes from SSM Parameter Store at run time through RESTIC_PASSWORD_COMMAND: never on disk,
  in a log or on a command line.

The script runs here with the fake CLIs of tests/fakecli.py (kubectl, restic), so nothing is installed or sent.
"""

import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from conftest import REPO_ROOT, backup_timer_tasks, secret_catalog, versions
from fakecli import Fakes, output

ROLE = REPO_ROOT / "ansible" / "roles" / "restic_backup"
SCRIPT = ROLE / "files" / "policypal-backup.sh"
SSM_HELPER = ROLE / "files" / "ssm-parameter"
SERVICE = ROLE / "templates" / "policypal-backup.service.j2"
ENV_FILE = ROLE / "templates" / "policypal-backup.env.j2"
GROUP_VARS = REPO_ROOT / "ansible" / "inventory" / "group_vars" / "aws.yml"
NODE_EXPORTER = REPO_ROOT / "ansible" / "roles" / "node_exporter"
DASHBOARD = REPO_ROOT / "k8s" / "policypal" / "overlays" / "aws" / "policypal-dashboard.json"
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


def env_lines(text: str) -> dict[str, str]:
    """KEY=value lines (values may be double-quoted), as systemd's EnvironmentFile and bash read them."""
    return {k: v.strip('"') for k, v in re.findall(r"^([A-Z_]+)=(.*)$", text, re.M)}


def rendered_env() -> str:
    """The settings file with the role's defaults filled in (the only template expressions it uses)."""
    return re.sub(r"\{\{ (\w+) \}\}", lambda m: str(defaults()[m.group(1)]), ENV_FILE.read_text())


def test_the_service_runs_a_backup_once_with_the_settings_file() -> None:
    service = unit(SERVICE)
    assert service["Type"] == ["oneshot"]
    assert service["ExecStart"] == ["/usr/local/sbin/policypal-backup backup"]
    assert service["EnvironmentFile"] == ["/etc/policypal-backup.env"]
    assert "Environment" not in service, "one place for the settings: the file a restore by hand reads too"


def test_the_settings_point_restic_at_the_bucket_and_the_password_command() -> None:
    env = env_lines(ENV_FILE.read_text())
    assert env["RESTIC_REPOSITORY"] == "s3:s3.{{ restic_backup_region }}.amazonaws.com/{{ restic_backup_bucket }}/restic"
    assert env["RESTIC_PASSWORD_COMMAND"] == "/usr/local/sbin/ssm-parameter {{ restic_backup_password_parameter }}"
    assert env["AWS_DEFAULT_REGION"] == "{{ restic_backup_region }}"
    assert env["RESTIC_HOST"] == "policypal", "one host name, so snapshots from every rebuilt node form one group"
    assert env["KUBECONFIG"] == "/etc/rancher/k3s/k3s.yaml"
    assert env["BACKUP_TEXTFILE_DIR"] == "{{ restic_backup_textfile_dir }}"
    assert not {"RESTIC_PASSWORD", "RESTIC_PASSWORD_FILE"} & set(env)
    [task] = [t for t in tasks() if t.get("ansible.builtin.template", {}).get("dest") == "/etc/policypal-backup.env"]
    assert task["ansible.builtin.template"]["mode"] == "0644", "no secret in it, only how to fetch one"


def test_the_timer_runs_a_backup_every_hour_but_a_new_node_leaves_it_off() -> None:
    # A new node's PolicyPal is empty until a restore. Retention keeps only the newest snapshot of each day, so the
    # new node's first hourly backup would push the last good one out (test_backup_retention.py). A successful
    # restore or `policypal-backup enable` turns the timer on. The role doesn't turn it off either: a second
    # cloud-config must not stop the backups a restore started.
    timer = unit(TIMER)
    assert timer["OnCalendar"] == ["hourly"] and timer["Unit"] == ["policypal-backup.service"]
    assert timer["WantedBy"] == ["timers.target"], "systemctl enable needs the [Install] section"
    assert not backup_timer_tasks(), backup_timer_tasks()


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
    return run(fakes, "backup")


def run(fakes: Fakes, *args: str) -> subprocess.CompletedProcess[str]:
    """The script as a root shell runs it: its settings from the settings file (rendered from the template), with
    the textfile directory moved into this test's folder."""
    textfile = fakes.dir / "textfile"
    textfile.mkdir(exist_ok=True)
    settings = fakes.dir / "policypal-backup.env"
    settings.write_text(rendered_env().replace(defaults()["restic_backup_textfile_dir"], str(textfile)))
    return fakes.script(str(SCRIPT), *args, env={"POLICYPAL_BACKUP_ENV": str(settings)})


def test_the_settings_file_sets_everything_the_script_requires() -> None:
    # The script refuses to run without these (": ${VAR:?}").
    required = set(re.findall(r'"\$\{(\w+):\?', SCRIPT.read_text()))
    assert required and required <= set(env_lines(ENV_FILE.read_text())), required


def restic(fakes: Fakes) -> list[list[str]]:
    return [c["args"] for c in fakes.calls("restic")]


def systemctl(fakes: Fakes) -> list[list[str]]:
    return [c["args"] for c in fakes.calls("systemctl")]


TIMER_ON = ["enable", "--now", "policypal-backup.timer"]


def test_enable_turns_the_hourly_backups_on(fakes: Fakes) -> None:
    # A new environment, nothing to restore: the owner starts the hourly backups by hand, once.
    result = run(fakes, "enable")
    assert result.returncode == 0, output(result)
    assert systemctl(fakes) == [TIMER_ON]
    assert not fakes.calls("restic") and not fakes.calls("kubectl")


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


# --- the last successful backup, for node_exporter's textfile collector --------------------------------------

def test_node_exporter_reads_the_textfile_directory_on_aws_only() -> None:
    aws = yaml.safe_load(GROUP_VARS.read_text())
    assert aws["node_exporter_textfile_dir"] == "/var/lib/node_exporter/textfile"
    assert defaults()["restic_backup_textfile_dir"] == aws["node_exporter_textfile_dir"]
    # The homelab nodes keep their flags: the option is off unless a directory is set.
    node_defaults = yaml.safe_load((NODE_EXPORTER / "defaults" / "main.yml").read_text())
    assert node_defaults["node_exporter_textfile_dir"] == ""
    for template in ("node_exporter.service.j2", "node_exporter.init.j2"):
        text = (NODE_EXPORTER / "templates" / template).read_text()
        assert "--collector.textfile.directory=" in text and "if node_exporter_textfile_dir" in text, template
    node_tasks = yaml.safe_load((NODE_EXPORTER / "tasks" / "main.yml").read_text())
    [create] = [t for t in node_tasks if t.get("ansible.builtin.file", {}).get("path") == "{{ node_exporter_textfile_dir }}"]
    assert create["when"] == "node_exporter_textfile_dir | length > 0"


def textfile(fakes: Fakes) -> Path:
    return fakes.dir / "textfile" / "policypal_backup.prom"


def test_a_successful_backup_records_its_time_for_prometheus(node: tuple[Fakes, Path]) -> None:
    fakes, _ = node
    import time
    before = int(time.time())
    assert backup(fakes).returncode == 0
    after = int(time.time())
    lines = textfile(fakes).read_text().splitlines()
    assert "# TYPE policypal_backup_last_success_timestamp_seconds gauge" in lines
    [sample] = [line for line in lines if not line.startswith("#")]
    name, value = sample.split()
    assert name == "policypal_backup_last_success_timestamp_seconds" and before <= int(value) <= after
    assert [p.name for p in textfile(fakes).parent.iterdir()] == ["policypal_backup.prom"], "no temporary file left"


@pytest.mark.parametrize("which", ["--stdin-from-command", "--tag"])
def test_a_failed_backup_leaves_the_last_success_time_alone(node: tuple[Fakes, Path], which: str) -> None:
    fakes, _ = node
    (fakes.dir / "textfile").mkdir()
    earlier = "policypal_backup_last_success_timestamp_seconds 1000000000\n"
    textfile(fakes).write_text(earlier)
    fakes.fail("restic", "backup", which)
    assert backup(fakes).returncode == 1
    assert textfile(fakes).read_text() == earlier


def test_a_failed_first_backup_records_nothing(node: tuple[Fakes, Path]) -> None:
    fakes, _ = node
    fakes.fail("restic", "backup", "--tag")
    assert backup(fakes).returncode == 1
    assert list((fakes.dir / "textfile").iterdir()) == []


def test_the_dashboard_shows_the_time_since_the_last_backup() -> None:
    import json
    panels = json.loads(DASHBOARD.read_text())["panels"]
    [panel] = [p for p in panels if "policypal_backup_last_success_timestamp_seconds" in p["targets"][0]["expr"]]
    assert panel["targets"][0]["expr"] == "time() - max(policypal_backup_last_success_timestamp_seconds)"
    assert panel["fieldConfig"]["defaults"]["unit"] == "s"


# --- show and restore ----------------------------------------------------------------------------------------

OLD_UPLOADS = "/var/lib/rancher/k3s/storage/pvc-old_policypal_policypal-uploads"
DB_A, DB_B, UP_B = "a" * 64, "b" * 64, "c" * 64


def snapshot(sid: str, time: str, kind: str, run: str, path: str) -> dict[str, Any]:
    return {"id": sid, "short_id": sid[:8], "time": time, "tags": [kind, run], "paths": [path], "hostname": "policypal"}


@pytest.fixture
def backups(node: tuple[Fakes, Path]) -> tuple[Fakes, Path]:
    """Two runs in the repository; the newer one (run-B) is complete."""
    fakes, uploads = node
    fakes.restic_snapshots("policypal-db", [
        snapshot(DB_A, "2026-11-25T01:00:00Z", "policypal-db", "run-20261125T010000Z", "/policypal.dump"),
        snapshot(DB_B, "2026-11-25T02:00:00Z", "policypal-db", "run-20261125T020000Z", "/policypal.dump")])
    fakes.restic_snapshots("policypal-db,run-20261125T020000Z", [
        snapshot(DB_B, "2026-11-25T02:00:00Z", "policypal-db", "run-20261125T020000Z", "/policypal.dump")])
    fakes.restic_snapshots("policypal-uploads,run-20261125T020000Z", [
        snapshot(UP_B, "2026-11-25T02:00:05Z", "policypal-uploads", "run-20261125T020000Z", OLD_UPLOADS)])
    fakes.restic_dump("PGDMP the dump")
    return fakes, uploads


@pytest.mark.parametrize("which", ["latest", "run-20261125T020000Z"])
def test_show_prints_a_runs_two_snapshots(backups: tuple[Fakes, Path], which: str) -> None:
    fakes, _ = backups
    result = run(fakes, "show", which)
    assert result.returncode == 0, output(result)
    assert result.stdout.split() == ["run-20261125T020000Z", DB_B, UP_B]
    assert not [c for c in restic(fakes) if c[:1] in (["backup"], ["restore"], ["forget"], ["prune"], ["init"])]


def test_show_refuses_a_run_without_its_uploads(backups: tuple[Fakes, Path]) -> None:
    fakes, _ = backups
    fakes.restic_snapshots("policypal-db,run-20261125T010000Z", [
        snapshot(DB_A, "2026-11-25T01:00:00Z", "policypal-db", "run-20261125T010000Z", "/policypal.dump")])
    result = run(fakes, "show", "run-20261125T010000Z")
    assert result.returncode == 1 and "no uploads backup for run-20261125T010000Z" in output(result)


def test_restore_stops_the_api_restores_the_database_then_the_uploads_then_starts_it(
        backups: tuple[Fakes, Path]) -> None:
    fakes, uploads = backups
    result = run(fakes, "restore", "run-20261125T020000Z")
    assert result.returncode == 0, output(result)
    steps = [(c["tool"], c["args"]) for c in fakes.calls() if c["tool"] in ("kubectl", "restic")
             and c["args"][:1] in (["scale"], ["get"], ["exec"], ["dump"], ["restore"], ["rollout"])
             and c["args"][:2] not in (["get", "pvc"], ["get", "pv"])]
    names = [(tool, args[0]) for tool, args in steps]
    assert names == [("kubectl", "scale"), ("kubectl", "get"), ("kubectl", "get"), ("kubectl", "exec"),
                     ("restic", "dump"), ("kubectl", "exec"), ("restic", "restore"), ("kubectl", "scale"),
                     ("kubectl", "rollout")]
    assert "--replicas=0" in steps[0][1] and "--replicas=1" in steps[7][1]
    assert steps[1][1][:2] == ["get", "pods"] and "app.kubernetes.io/name=policypal-api" in steps[1][1], \
        "the database is touched only once the API pod is gone"
    assert "dropdb" in steps[3][1][-1] and "createdb" in steps[3][1][-1], "a clean database first"
    assert steps[4][1] == ["dump", DB_B, "/policypal.dump"]
    [restore_db] = [c for c in fakes.calls("kubectl") if c["args"][:2] == ["exec", "-i"]]
    assert "pg_restore" in restore_db["args"][-1] and restore_db["stdin"] == "PGDMP the dump"
    assert steps[6][1] == ["restore", f"{UP_B}:{OLD_UPLOADS}", "--target", str(uploads), "--delete"]
    assert "run-20261125T020000Z" in result.stdout and DB_B in result.stdout and UP_B in result.stdout


@pytest.mark.parametrize("failing", [("kubectl", "exec", "-i"), ("restic", "restore")])
def test_a_failed_restore_leaves_the_api_stopped_and_says_so(backups: tuple[Fakes, Path],
                                                            failing: tuple[str, ...]) -> None:
    # Starting PolicyPal on half-restored data could delete database rows (ingestion removes rows whose upload is
    # missing), so the API stays at 0 replicas until someone looks. Backups stay off too: they would save that.
    fakes, _ = backups
    fakes.fail(*failing)
    result = run(fakes, "restore", "run-20261125T020000Z")
    assert result.returncode == 1
    assert [c for c in fakes.calls(failing[0]) if c["args"][:len(failing) - 1] == list(failing[1:])], "it ran"
    assert not [c for c in fakes.calls("kubectl") if c["args"][:1] == ["scale"] and "--replicas=1" in c["args"]]
    assert "0 replicas" in output(result)
    assert not systemctl(fakes)


def test_a_successful_restore_turns_the_hourly_backups_on_last(backups: tuple[Fakes, Path]) -> None:
    # Only once the API runs on the restored data: from then on, a backup saves that data.
    fakes, _ = backups
    result = run(fakes, "restore", "latest")
    assert result.returncode == 0, output(result)
    steps = [(c["tool"], c["args"][0]) for c in fakes.calls() if c["tool"] in ("kubectl", "restic", "systemctl")]
    assert steps[-2:] == [("kubectl", "rollout"), ("systemctl", "enable")]
    assert systemctl(fakes) == [TIMER_ON]


def test_a_restore_whose_api_does_not_come_back_leaves_the_backups_off(backups: tuple[Fakes, Path]) -> None:
    fakes, _ = backups
    fakes.fail("kubectl", "rollout")
    result = run(fakes, "restore", "latest")
    assert result.returncode != 0
    assert fakes.calls("kubectl", "rollout"), "it got that far"
    assert not systemctl(fakes)


def test_backup_and_show_leave_the_timer_alone(backups: tuple[Fakes, Path]) -> None:
    fakes, _ = backups
    assert run(fakes, "show", "latest").returncode == 0
    assert backup(fakes).returncode == 0
    assert not systemctl(fakes)


def test_restore_checks_the_uploads_directory_before_stopping_anything(backups: tuple[Fakes, Path]) -> None:
    fakes, uploads = backups
    uploads.rmdir()
    result = run(fakes, "restore", "latest")
    assert result.returncode != 0 and str(uploads) in output(result)
    assert not [c for c in fakes.calls("kubectl") if c["args"][:1] == ["scale"]]


def test_restore_never_backs_up_or_deletes_snapshots(backups: tuple[Fakes, Path]) -> None:
    fakes, _ = backups
    assert run(fakes, "restore", "latest").returncode == 0
    assert not [c for c in restic(fakes) if c[:1] in (["backup"], ["forget"], ["prune"], ["init"])]


@pytest.mark.parametrize("args", [[], ["restore"], ["show"], ["delete", "latest"], ["enable", "now"]])
def test_usage(fakes: Fakes, args: list[str]) -> None:
    result = run(fakes, *args)
    assert result.returncode == 2 and "usage:" in result.stderr
    assert not fakes.calls("restic") and not fakes.calls("kubectl") and not fakes.calls("systemctl")
