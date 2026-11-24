"""Tests for scripts/restore-test.sh: a backup survives a rebuild of the AWS node (Phase C7).

    restore-test.sh before [STATE]   on the running node: ask a unique question, upload a unique policy, back up now,
                                     and record the run, its two snapshots, the node and the upload's sha256 in STATE
    (make cloud-down, cloud-rebuild, cloud-addons, cloud-deploy: a new node with empty data)
    restore-test.sh after [STATE]    on the new node: the sentinels are absent; restore the recorded run; the restore
                                     reports the recorded snapshots; the question and the policy are back; the file's
                                     bytes are the ones uploaded

Node commands go through SSM Run Command (AWS-RunShellScript, as root); the API through kubectl port-forward. The
admin token reaches curl on stdin only. They run with the fake CLIs of tests/fakecli.py.
"""

import hashlib
import json
import re
from pathlib import Path

import pytest

from fakecli import INSTANCE_ID, Fakes, output

TOKEN = "admin-token-value-for-tests"
RUN = "run-20261125T030000Z"
DB, UP = "d" * 64, "e" * 64
UPLOADS = "/var/lib/rancher/k3s/storage/pvc-new_policypal_policypal-uploads"


def invocation(stdout: str = "", status: str = "Success", rc: int = 0) -> tuple[int, str]:
    return 0, json.dumps({"Status": status, "ResponseCode": rc, "StandardOutputContent": stdout,
                          "StandardErrorContent": ""})


def restore_test(fakes: Fakes, phase: str, state: Path) -> object:
    return fakes.script("restore-test.sh", phase, str(state), env={"POLL_SECONDS": "0"})


def node_commands(fakes: Fakes) -> list[str]:
    commands = []
    for c in fakes.calls("aws"):
        if c["args"][:2] == ["ssm", "send-command"]:
            assert c["args"][c["args"].index("--instance-ids") + 1] == fakes_instance(fakes)
            assert c["args"][c["args"].index("--document-name") + 1] == "AWS-RunShellScript"
            commands += json.loads(c["args"][c["args"].index("--parameters") + 1])["commands"]
    return commands


def fakes_instance(fakes: Fakes) -> str:
    return (fakes.dir / "tf-output-instance_id").read_text()


@pytest.fixture
def api(fakes: Fakes) -> Fakes:
    fakes.parameter("/policypal/aws/admin-token", TOKEN)
    fakes.aws("ssm.send-command", "c-0123")
    return fakes


# --- before ------------------------------------------------------------------------------------------------

@pytest.fixture
def before(api: Fakes) -> Fakes:
    api.http("/ask", '{"refused": true, "mode": "refused"}')
    api.http("/documents", '{"status": "created", "document": {"slug": "x"}}', when="file=@")
    api.aws_sequence("ssm.get-command-invocation", [invocation("policypal-backup: done"),
                                                    invocation(f"{RUN} {DB} {UP}\n")])
    return api


def test_before_records_the_sentinels_the_run_and_the_node(before: Fakes, tmp_path: Path) -> None:
    state = tmp_path / "state" / "restore-test.env"
    result = restore_test(before, "before", state)
    assert result.returncode == 0, output(result)
    recorded = dict(re.findall(r"^([A-Z_0-9]+)=(.*)$", state.read_text(), re.M))
    assert recorded["RUN"] == RUN and recorded["DB_SNAPSHOT"] == DB and recorded["UPLOADS_SNAPSHOT"] == UP
    assert recorded["INSTANCE"] == INSTANCE_ID
    assert re.fullmatch(r"restore-test-\d{8}t\d{6}z", recorded["SLUG"]), "a lower-case slug, as PolicyPal stores it"
    assert recorded["QUESTION"].startswith(recorded["SLUG"]) and recorded["QUESTION"] == recorded["QUESTION"].lower()
    [upload] = [c for c in before.calls("curl") if c["upload"]]
    assert f"filename={recorded['SLUG']}.md" in " ".join(upload["args"])
    assert recorded["SHA256"] == hashlib.sha256(upload["upload"].encode()).hexdigest()
    assert oct(state.stat().st_mode & 0o777) == "0o600"


