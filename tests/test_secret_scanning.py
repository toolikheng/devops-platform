"""Secret scanning: gitleaks with its default rules plus one rule for literal *_PASSWORD= assignments.

The extra rule exists because the default rules missed two restic passwords committed in 7ca581f (found by
hand in C0, recorded in docs/runbook.md): `RESTIC_PASSWORD="${RESTIC_PASSWORD:-<literal>}"`. These tests run
the pinned gitleaks image (the one ci/gitleaks.sh uses) against that commit, against lines written at run
time, and against the whole history, and check the pre-commit hook and the scan of staged changes.

Reports are always redacted, and the test lines are assembled at run time, so this file never matches the
rule it tests and no value is ever printed.
"""

import json
import re
import secrets
import shutil
import string
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
GITLEAKS_SH = REPO_ROOT / "ci" / "gitleaks.sh"
CONFIG = REPO_ROOT / ".gitleaks.toml"
IGNORE = REPO_ROOT / ".gitleaksignore"
PRE_COMMIT = REPO_ROOT / ".pre-commit-config.yaml"
LEAK_COMMIT = "7ca581f"
RULE = "plain-password-assignment"
PW = "_PASS" + "WORD"  # in two parts, so this file never matches the rule


def gitleaks_image() -> str:
    found = re.search(r'^GITLEAKS_IMAGE="([^"]+)"', GITLEAKS_SH.read_text(), re.M)
    assert found, "ci/gitleaks.sh must pin GITLEAKS_IMAGE"
    return found[1]


