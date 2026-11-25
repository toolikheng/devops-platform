"""Retention must never delete the run a restore needs (both found around the restore test on AWS).

policypal-backup's retention (its KEEP: 7 daily, 4 weekly, 12 monthly) keeps only the newest snapshot of each day.
- A node just built by `make cloud-rebuild` runs a freshly deployed PolicyPal with none of the old data. If its hourly
  backup ran before a restore, that backup would be the day's newest, and the run that ought to be restored would be
  forgotten and then pruned. So the restic_backup role leaves the timer off; a successful restore or
  `policypal-backup enable` turns it on (tests/test_restic_backup.py).
- A restore waits for a backup that is already running. That backup's retention can delete the run the restore was
  asked for, or make a newer one "latest", so the run is looked up only after the wait, and nothing is touched unless
  both of its snapshots are there.

These tests run restic itself, the pinned version (ci/versions.env RESTIC_VERSION, the homelab backup's image),
on a repository in a temporary folder, with the retention arguments read from the script. Snapshot times are set
with --time, so nothing depends on today's date. The restore tests run the script's own restore with that restic and
the fake kubectl and systemctl.
"""

import json
import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import REPO_ROOT, backup_timer_tasks, versions
from fakecli import Fakes, output

SCRIPT = REPO_ROOT / "ansible" / "roles" / "restic_backup" / "files" / "policypal-backup.sh"
HISTORY = [f"run-202611{day}T090000Z" for day in range(16, 25)]  # a backup a day for nine days
GOOD_RUN = "run-20261125T090000Z"  # the last backup before the node was lost
EMPTY_RUN = "run-20261125T100000Z"  # the new node's first hourly backup, an hour later the same day
BUCKET = ["run-20261125T091645Z", "run-20261125T092159Z"]  # the S3 bucket after the restore test on AWS
ASKED = "run-20261125T092159Z"  # the run a restore is asked for (also what "latest" was when it began)
RUNNING = "run-20261125T163000Z"  # a backup already running when the restore begins, later the same (UTC) day


def forget_args(kind: str) -> list[str]:
    """The script's retention for one kind of snapshot (db, uploads), word for word, with its KEEP filled in."""
    text = SCRIPT.read_text()
    keep = re.search(r"^KEEP=\((.*)\)$", text, re.M)
    forget = re.search(rf"^\s*restic (forget --tag policypal-{kind} .*)$", text, re.M)
    assert keep and forget, f"policypal-backup.sh: KEEP=(...) and the {kind} restic forget line"
    args = shlex.split(forget.group(1))
    i = args.index("${KEEP[@]}")
    return args[:i] + keep.group(1).split() + args[i + 1:]


def run_time(run: str) -> str:
    """restic's --time for a run: the time in its name."""
    return re.sub(r"run-(\d{4})(\d\d)(\d\d)T(\d\d)(\d\d)(\d\d)Z", r"\1-\2-\3 \4:\5:\6", run)