def test_before_backs_up_after_writing_the_sentinels_then_reads_the_run(before: Fakes, tmp_path: Path) -> None:
    assert restore_test(before, "before", tmp_path / "s.env").returncode == 0
    assert node_commands(before) == ["systemctl start policypal-backup", "/usr/local/sbin/policypal-backup show latest"]
    calls = before.calls()
    first_node = next(i for i, c in enumerate(calls) if c["args"][:2] == ["ssm", "send-command"])
    sentinels = [i for i, c in enumerate(calls) if c["tool"] == "curl" and ("/ask" in " ".join(c["args"]) or c["upload"])]
    assert len(sentinels) == 2 and max(sentinels) < first_node


def test_the_admin_token_reaches_curl_on_stdin_only(before: Fakes, tmp_path: Path) -> None:
    result = restore_test(before, "before", tmp_path / "s.env")
    assert result.returncode == 0, output(result)
    assert not [c for c in before.calls() if any(TOKEN in a for a in c["args"])]
    assert [c for c in before.calls("curl") if TOKEN in c["stdin"]], "the upload sends it as a header from stdin"
    assert TOKEN not in output(result) and TOKEN not in (tmp_path / "s.env").read_text()


def test_before_stops_when_the_backup_fails(before: Fakes, tmp_path: Path) -> None:
    before.aws_sequence("ssm.get-command-invocation", [invocation("", status="Failed", rc=1)])
    state = tmp_path / "s.env"
    result = restore_test(before, "before", state)
    assert result.returncode != 0 and "systemctl start policypal-backup" in output(result)
    assert not state.exists()


# --- after -------------------------------------------------------------------------------------------------

SLUG = "restore-test-20261125t030000z"
QUESTION = f"{SLUG}: is there a policy on travelling to the moon?"
SHA = hashlib.sha256(b"the sentinel policy").hexdigest()


def documents(*slugs: str) -> str:
    return json.dumps([{"slug": s} for s in ("leave-policy", *slugs)])


def analytics(*questions: str) -> str:
    return json.dumps({"top_questions": [{"question": q, "count": 1} for q in questions],
                       "unanswered_questions": [{"question": q, "count": 1} for q in questions]})


@pytest.fixture
def after(api: Fakes, tmp_path: Path) -> tuple[Fakes, Path]:
    state = tmp_path / "s.env"
    state.write_text(f"RUN={RUN}\nDB_SNAPSHOT={DB}\nUPLOADS_SNAPSHOT={UP}\nINSTANCE=i-0aaaaaaaaaaaaaaaa\n"
                     f"SLUG={SLUG}\nQUESTION={QUESTION}\nSHA256={SHA}\n")
    api.http_sequence("/documents", [documents(), documents(SLUG)])
    api.http_sequence("/analytics?days=1", [analytics("how many days of leave?"), analytics(QUESTION)])
    api.aws_sequence("ssm.get-command-invocation", [
        invocation(f"policypal-backup: restored {RUN}: database {DB}, uploads {UP}\n"),
        invocation(f"{SHA}  {UPLOADS}/{SLUG}.md\n")])
    api.pvc_volume("pvc-new")
    api.pv_path(UPLOADS)
    return api, state


def test_after_restores_the_run_on_a_new_node_and_finds_every_sentinel(after: tuple[Fakes, Path]) -> None:
    fakes, state = after
    result = restore_test(fakes, "after", state)
    assert result.returncode == 0, output(result)
    assert node_commands(fakes) == [f"/usr/local/sbin/policypal-backup restore {RUN}", f"sha256sum {UPLOADS}/{SLUG}.md"]
    assert "PASS" in result.stdout


