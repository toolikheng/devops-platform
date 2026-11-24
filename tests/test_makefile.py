"""Every Make target is declared .PHONY and listed by `make help`.

The Makefile keeps three hand-written lists: the .PHONY block, the help text and the targets themselves. A
target missing from .PHONY stops working if a file of that name ever appears; one missing from the help is
invisible to anyone who runs `make help`. `make help` only prints text, so it runs for real here.
"""

import re
import subprocess

from conftest import REPO_ROOT

MAKEFILE = REPO_ROOT / "Makefile"


def targets() -> set[str]:
    """Named targets (`name:` at the start of a line), not variables (`NAME :=`) or file targets."""
    return set(re.findall(r"^([a-z][a-z0-9-]*):(?!=)", MAKEFILE.read_text(), re.M))


def phony() -> set[str]:
    block = re.search(r"^\.PHONY:(.*?)(?<!\\)\n", MAKEFILE.read_text(), re.M | re.S)
    assert block, "the Makefile must have a .PHONY block"
    return set(block.group(1).replace("\\", " ").split())


def help_entries() -> set[str]:
    result = subprocess.run(["make", "--no-print-directory", "help"], cwd=REPO_ROOT, capture_output=True,
                            text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    plain = re.sub(r"\x1b\[[0-9;]*m", "", result.stdout)
    return set(re.findall(r"^\s*make ([a-z][a-z0-9-]*)", plain, re.M))


def test_the_makefile_has_targets() -> None:
    assert {"help", "test-infra", "cloud-up", "monitoring-up"} <= targets()


def test_every_target_is_phony() -> None:
    assert targets() - phony() == set()


def test_every_target_is_in_make_help() -> None:
    assert targets() - {"help"} - help_entries() == set()


def test_help_lists_only_real_targets() -> None:
    assert help_entries() - targets() == set()
