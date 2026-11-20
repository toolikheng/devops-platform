"""Tests for scripts/cost-check.sh, run against a fake `aws` CLI so they need no AWS account.

The fake answers each call from a fixture file named after it (for example `ec2.describe-volumes`,
or `ec2.describe-volumes@eu-west-1` for one region), returns an AccessDenied error when a `.fail`
fixture exists, prints nothing otherwise, and logs every call it receives.
"""

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "cost-check.sh"
HOME_REGION = "ap-southeast-1"
OTHER_REGION = "eu-west-1"

FAKE_AWS = r"""#!/usr/bin/env bash
echo "$*" >> "$FAKE_AWS_LOG"
name="$1.$2"
region=""
while [ $# -gt 0 ]; do
  if [ "$1" = "--region" ]; then region="$2"; fi
  shift
done
for candidate in "$name@$region" "$name"; do
  if [ -e "$FAKE_AWS_FIXTURES/$candidate.fail" ]; then
    echo "An error occurred (AccessDenied) when calling ${name#*.}" >&2
    exit 254
  fi
  if [ -e "$FAKE_AWS_FIXTURES/$candidate" ]; then
    cat "$FAKE_AWS_FIXTURES/$candidate"
    exit 0
  fi
done
"""


class FakeAws:
    def __init__(self, root: Path) -> None:
        self.bin = root / "bin"
        self.fixtures = root / "fixtures"
        self.log = root / "calls.log"
        self.bin.mkdir()
        self.fixtures.mkdir()
        self.log.touch()
        fake = self.bin / "aws"
        fake.write_text(FAKE_AWS)
        fake.chmod(0o755)
        self.answer("sts.get-caller-identity", "arn:aws:iam::111122223333:user/tester")
        # Text output of a list of strings is one tab-separated line.
        self.answer("ec2.describe-regions", f"us-east-1\t{OTHER_REGION}\t{HOME_REGION}")

    def _path(self, call: str, region: str | None, suffix: str = "") -> Path:
        return self.fixtures / ((f"{call}@{region}" if region else call) + suffix)

    def answer(self, call: str, output: str, region: str | None = None) -> None:
        self._path(call, region).write_text(output + "\n")

    def fail(self, call: str, region: str | None = None) -> None:
        self._path(call, region, ".fail").touch()

    def run(self) -> subprocess.CompletedProcess[str]:
        env = {k: v for k, v in os.environ.items() if not k.startswith("AWS_")}
        env.update(
            PATH=f"{self.bin}{os.pathsep}{env['PATH']}",
            FAKE_AWS_FIXTURES=str(self.fixtures),
            FAKE_AWS_LOG=str(self.log),
            AWS_REGION=HOME_REGION,
        )
        return subprocess.run(
            ["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60
        )

    def calls(self) -> list[str]:
        return self.log.read_text().splitlines()


@pytest.fixture
def aws(tmp_path: Path) -> FakeAws:
    return FakeAws(tmp_path)


def test_clean_account_reports_no_leftovers(aws: FakeAws) -> None:
    result = aws.run()
    assert result.returncode == 0, result.stdout + result.stderr
    assert "No billable leftovers" in result.stdout


BILLABLE = [
    ("ec2.describe-instances", "i-0abc123\tt3.micro\trunning"),
    ("ec2.describe-volumes", "vol-0abc123\t20\tavailable"),
    ("ec2.describe-snapshots", "snap-0abc123\t20"),
    ("ec2.describe-addresses", "203.0.113.10\teipalloc-0abc123"),
    ("ec2.describe-network-interfaces", "eni-0abc123\t203.0.113.11"),
    ("ec2.describe-nat-gateways", "nat-0abc123\tavailable"),
    ("ec2.describe-vpc-endpoints", "vpce-0abc123\tcom.amazonaws.ap-southeast-1.ssm"),
    ("elbv2.describe-load-balancers", "lb-alb-test\tapplication"),
    ("elb.describe-load-balancers", "lb-classic-test"),
    ("eks.list-clusters", "eks-cluster-test"),
    ("rds.describe-db-instances", "rds-instance-test\tdb.t3.micro"),
    ("rds.describe-db-clusters", "rds-cluster-test"),
    ("secretsmanager.list-secrets", "secret-test"),
    ("ec2.describe-spot-instance-requests", "sir-0abc123\topen\tpersistent"),
]


@pytest.mark.parametrize(("call", "output"), BILLABLE, ids=[c for c, _ in BILLABLE])
def test_leftover_in_home_region_is_reported(aws: FakeAws, call: str, output: str) -> None:
    aws.answer(call, output, region=HOME_REGION)
    result = aws.run()
    assert result.returncode == 1, result.stdout + result.stderr
    assert output.split("\t")[0] in result.stdout
    assert "No billable leftovers" not in result.stdout


@pytest.mark.parametrize(
    "call", ["ec2.describe-instances", "ec2.describe-volumes", "ec2.describe-addresses"]
)
def test_other_enabled_regions_are_checked(aws: FakeAws, call: str) -> None:
    aws.answer(call, "leftover-in-other-region", region=OTHER_REGION)
    result = aws.run()
    assert result.returncode == 1, result.stdout + result.stderr
    assert "leftover-in-other-region" in result.stdout
    assert OTHER_REGION in result.stdout


def test_each_enabled_region_is_checked_exactly_once(aws: FakeAws) -> None:
    aws.run()
    regions = []
    for call in aws.calls():
        args = call.split()
        if args[:2] == ["ec2", "describe-instances"]:
            regions.append(args[args.index("--region") + 1])
    assert sorted(regions) == sorted(["us-east-1", OTHER_REGION, HOME_REGION])


def test_home_region_is_still_checked_when_region_listing_fails(aws: FakeAws) -> None:
    aws.fail("ec2.describe-regions")
    aws.answer("ec2.describe-instances", "i-0home123\tt3.micro\trunning", region=HOME_REGION)
    result = aws.run()
    assert result.returncode == 1, result.stdout + result.stderr
    assert "i-0home123" in result.stdout


def test_only_read_only_calls_and_never_cost_explorer(aws: FakeAws) -> None:
    aws.run()
    calls = aws.calls()
    assert calls, "the script made no AWS calls"
    for call in calls:
        service, operation = call.split()[:2]
        assert service != "ce", f"Cost Explorer API call (billed per request): {call}"
        assert operation.startswith(("describe-", "list-", "get-")), f"not read-only: {call}"


def test_every_call_names_its_region(aws: FakeAws) -> None:
    aws.run()
    for call in aws.calls():
        assert "--region" in call.split(), f"call without an explicit region: {call}"


def test_failed_check_is_an_error_not_a_clean_result(aws: FakeAws) -> None:
    aws.fail("ec2.describe-volumes", region=HOME_REGION)
    result = aws.run()
    assert result.returncode == 2, result.stdout + result.stderr
    assert "No billable leftovers" not in result.stdout
    assert "EBS volumes" in result.stdout + result.stderr


def test_region_listing_failure_is_an_error(aws: FakeAws) -> None:
    aws.fail("ec2.describe-regions")
    result = aws.run()
    assert result.returncode == 2, result.stdout + result.stderr
    assert "No billable leftovers" not in result.stdout


def test_missing_credentials_stop_the_scan(aws: FakeAws) -> None:
    aws.fail("sts.get-caller-identity")
    result = aws.run()
    assert result.returncode == 2, result.stdout + result.stderr
    assert "No billable leftovers" not in result.stdout


def test_root_identity_triggers_a_warning(aws: FakeAws) -> None:
    aws.answer("sts.get-caller-identity", "arn:aws:iam::111122223333:root")
    result = aws.run()
    assert "root" in (result.stdout + result.stderr).lower()
    assert "WARNING" in result.stdout + result.stderr


def test_none_output_counts_as_empty(aws: FakeAws) -> None:
    aws.answer("eks.list-clusters", "None", region=HOME_REGION)
    result = aws.run()
    assert result.returncode == 0, result.stdout + result.stderr


def test_only_open_or_active_spot_requests_count(aws: FakeAws) -> None:
    # A one-time request is closed or cancelled once its instance is gone, and costs nothing. An open or
    # active one (persistent, or still waiting for capacity) can start an instance again on its own.
    aws.run()
    spot = [c for c in aws.calls() if c.startswith("ec2 describe-spot-instance-requests")]
    assert len(spot) == 1, "Spot requests are checked once, in the home region"
    assert "Name=state,Values=open,active" in spot[0]
    assert f"--region {HOME_REGION}" in spot[0]