def test_after_refuses_the_node_the_backup_was_made_on(after: tuple[Fakes, Path]) -> None:
    fakes, state = after
    state.write_text(state.read_text().replace("INSTANCE=i-0aaaaaaaaaaaaaaaa", f"INSTANCE={INSTANCE_ID}"))
    result = restore_test(fakes, "after", state)
    assert result.returncode == 1 and "same node" in output(result)
    assert not node_commands(fakes)


def test_after_refuses_when_the_sentinel_is_already_there(after: tuple[Fakes, Path]) -> None:
    fakes, state = after
    fakes.http_sequence("/documents", [documents(SLUG)])
    result = restore_test(fakes, "after", state)
    assert result.returncode == 1 and SLUG in output(result)
    assert not node_commands(fakes), "nothing to prove: the data was never lost"


@pytest.mark.parametrize("restored, sha, docs, questions, reason", [
    (f"restored {RUN}: database {'f' * 64}, uploads {UP}", SHA, documents(SLUG), analytics(QUESTION), "snapshot"),
    (f"restored {RUN}: database {DB}, uploads {UP}", "0" * 64, documents(SLUG), analytics(QUESTION), "sha256"),
    (f"restored {RUN}: database {DB}, uploads {UP}", SHA, documents(), analytics(QUESTION), "policy"),
    (f"restored {RUN}: database {DB}, uploads {UP}", SHA, documents(SLUG), analytics(), "question"),
])
def test_after_fails_when_anything_did_not_come_back(after: tuple[Fakes, Path], restored: str, sha: str, docs: str,
                                                     questions: str, reason: str) -> None:
    fakes, state = after
    fakes.http_sequence("/documents", [documents(), docs])
    fakes.http_sequence("/analytics?days=1", [analytics(), questions])
    fakes.aws_sequence("ssm.get-command-invocation", [invocation(restored + "\n"),
                                                       invocation(f"{sha}  {UPLOADS}/{SLUG}.md\n")])
    result = restore_test(fakes, "after", state)
    assert result.returncode == 1 and reason in output(result), output(result)
    assert "PASS" not in result.stdout


def test_after_stops_when_the_restore_fails(after: tuple[Fakes, Path]) -> None:
    fakes, state = after
    fakes.aws_sequence("ssm.get-command-invocation", [invocation("", status="Failed", rc=1)])
    result = restore_test(fakes, "after", state)
    assert result.returncode == 1 and "restore" in output(result)


@pytest.mark.parametrize("path", ["restore-test.env", "scripts/restore-test.env", "{repo}/restore-test.env",
                                  "/tmp/../{repo}/planning/restore-test.env"])
def test_the_record_is_never_written_inside_the_repository(fakes: Fakes, path: str) -> None:
    # It holds snapshot and instance IDs: kept out of the working tree, it can't be committed by accident.
    repo = str(Path(__file__).resolve().parent.parent).lstrip("/")
    target = path.format(repo=repo if path.startswith("/tmp") else "/" + repo)
    result = fakes.script("restore-test.sh", "before", target)
    assert result.returncode == 2 and "outside the repository" in result.stderr, output(result)
    assert not fakes.calls()
    assert not (Path(__file__).resolve().parent.parent / "restore-test.env").exists()


@pytest.mark.parametrize("args", [[], ["during"], ["before", "a", "b"]])
def test_usage(fakes: Fakes, args: list[str]) -> None:
    result = fakes.script("restore-test.sh", *args)
    assert result.returncode == 2 and "usage:" in result.stderr
    assert not fakes.calls()


def test_make_targets_run_both_phases_against_the_aws_node(fakes: Fakes) -> None:
    makefile = (Path(__file__).resolve().parent.parent / "Makefile").read_text()
    for phase in ("before", "after"):
        recipe = re.search(rf"^cloud-restore-test-{phase}:\n\t(.+)$", makefile, re.M)
        assert recipe and f"restore-test.sh\" {phase}" in recipe.group(1), phase
        assert 'KUBECONFIG="$(AWS_KUBECONFIG)"' in recipe.group(1) and 'AWS_PROFILE="$(AWS_PROFILE)"' in recipe.group(1)