class Repository:
    """A restic repository in a folder (repo/, with the files it backs up beside it), used through the pinned restic
    image."""

    def __init__(self, root: Path) -> None:
        self.root = root

    @classmethod
    def new(cls, root: Path) -> "Repository":
        (root / "repo").mkdir(parents=True)
        (root / "data").mkdir()
        (root / "data" / "good.dump").write_text("PGDMP PolicyPal with the policies people uploaded\n")
        (root / "data" / "empty.dump").write_text("PGDMP PolicyPal just deployed, nothing uploaded\n")
        (root / "password").write_text("only-for-this-test\n")
        repository = cls(root)
        repository.restic("init")
        return repository

    def command(self) -> list[str]:
        """restic in the pinned image, with this folder mounted at its own path: a path means the same inside and
        outside the container (a restore's --target, a snapshot's directory). The real docker by its full path: a
        shim runs with the fakes first on PATH, and they include docker."""
        docker = shutil.which("docker")
        assert docker, "docker not installed"
        return [docker, "run", "--rm", "-i", "-u", f"{os.getuid()}:{os.getgid()}", "-v", f"{self.root}:{self.root}",
                "-e", f"RESTIC_REPOSITORY={self.root / 'repo'}", "-e", f"RESTIC_PASSWORD_FILE={self.root / 'password'}",
                "-e", "RESTIC_CACHE_DIR=/tmp/c", f"restic/restic:{versions()['RESTIC_VERSION']}"]

    def shim(self, path: Path) -> Path:
        """An executable at `path` that runs this restic with its arguments (what the script calls `restic`)."""
        path.write_text(f'#!/bin/sh\nexec {shlex.join(self.command())} "$@"\n')
        path.chmod(0o755)
        return path

    def restic(self, *args: str, stdin: str = "") -> str:
        result = subprocess.run([*self.command(), *args], input=stdin, capture_output=True, text=True, timeout=120)
        assert result.returncode == 0, result.stdout + result.stderr
        return result.stdout

    def backup(self, run: str, dump: str) -> None:
        """A database snapshot as policypal-backup tags it (RESTIC_HOST policypal, kind and run), taken at the
        run's own time."""
        self.restic("backup", "--host", "policypal", "--time", run_time(run), "--tag", f"policypal-db,{run}",
                    str(self.root / "data" / dump))

    @staticmethod
    def run_commands(run: str, dump: str, uploads: Path) -> list[tuple[list[str], str]]:
        """policypal-backup's two snapshots of a run, as (restic arguments, stdin): the dump from stdin as
        /policypal.dump, then the uploads directory, both at the run's own time."""
        common = ["backup", "--host", "policypal", "--time", run_time(run)]
        return [([*common, "--tag", f"policypal-db,{run}", "--stdin", "--stdin-filename", "policypal.dump"], dump),
                ([*common, "--tag", f"policypal-uploads,{run}", str(uploads)], "")]

    def backup_run(self, run: str, dump: str, uploads: Path) -> None:
        for args, stdin in self.run_commands(run, dump, uploads):
            self.restic(*args, stdin=stdin)

    def runs(self) -> set[str]:
        return {t for s in json.loads(self.restic("snapshots", "--json")) for t in s["tags"] if t.startswith("run-")}


