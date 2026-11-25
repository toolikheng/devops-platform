"""A new node must not push the last good backup out of the repository (found in the restore test on AWS).

policypal-backup's retention (its KEEP: 7 daily, 4 weekly, 12 monthly) keeps only the newest snapshot of each day.
A node just built by `make cloud-rebuild` runs a freshly deployed PolicyPal with none of the old data. If its hourly
backup ran before a restore, that backup would be the day's newest, and the run that ought to be restored would be
forgotten and then pruned. So the restic_backup role leaves the timer off; a successful restore or
`policypal-backup enable` turns it on (tests/test_restic_backup.py).

These tests run restic itself, the pinned version (ci/versions.env RESTIC_VERSION, the homelab backup's image),
on a repository in a temporary folder, with the retention arguments read from the script. Snapshot times are set
with --time, so nothing depends on today's date. The database snapshots stand for both kinds: the script applies
the same retention to the uploads.
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

SCRIPT = REPO_ROOT / "ansible" / "roles" / "restic_backup" / "files" / "policypal-backup.sh"
HISTORY = [f"run-202611{day}T090000Z" for day in range(16, 25)]  # a backup a day for nine days
GOOD_RUN = "run-20261125T090000Z"  # the last backup before the node was lost
EMPTY_RUN = "run-20261125T100000Z"  # the new node's first hourly backup, an hour later the same day


def forget_args() -> list[str]:
    """The script's retention for the database snapshots, word for word, with its KEEP filled in."""
    text = SCRIPT.read_text()
    keep = re.search(r"^KEEP=\((.*)\)$", text, re.M)
    forget = re.search(r"^\s*restic (forget --tag policypal-db .*)$", text, re.M)
    assert keep and forget, "policypal-backup.sh: KEEP=(...) and the database's restic forget line"
    args = shlex.split(forget.group(1))
    i = args.index("${KEEP[@]}")
    return args[:i] + keep.group(1).split() + args[i + 1:]


class Repository:
    """A restic repository in a folder (repo/, with the files it backs up in data/), used through the pinned restic
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

    def restic(self, *args: str) -> str:
        result = subprocess.run(
            ["docker", "run", "--rm", "-u", f"{os.getuid()}:{os.getgid()}",
             "-v", f"{self.root / 'repo'}:/repo", "-v", f"{self.root / 'data'}:/data:ro",
             "-v", f"{self.root / 'password'}:/password:ro",
             "-e", "RESTIC_REPOSITORY=/repo", "-e", "RESTIC_PASSWORD_FILE=/password", "-e", "RESTIC_CACHE_DIR=/tmp/c",
             f"restic/restic:{versions()['RESTIC_VERSION']}", *args],
            capture_output=True, text=True, timeout=120)
        assert result.returncode == 0, result.stdout + result.stderr
        return result.stdout

    def backup(self, run: str, dump: str) -> None:
        """A database snapshot as policypal-backup tags it (RESTIC_HOST policypal, kind and run), taken at the
        run's own time."""
        when = re.sub(r"run-(\d{4})(\d\d)(\d\d)T(\d\d)(\d\d)(\d\d)Z", r"\1-\2-\3 \4:\5:\6", run)
        self.restic("backup", "--host", "policypal", "--time", when, "--tag", f"policypal-db,{run}", f"/data/{dump}")

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
        repository.restic(*forget_args())


def test_retention_keeps_only_the_newest_backup_of_a_day(repository: Repository) -> None:
    # Why the timer has to wait: one later backup on the same day, and the earlier one is forgotten.
    repository.backup(EMPTY_RUN, "empty.dump")
    repository.restic(*forget_args())
    runs = repository.runs()
    assert EMPTY_RUN in runs and GOOD_RUN not in runs


def test_a_new_node_does_not_push_the_last_good_backup_out(repository: Repository) -> None:
    an_hour_on_a_new_node(repository)
    assert GOOD_RUN in repository.runs(), "the run to restore was forgotten after the new node's first backup"