def gitleaks(tmp: Path, mounts: dict[Path, str], *args: str) -> list[dict[str, Any]]:
    """Run the pinned gitleaks image with a redacted JSON report; return its findings."""
    out = tmp / "out"
    out.mkdir()
    out.chmod(0o777)  # the container's user differs from ours
    volumes = [x for host, inside in mounts.items() for x in ("-v", f"{host}:{inside}:ro")]
    result = subprocess.run(
        ["docker", "run", "--rm", *volumes, "-v", f"{out}:/out",
         "-e", "GIT_CONFIG_COUNT=1", "-e", "GIT_CONFIG_KEY_0=safe.directory", "-e", "GIT_CONFIG_VALUE_0=*",
         gitleaks_image(), *args, "--redact", "--no-banner", "--exit-code", "0",
         "--report-format", "json", "--report-path", "/out/report.json"],
        capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads((out / "report.json").read_text())


def leak_commit() -> str:
    result = subprocess.run(["git", "-C", str(REPO_ROOT), "rev-parse", "--verify", f"{LEAK_COMMIT}^{{commit}}"],
                            capture_output=True, text=True)
    assert result.returncode == 0, f"commit {LEAK_COMMIT} is not in this clone (a shallow checkout?)"
    return result.stdout.strip()


def scan_leak_commit(tmp: Path, config: Path) -> list[dict[str, Any]]:
    """Scan only 7ca581f. Only .git is mounted, so the repo's .gitleaksignore can't hide anything."""
    (tmp / "empty").write_text("")
    return gitleaks(tmp, {REPO_ROOT / ".git": "/scan/.git", config.parent: "/cfg", tmp / "empty": "/empty"},
                    "git", "/scan", f"--log-opts=-1 {leak_commit()}", "--config", f"/cfg/{config.name}",
                    "--gitleaks-ignore-path", "/empty")


@pytest.fixture(scope="module")
def leak_findings(tmp_path_factory: pytest.TempPathFactory) -> list[dict[str, Any]]:
    return scan_leak_commit(tmp_path_factory.mktemp("leak"), CONFIG)


# --- the rule -------------------------------------------------------------------------------------------

def test_default_rules_alone_miss_the_passwords_in_7ca581f(tmp_path: Path) -> None:
    # Why the rule exists. A config with only the defaults finds nothing in that commit.
    config = tmp_path / "cfg" / "defaults.toml"
    config.parent.mkdir()
    config.write_text("[extend]\nuseDefault = true\n")
    assert scan_leak_commit(tmp_path, config) == []


def test_the_rule_catches_both_passwords_committed_in_7ca581f(leak_findings: list) -> None:
    ours = [f for f in leak_findings if f["RuleID"] == RULE]
    assert sorted(f["File"] for f in ours) == ["backup/scripts/backup.sh", "backup/scripts/restore-test.sh"]
    # The `export RESTIC_PASSWORD="$RESTIC_PASSWORD"` line after each one is a reference, not a finding.
    assert len(leak_findings) == 2
    assert all(f["Secret"] == "REDACTED" for f in leak_findings)


LITERAL_SAFE = string.ascii_letters + string.digits


def lines_under_test() -> list[tuple[str, str | None]]:
    """(line, rule that must flag it or None). Values are made at run time."""
    weak = "summer-holiday-in-penang"  # low entropy, like the 7ca581f passwords: the rule must not need entropy
    strong = "".join(secrets.choice(LITERAL_SAFE) for _ in range(24))
    token = "ghp" + "_" + "".join(secrets.choice(LITERAL_SAFE) for _ in range(36))
    return [
        (f'RESTIC{PW}="${{RESTIC{PW}:-{weak}}}"', RULE),             # the 7ca581f shape
        (f"DB{PW}={strong}", RULE),
        (f"export ADMIN{PW}='{weak}'", RULE),
        (f"kubectl create secret generic db --from-literal=POSTGRES{PW}={strong}", RULE),
        (f'CACHE{PW}="${{CACHE{PW}:={weak}}}"', RULE),
        (f'export RESTIC{PW}="$RESTIC{PW}"', None),                 # references and commands
        (f"DB{PW}=$(openssl rand -hex 24)", None),
        (f'DB{PW}="${{DB{PW}}}"', None),
        (f'DB{PW}="${{DB{PW}:?set DB{PW} first}}"', None),
        (f"printf 'POSTGRES{PW}=%s\\n' \"$password\"", None),       # policypal-secrets.sh
        (f"            - name: RESTIC{PW}", None),                   # a YAML env var name
        (f"gitleaks' default rules don't match plain `NAME{PW}=value` lines", None),  # the runbook
        (f"DB{PW}=<your-password-here>", None),                      # a placeholder
        (f"# one rule for plain *{PW}= assignments", None),          # prose (caught by the hook in C5)
        (f"DB{PW}=", None),                                          # empty: must not run into the next line
        (f"token={token}", "github-pat"),                            # the default rules still apply
    ]


def test_the_rule_flags_literal_passwords_and_nothing_else(tmp_path: Path) -> None:
    cases = lines_under_test()
    scan = tmp_path / "scan"
    scan.mkdir()
    (scan / "lines.sh").write_text("\n".join(line for line, _ in cases) + "\n")
    shutil.copy(CONFIG, tmp_path / "gitleaks.toml")
    findings = gitleaks(tmp_path, {scan: "/scan", tmp_path / "gitleaks.toml": "/cfg/gitleaks.toml"},
                        "dir", "/scan", "--config", "/cfg/gitleaks.toml")
    by_line: dict[int, set[str]] = {}
    for f in findings:
        by_line.setdefault(f["StartLine"], set()).add(f["RuleID"])
        assert f["Secret"] == "REDACTED"
    for number, (line, rule) in enumerate(cases, start=1):
        shown = line.split("=")[0]  # never the value
        if rule:
            assert rule in by_line.get(number, set()), f"line {number} ({shown}=...) must be flagged by {rule}"
        else:
            assert number not in by_line, f"line {number} ({shown}...) flagged by {by_line[number]}"


# --- the known findings, and the whole history ----------------------------------------------------------

def test_gitleaksignore_lists_exactly_the_two_7ca581f_findings(leak_findings: list) -> None:
    # Those passwords were never used outside throwaway k3d clusters (runbook, 2026-11-20); history is not
    # rewritten. Anything else added here would hide a new finding.
    listed = [line for line in IGNORE.read_text().splitlines() if line.strip() and not line.startswith("#")]
    assert sorted(listed) == sorted(f["Fingerprint"] for f in leak_findings if f["RuleID"] == RULE)


def test_full_history_is_clean_with_the_rule() -> None:
    result = subprocess.run(["bash", str(GITLEAKS_SH)], cwd=REPO_ROOT, capture_output=True, text=True,
                            timeout=300)
    assert result.returncode == 0, result.stdout + result.stderr


# --- staged changes and the pre-commit hook -------------------------------------------------------------

def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def test_staged_scan_blocks_a_staged_password_and_passes_clean_changes(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    shutil.copy(CONFIG, repo / ".gitleaks.toml")
    (repo / "clean.sh").write_text(f'export RESTIC{PW}="$RESTIC{PW}"\n')
    git(repo, "add", ".")

    def staged_scan() -> subprocess.CompletedProcess[str]:
        return subprocess.run(["bash", str(GITLEAKS_SH), "--staged"], cwd=repo, capture_output=True, text=True,
                              timeout=300)

    clean = staged_scan()
    assert clean.returncode == 0, clean.stdout + clean.stderr

    value = "".join(secrets.choice(LITERAL_SAFE) for _ in range(24))
    (repo / "leak.sh").write_text(f"DB{PW}={value}\n")
    (repo / "unstaged.sh").write_text(f"OTHER{PW}={value}\n")
    git(repo, "add", "leak.sh")
    blocked = staged_scan()
    assert blocked.returncode == 1, blocked.stdout + blocked.stderr
    assert value not in blocked.stdout + blocked.stderr, "the report must be redacted"
    assert "leak.sh" in blocked.stdout + blocked.stderr and "unstaged.sh" not in blocked.stdout + blocked.stderr


def test_pre_commit_runs_only_the_staged_scan_on_the_pre_commit_hook() -> None:
    # Only the pre-commit hook type: the local commit-msg hook stays as it is.
    config = yaml.safe_load(PRE_COMMIT.read_text())
    assert config.get("default_install_hook_types", ["pre-commit"]) == ["pre-commit"]
    assert config["repos"] == [{
        "repo": "local",
        "hooks": [{
            "id": "gitleaks",
            "name": "gitleaks (staged changes)",
            "entry": "bash ci/gitleaks.sh --staged",
            "language": "system",
            "pass_filenames": False,
            "stages": ["pre-commit"],
        }],
    }]