@pytest.fixture(scope="module")
def history(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Nine days of backups, then today's good one: built once, copied for each test."""
    repository = Repository.new(tmp_path_factory.mktemp("history"))
    for run in [*HISTORY, GOOD_RUN]:
        repository.backup(run, "good.dump")
    return repository.root


@pytest.fixture
def repository(history: Path, tmp_path: Path) -> Repository:
    return Repository(Path(shutil.copytree(history, tmp_path / "copy")))


def an_hour_on_a_new_node(repository: Repository) -> None:
    """The first hour of a node fresh from `make cloud-config`, before anyone restores: if the role turned the hourly
    timer on, policypal-backup backup runs, saves the new node's data and applies the script's retention. Any role
    task that manages the timer counts as turning it on: the role must leave it alone (test_restic_backup.py)."""
    if backup_timer_tasks():
        repository.backup(EMPTY_RUN, "empty.dump")
        repository.restic(*forget_args("db"))


def test_retention_keeps_only_the_newest_backup_of_a_day(repository: Repository) -> None:
    # Why the timer has to wait: one later backup on the same day, and the earlier one is forgotten.
    repository.backup(EMPTY_RUN, "empty.dump")
    repository.restic(*forget_args("db"))
    runs = repository.runs()
    assert EMPTY_RUN in runs and GOOD_RUN not in runs


def test_a_new_node_does_not_push_the_last_good_backup_out(repository: Repository) -> None:
    an_hour_on_a_new_node(repository)
    assert GOOD_RUN in repository.runs(), "the run to restore was forgotten after the new node's first backup"


# --- a restore while a backup is running -------------------------------------------------------------------

@pytest.fixture(scope="module")
def bucket(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The bucket's two runs, both kinds, from the old node: built once, copied for each test."""
    repository = Repository.new(tmp_path_factory.mktemp("bucket"))
    uploads = repository.root / "old-node-uploads"
    uploads.mkdir()
    for run in BUCKET:
        (uploads / f"{run}.md").write_text(f"uploaded before {run}\n")
        repository.backup_run(run, f"PGDMP {run}", uploads)
    return repository.root


def a_backup_finishes_during_the_wait(fakes: Fakes, repository: Repository, uploads: Path) -> None:
    """systemctl on a node whose hourly backup was running when the restore began: the restore's first look sees the
    service "activating", and by its next look the backup has finished, the script's own retention included
    (`restic` here is the real one, not the logging fake, as on the node); "inactive" from then on."""
    restic = repository.shim(fakes.dir / "restic-of-the-running-backup")
    steps = [f"printf %s {shlex.quote(stdin)} | {restic} {shlex.join(args)}"
             for args, stdin in Repository.run_commands(RUNNING, f"PGDMP {RUNNING}", uploads)]
    steps += [f"{restic} {shlex.join(forget_args(kind))}" for kind in ("db", "uploads")]
    finished = fakes.dir / "running-backup-finished"
    systemctl = fakes.dir / "systemctl-on-the-node"
    systemctl.write_text(
        "#!/bin/bash\nset -euo pipefail\n"
        '[ "$1" = is-active ] || exit 0\n'
        f"if [ ! -e {finished} ]; then\n"
        + "".join(f"  {step} >/dev/null 2>>{fakes.dir / 'running-backup.err'}\n" for step in steps)
        + f"  touch {finished}\n  echo activating\nelse\n  echo inactive\nfi\nexit 3\n")
    systemctl.chmod(0o755)
    fakes.instead("systemctl", str(systemctl))


@pytest.fixture
def restoring(bucket: Path, fakes: Fakes) -> tuple[Fakes, Repository]:
    """A node with the bucket's repository and its own uploads, whose hourly backup is still running when the
    restore begins."""
    repository = Repository(Path(shutil.copytree(bucket, fakes.dir / "copy")))
    uploads = repository.root / "node-uploads"
    uploads.mkdir()
    (uploads / "on-the-node.md").write_text("uploaded on the node\n")
    fakes.pvc_volume("pvc-1234abcd-0000-4000-8000-123456789abc")
    fakes.pv_path(str(uploads))
    fakes.instead("restic", str(repository.shim(fakes.dir / "restic-in-docker")))
    a_backup_finishes_during_the_wait(fakes, repository, uploads)
    return fakes, repository


def restore(fakes: Fakes, which: str) -> subprocess.CompletedProcess[str]:
    settings = fakes.dir / "policypal-backup.env"
    settings.write_text("RESTIC_REPOSITORY=s3:example/restic\nRESTIC_PASSWORD_COMMAND=false\nRESTIC_HOST=policypal\n"
                        "KUBECONFIG=/etc/rancher/k3s/k3s.yaml\n")
    return fakes.script(str(SCRIPT), "restore", which, env={"POLICYPAL_BACKUP_ENV": str(settings), "POLL_SECONDS": "0"})


def test_a_run_deleted_by_the_backup_the_restore_waited_for_is_refused_before_anything_is_touched(
        restoring: tuple[Fakes, Repository]) -> None:
    # Found reading the restore before the demo session (2026-11-26): it looked the run up, then waited for the running
    # backup, whose retention deleted that run; the database was already dropped when restic dump failed.
    fakes, repository = restoring
    result = restore(fakes, ASKED)
    assert {RUNNING} <= repository.runs() and ASKED not in repository.runs(), \
        "the backup that was running finished during the wait, and its retention deleted the run"
    assert not fakes.calls("kubectl", "scale") and not fakes.calls("kubectl", "exec"), \
        "nothing may be touched (API, database) for a run that is gone:\n" + output(result)
    assert result.returncode == 1 and f"no database backup for {ASKED}" in output(result)
    assert "policypal-backup enable" in output(result), "it says the backups are off and how to turn them back on"


def test_restore_latest_takes_the_newest_run_once_the_running_backup_has_finished(
        restoring: tuple[Fakes, Repository]) -> None:
    fakes, _ = restoring
    result = restore(fakes, "latest")
    assert result.returncode == 0, output(result)
    assert f"policypal-backup: restored {RUNNING}: database " in result.stdout
    [load] = [c for c in fakes.calls("kubectl") if c["args"][:2] == ["exec", "-i"]]
    assert load["stdin"] == f"PGDMP {RUNNING}", "the newest dump, read from the repository by restic"
