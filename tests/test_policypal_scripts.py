"""Tests for the PolicyPal deploy scripts and their Make targets.

- scripts/policypal-secrets.sh: creates the two Secrets on k3d once, with random values, never printed (on AWS,
  External Secrets Operator makes them from SSM Parameter Store).
- scripts/smoke-test.sh: /health, one answered and cited question, one refusal (the API through a kubectl
  port-forward to its Service), the UI through the ingress, and the API not reachable through it.
- scripts/deploy-policypal.sh: render, apply, wait for the Secrets and the rollout, smoke test, and roll back
  what it changed on failure.
- scripts/install-eso.sh and render-eso.sh: External Secrets Operator's pinned chart, installed and rendered.
- scripts/check-state-secrets.sh: counts each live secret value in the persistent state, never shows it.
- make k8s-check, policypal-secrets-local, policypal-local, cloud-deploy, cloud-smoke, cloud-addons,
  check-state-secrets.

They run with fake `kubectl`, `curl`, `docker`, `k3d`, `terraform`, `aws` and `helm` first on PATH, so no
cluster, container or AWS account is touched. Each fake appends one JSON line per call (tool, args,
KUBECONFIG, AWS_PROFILE, working directory; for kubectl also the namespace, taken out of the args) to a log,
and answers from files the test writes (secrets that exist, revisions, API replies, state, parameters).
"""

import json
import os
import re
import secrets
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from conftest import chart_env, secret_refs

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
AWS_GROUP_VARS = REPO_ROOT / "ansible" / "inventory" / "group_vars" / "aws.yml"
ESO_DIR = REPO_ROOT / "k8s" / "platform" / "external-secrets"

G04 = "How many days of compassionate leave do I get when my grandmother passes away?"
G41 = "How many months of bonus will we get at the end of the year?"
HEALTH_OK = {"status": "ok", "database": "ok", "llm": {"provider": "extractive", "model": None, "status": "ok"},
             "documents": 8, "chunks": 119, "files_not_loaded": [], "version": "0.1.0"}
ANSWER_G04 = {"mode": "extractive", "refused": False, "answer": "**[1] Leave Policy - 6.3** ...",
              "citations": [{"number": 1, "document_id": 3, "document_title": "Leave Policy",
                             "section": "6.3 Compassionate Leave", "text": "...", "similarity": 0.81}]}
ANSWER_G41 = {"mode": "refused", "refused": True, "answer": "I couldn't find this in the policies.",
              "citations": []}
UI_URL = "http://203.0.113.20"
MANIFEST = "apiVersion: v1\nkind: Namespace\nmetadata:\n  name: policypal\n"
ESO_MANIFEST = "apiVersion: v1\nkind: Namespace\nmetadata:\n  name: external-secrets\n"
FORWARD_PORT = "41234"  # the local port the fake `kubectl port-forward` says it picked
API = f"http://127.0.0.1:{FORWARD_PORT}"

FAKE = r'''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path

tool, args = Path(sys.argv[0]).name, sys.argv[1:]
d = Path(os.environ["FAKE_DIR"])
# kubectl: the namespace is logged on its own; args keep everything else, in order.
ns = ""
if tool == "kubectl":
    rest, it = [], iter(args)
    for x in it:
        if x in ("-n", "--namespace"):
            ns = next(it)
        elif x.startswith("--namespace="):
            ns = x.split("=", 1)[1]
        else:
            rest.append(x)
    args = rest
stdin = sys.stdin.read() if "-" in args or any(a.endswith("/dev/stdin") for a in args) or tool == "docker" and "-i" in args else ""
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(json.dumps({"tool": tool, "args": args, "ns": ns, "kubeconfig": os.environ.get("KUBECONFIG", ""),
                          "profile": os.environ.get("AWS_PROFILE", ""), "cwd": os.getcwd(), "stdin": stdin}) + "\n")

def answer(name, default_rc=0):
    f = d / name
    if f.exists():
        sys.stdout.write(f.read_text())
        rc = d / (name + ".rc")
        # Like curl: an HTTP error status is an error exit only with -f (--fail).
        failing = tool != "curl" or any(a.startswith("-f") for a in args)
        sys.exit(int(rc.read_text()) if rc.exists() and failing else 0)
    sys.exit(default_rc)

if tool == "kubectl":
    a = args
    if a[:2] == ["get", "namespace"]:
        sys.exit(0 if (d / "namespace-exists").exists() else 1)
    if a[:2] == ["get", "secret"]:
        sys.exit(0 if all((d / f"secret-{n}").exists() for n in a[2:] if not n.startswith("-")) else 1)
    if a[:3] == ["create", "secret", "generic"]:
        (d / f"secret-{a[3]}").write_text(stdin)
        sys.exit(0)
    if a[:2] == ["wait", "--for=create"]:
        # Like kubectl 1.37: --for=create waits for one resource; given several, it fails at once on the
        # first missing one (seen on k3d).
        names = [x.split("/", 1)[1] for x in a[2:] if x.startswith("secret/")]
        missing = [n for n in names if not (d / f"secret-{n}").exists()]
        if missing:
            print(f'Error from server (NotFound): secrets "{missing[0]}" not found' if len(names) > 1
                  else "error: timed out waiting for the condition", file=sys.stderr)
        sys.exit(1 if missing or not names else 0)
    if a[:1] == ["apply"]:
        (d / "applied").write_text(stdin)
        sys.exit(1 if (d / "fail-apply").exists() else 0)
    if a[:2] == ["rollout", "status"]:
        sys.exit(1 if (d / f"fail-rollout-{a[2].split('/')[-1]}").exists() else 0)
    if a[:2] == ["rollout", "undo"]:
        sys.exit(1 if (d / f"fail-undo-{a[2].split('/')[-1]}").exists() else 0)
    if a[:2] == ["get", "deployment"] and any("revision" in x for x in a):
        when = "after" if (d / "applied").exists() else "before"
        answer(f"rev-{a[2]}-{when}", 1)
    if a[:1] == ["port-forward"]:
        if (d / "fail-port-forward").exists():
            print('error: services "policypal-api" not found', file=sys.stderr)
            sys.exit(1)
        port = (d / "forward-port").read_text()
        print(f"Forwarding from 127.0.0.1:{port} -> 8000\nForwarding from [::1]:{port} -> 8000")
    sys.exit(0)
if tool == "curl":
    url = [x for x in args if x.startswith("http")][0]
    if url.endswith("/ask"):
        sent = " ".join(args)
        answer("ask-g04" if "compassionate" in sent else "ask-g41" if "bonus" in sent else "none", 22)
    answer("curl" + url.split("//", 1)[1].split("/", 1)[-1].replace("/", "_") if url.count("/") > 2 else "curl_root", 7)
if tool == "docker":
    if any("kustomize" in x for x in args):
        answer("manifest")
    if any(x.startswith("alpine/helm:") for x in args):
        answer("eso-manifest")
    sys.exit(0)
if tool == "terraform" and args[:2] == ["output", "-raw"]:
    answer(f"tf-{args[2]}", 1)
if tool == "terraform" and args[:2] == ["state", "pull"]:
    answer("tf-state", 1)
if tool == "aws" and args[:2] == ["ssm", "get-parameter"]:
    answer("ssm" + args[args.index("--name") + 1].replace("/", "_"), 254)
if tool == "helm":
    sys.exit(1 if (d / "fail-helm").exists() else 0)
if tool == "k3d" and args[:2] == ["cluster", "get"]:
    sys.exit(0)
sys.exit(0)
'''


class Fakes:
    def __init__(self, root: Path) -> None:
        self.dir = root
        self.bin = root / "bin"
        self.log = root / "calls.log"
        self.home = root / "home"
        self.bin.mkdir()
        self.log.touch()
        # ephemeral-tf.sh (behind the node's IP lookup) needs a public key even for `terraform output`.
        (self.home / ".ssh").mkdir(parents=True)
        (self.home / ".ssh" / "id_ed25519.pub").write_text("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExample test\n")
        for tool in ("kubectl", "curl", "docker", "k3d", "terraform", "aws", "helm"):
            path = self.bin / tool
            path.write_text(FAKE)
            path.chmod(0o755)
        self.write("manifest", MANIFEST)
        self.write("eso-manifest", ESO_MANIFEST)
        self.write("forward-port", FORWARD_PORT)
        self.write("tf-public_ip", "203.0.113.20")
        self.api(health=HEALTH_OK, g04=ANSWER_G04, g41=ANSWER_G41)
        self.ui(health="ok", docs="<html>Streamlit</html>", openapi="<html>Streamlit</html>")

    # --- fixtures -------------------------------------------------------------------------------------
    def write(self, name: str, text: str, rc: int | None = None) -> None:
        (self.dir / name).write_text(text)
        if rc is not None:
            (self.dir / f"{name}.rc").write_text(str(rc))

    def api(self, health: dict | None = None, g04: dict | None = None, g41: dict | None = None,
            health_rc: int | None = None) -> None:
        if health is not None:
            self.write("curlhealth", json.dumps(health), health_rc)
        if g04 is not None:
            self.write("ask-g04", json.dumps(g04))
        if g41 is not None:
            self.write("ask-g41", json.dumps(g41))

    def ui(self, health: str | None = None, docs: str | None = None, openapi: str | None = None) -> None:
        for name, text in (("_stcore_health", health), ("docs", docs), ("openapi.json", openapi)):
            if text is not None:
                self.write(f"curl{name}", text)

    def secret_exists(self, name: str) -> None:
        self.write(f"secret-{name}", "existing")

    def eso_synced(self) -> None:
        """What External Secrets Operator does on AWS: both Secrets exist once the ExternalSecrets are applied."""
        self.secret_exists("policypal-db")
        self.secret_exists("policypal-admin")

    def parameter(self, name: str, value: str) -> None:
        self.write("ssm" + name.replace("/", "_"), value)

    def log_grep(self) -> None:
        """Put a grep first on PATH that records its arguments, then runs the real grep."""
        real = shutil.which("grep")
        path = self.bin / "grep"
        path.write_text(f'#!/bin/bash\nprintf "%s\\n" "$@" >> "$FAKE_DIR/grep-args"\nexec {real} "$@"\n')
        path.chmod(0o755)

    def revisions(self, name: str, before: str | None, after: str | None) -> None:
        if before is not None:
            self.write(f"rev-{name}-before", before)
        if after is not None:
            self.write(f"rev-{name}-after", after)

    def flag(self, name: str) -> None:
        (self.dir / name).touch()

    # --- inspection -----------------------------------------------------------------------------------
    def calls(self, tool: str | None = None) -> list[dict[str, Any]]:
        rows = [json.loads(line) for line in self.log.read_text().splitlines()]
        return [r for r in rows if tool is None or r["tool"] == tool]

    def kubectl(self, *prefix: str) -> list[list[str]]:
        """The args of kubectl calls that start with these words (the namespace is in each call's "ns")."""
        return [c["args"] for c in self.calls("kubectl") if c["args"][:len(prefix)] == list(prefix)]

    def created_secret(self, name: str) -> dict[str, str]:
        text = (self.dir / f"secret-{name}").read_text()
        return dict(line.split("=", 1) for line in text.splitlines() if line)

    def created_secrets(self) -> set[tuple[str, str]]:
        """(Secret, key) for every Secret the scripts created."""
        return {(p.name.removeprefix("secret-"), key)
                for p in self.dir.glob("secret-*") for key in self.created_secret(p.name.removeprefix("secret-"))}

    # --- running --------------------------------------------------------------------------------------
    def env(self) -> dict[str, str]:
        return {"PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}", "HOME": str(self.home),
                "FAKE_LOG": str(self.log), "FAKE_DIR": str(self.dir)}

    def script(self, name: str, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["bash", str(SCRIPTS / name), *args], cwd=REPO_ROOT, env=self.env(),
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120)

    def make(self, target: str, *overrides: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["make", "--no-print-directory", target, *overrides], cwd=REPO_ROOT,
                              env=self.env(), stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              timeout=120)


@pytest.fixture
def fakes(tmp_path: Path) -> Fakes:
    return Fakes(tmp_path)


def output(result: subprocess.CompletedProcess[str]) -> str:
    return result.stdout + result.stderr


# --- scripts/policypal-secrets.sh -----------------------------------------------------------------------

def test_secrets_are_created_with_the_keys_the_manifests_reference(fakes: Fakes, rendered: dict) -> None:
    # The manifests are the source of truth: a key renamed there but not here would only show up as a pod
    # that can't start, after the rollout times out.
    result = fakes.script("policypal-secrets.sh")
    assert result.returncode == 0, output(result)
    for overlay, docs in rendered.items():
        assert fakes.created_secrets() == secret_refs(docs), overlay
    assert fakes.kubectl("create", "namespace"), "the namespace must exist before its Secrets"


def test_a_new_namespace_is_ready_for_kubectl_apply(fakes: Fakes) -> None:
    # Without --save-config the deploy's kubectl apply warns that the namespace has no last-applied
    # configuration (seen on both AWS runs), then patches it in.
    fakes.script("policypal-secrets.sh")
    creates = fakes.kubectl("create", "namespace")
    assert creates and creates[0][:3] == ["create", "namespace", "policypal"] and "--save-config" in creates[0]


def test_an_existing_namespace_is_left_to_the_manifests(fakes: Fakes) -> None:
    # Re-applying a bare Namespace would strip the labels the deploy gives it, and the next deploy would put
    # them back: "configured" on every run although nothing really changed.
    fakes.flag("namespace-exists")
    result = fakes.script("policypal-secrets.sh")
    assert result.returncode == 0, output(result)
    assert fakes.created_secret("policypal-db"), "the script must still run"
    assert not fakes.kubectl("create", "namespace") and not fakes.kubectl("apply")


def test_values_are_long_random_hex(fakes: Fakes) -> None:
    # Hex only: the API puts the password inside its database URL (api.yaml), where it needs no escaping.
    fakes.script("policypal-secrets.sh")
    assert re.fullmatch(r"[0-9a-f]{48}", fakes.created_secret("policypal-db")["POSTGRES_PASSWORD"])
    assert re.fullmatch(r"[0-9a-f]{48}", fakes.created_secret("policypal-admin")["ADMIN_TOKEN"])


def test_values_differ_between_clusters(fakes: Fakes, tmp_path_factory: pytest.TempPathFactory) -> None:
    other = Fakes(tmp_path_factory.mktemp("other"))
    fakes.script("policypal-secrets.sh")
    other.script("policypal-secrets.sh")
    assert fakes.created_secret("policypal-db") != other.created_secret("policypal-db")
    assert fakes.created_secret("policypal-admin") != other.created_secret("policypal-admin")


def test_values_never_appear_in_arguments_or_output(fakes: Fakes) -> None:
    # Command-line arguments are visible to every local user in `ps`; values go through stdin only.
    result = fakes.script("policypal-secrets.sh")
    values = [*fakes.created_secret("policypal-db").values(), *fakes.created_secret("policypal-admin").values()]
    every_arg = " ".join(" ".join(c["args"]) for c in fakes.calls())
    for value in values:
        assert value not in every_arg
        assert value not in output(result)


def test_existing_secrets_are_never_replaced(fakes: Fakes) -> None:
    # Postgres keeps the password it was initialised with; a new one would lock the API out.
    fakes.secret_exists("policypal-db")
    fakes.secret_exists("policypal-admin")
    result = fakes.script("policypal-secrets.sh")
    assert result.returncode == 0, output(result)
    assert not fakes.kubectl("create", "secret")
    assert not fakes.kubectl("delete")


# --- scripts/smoke-test.sh ------------------------------------------------------------------------------

def test_smoke_test_passes_on_a_healthy_deployment(fakes: Fakes) -> None:
    result = fakes.script("smoke-test.sh", UI_URL)
    assert result.returncode == 0, output(result)


def test_smoke_test_calls_the_api_through_a_port_forward_to_its_service(fakes: Fakes) -> None:
    # The API has no ingress and no open port: kubectl forwards a local port to its ClusterIP Service.
    fakes.script("smoke-test.sh", UI_URL)
    assert fakes.kubectl("port-forward", "svc/policypal-api", ":8000"), "kubectl picks a free local port"
    curls = [c["args"] for c in fakes.calls("curl")]
    assert any(f"{API}/health" in a for a in curls)
    asks = [a for a in curls if f"{API}/ask" in a]
    assert len(asks) == 2
    for a in asks:
        assert "Content-Type: application/json" in a, "FastAPI reads the body as JSON only with this header"
    bodies = [json.loads(a[a.index("-d") + 1]) for a in asks]
    assert sorted(b["question"] for b in bodies) == sorted([G04, G41])


def test_smoke_test_uses_the_port_kubectl_picked(fakes: Fakes) -> None:
    fakes.write("forward-port", "45678")
    result = fakes.script("smoke-test.sh", UI_URL)
    assert result.returncode == 0, output(result)
    api_urls = {a for c in fakes.calls("curl") for a in c["args"] if a.startswith("http://127.0.0.1")}
    assert api_urls == {"http://127.0.0.1:45678/health", "http://127.0.0.1:45678/ask"}


def test_smoke_test_fails_when_the_port_forward_does_not_start(fakes: Fakes) -> None:
    # Then nothing must be asked on localhost: whatever else listens there (another forward, maybe to another
    # cluster) would answer, and the checks would pass for the wrong cluster.
    fakes.flag("fail-port-forward")
    result = fakes.script("smoke-test.sh", UI_URL)
    assert result.returncode != 0
    assert "port-forward" in output(result)
    assert not [c for c in fakes.calls("curl") if any(a.startswith("http://127.0.0.1") for a in c["args"])]


def test_smoke_test_checks_the_ui_through_the_ingress(fakes: Fakes) -> None:
    fakes.script("smoke-test.sh", UI_URL)
    urls = {a for c in fakes.calls("curl") for a in c["args"] if a.startswith("http")}
    assert {f"{UI_URL}/_stcore/health", f"{UI_URL}/docs", f"{UI_URL}/openapi.json"} <= urls


@pytest.mark.parametrize("change, why", [
    ({"health": {**HEALTH_OK, "status": "degraded"}}, "health not ok"),
    ({"health": {**HEALTH_OK, "llm": {"provider": "ollama", "model": "llama3.2:3b", "status": "ok"}}}, "LLM on"),
    ({"health": {**HEALTH_OK, "files_not_loaded": ["leave-policy.md"]}}, "a policy failed to load"),
    ({"health": {**HEALTH_OK, "documents": 0}}, "nothing ingested"),
    ({"health": {**HEALTH_OK, "status": "unavailable"}, "health_rc": 1}, "API returns 503"),
    ({"g04": ANSWER_G41}, "g04 refused"),
    ({"g04": {**ANSWER_G04, "citations": [{**ANSWER_G04["citations"][0], "document_title": "Travel Policy"}]}},
     "g04 cites the wrong policy"),
    ({"g04": {**ANSWER_G04, "citations": [{**ANSWER_G04["citations"][0], "section": "5.1 Annual Leave"}]}},
     "g04 cites the wrong section"),
    ({"g04": {**ANSWER_G04, "mode": "llm"}}, "g04 answered by an LLM"),
    ({"g41": ANSWER_G04}, "g41 answered"),
])
def test_smoke_test_fails_when_the_app_misbehaves(fakes: Fakes, change: dict, why: str) -> None:
    fakes.api(**change)
    result = fakes.script("smoke-test.sh", UI_URL)
    assert result.returncode != 0, why
    assert fakes.kubectl("port-forward"), "the check must actually run"


@pytest.mark.parametrize("ui, why", [
    ({"health": "not ok"}, "UI unhealthy"),
    ({"docs": '<html><div id="swagger-ui"></div></html>'}, "Swagger reachable from outside"),
    ({"openapi": '{"openapi": "3.1.0", "info": {"title": "PolicyPal"}}'}, "API schema reachable from outside"),
])
def test_smoke_test_fails_when_the_ingress_is_wrong(fakes: Fakes, ui: dict, why: str) -> None:
    fakes.ui(**ui)
    result = fakes.script("smoke-test.sh", UI_URL)
    assert result.returncode != 0, why
    assert "FAIL" in output(result), "the smoke test must report the failed check"


def test_smoke_test_fails_when_the_ui_is_unreachable(fakes: Fakes) -> None:
    (fakes.dir / "curl_stcore_health").unlink()  # the fake curl then exits 7: could not connect
    result = fakes.script("smoke-test.sh", UI_URL)
    assert result.returncode != 0
    assert "FAIL" in output(result) and fakes.calls("curl")


def test_smoke_test_needs_the_ui_url(fakes: Fakes) -> None:
    result = fakes.script("smoke-test.sh")
    assert result.returncode != 0
    assert "usage: smoke-test.sh" in output(result)
    assert not fakes.calls()


# --- scripts/deploy-policypal.sh ------------------------------------------------------------------------

def test_deploy_applies_the_rendered_overlay_then_waits_and_smoke_tests(fakes: Fakes, rendered: dict) -> None:
    fakes.eso_synced()
    result = fakes.script("deploy-policypal.sh", "aws", UI_URL)
    assert result.returncode == 0, output(result)

    # The Secrets are waited for after the apply (ExternalSecrets make them only then), before the rollout:
    # one call per Secret, because kubectl's --for=create only waits when it is given a single resource.
    calls = fakes.calls("kubectl")
    waits = [i for i, c in enumerate(calls) if c["args"][:2] == ["wait", "--for=create"]]
    waited = []
    for i in waits:
        names = [a.removeprefix("secret/") for a in calls[i]["args"] if a.startswith("secret/")]
        assert len(names) == 1, f"one Secret per kubectl wait: {calls[i]['args']}"
        assert calls[i]["ns"] == "policypal" and "--timeout=2m" in calls[i]["args"]
        waited += names
    assert sorted(waited) == sorted({name for name, _ in secret_refs(rendered["aws"])}), "exactly the Secrets the pods need"
    applied = next(i for i, c in enumerate(calls) if c["args"][:2] == ["apply", "-f"])
    rollout = next(i for i, c in enumerate(calls) if c["args"][:2] == ["rollout", "status"])
    assert applied < min(waits) and max(waits) < rollout

    renders = [c for c in fakes.calls("docker") if any("kustomize" in a for a in c["args"])]
    assert len(renders) == 1 and renders[0]["args"][-1].endswith("/policypal/overlays/aws")
    assert (fakes.dir / "applied").read_text() == MANIFEST, "exactly the rendered YAML is applied"
    waited = [a[2] for a in fakes.kubectl("rollout", "status")]
    assert waited == ["statefulset/postgres", "deployment/policypal-api", "deployment/policypal-ui"]
    assert fakes.kubectl("port-forward"), "smoke test after the rollout"
    assert not fakes.kubectl("rollout", "undo")


@pytest.mark.parametrize("overlay", ["", "prod", "../aws"])
def test_deploy_accepts_only_the_two_overlays(fakes: Fakes, overlay: str) -> None:
    result = fakes.script("deploy-policypal.sh", overlay, UI_URL)
    assert result.returncode != 0
    assert "usage: deploy-policypal.sh" in output(result)
    assert not fakes.kubectl("apply")


@pytest.mark.parametrize("overlay", ["local", "aws"])
def test_deploy_stops_soon_when_the_secrets_do_not_appear(fakes: Fakes, overlay: str) -> None:
    # On AWS the ExternalSecrets make the Secrets after the apply; if External Secrets Operator can't (no
    # store, no IAM access) or the k3d script wasn't run, the deploy stops after the 2-minute wait instead of
    # the 15-minute rollout timeout, says where to look, and rolls back what the apply changed.
    fakes.secret_exists("policypal-admin")
    fakes.revisions("policypal-api", before="3", after="4")
    fakes.revisions("policypal-ui", before="2", after="2")
    result = fakes.script("deploy-policypal.sh", overlay, UI_URL)
    assert result.returncode != 0
    assert fakes.kubectl("apply", "-f", "-"), "the apply comes first"
    assert fakes.kubectl("wait", "--for=create"), "then the wait"
    assert ["rollout", "status", "statefulset/postgres"] not in [a[:3] for a in fakes.kubectl("rollout", "status")]
    text = output(result)
    assert "externalsecret" in text and "policypal-secrets.sh" in text
    assert fakes.kubectl("rollout", "undo") == [["rollout", "undo", "deployment/policypal-api", "--to-revision=3"]]


def test_failed_rollout_rolls_back_what_this_deploy_changed(fakes: Fakes) -> None:
    fakes.eso_synced()
    fakes.revisions("policypal-api", before="3", after="4")
    fakes.revisions("policypal-ui", before="2", after="2")
    fakes.flag("fail-rollout-policypal-api")
    result = fakes.script("deploy-policypal.sh", "aws", UI_URL)
    assert result.returncode != 0
    undos = fakes.kubectl("rollout", "undo")
    assert undos == [["rollout", "undo", "deployment/policypal-api", "--to-revision=3"]]


def test_failed_smoke_test_rolls_back_too(fakes: Fakes) -> None:
    fakes.eso_synced()
    fakes.revisions("policypal-api", before="3", after="4")
    fakes.revisions("policypal-ui", before="2", after="3")
    fakes.api(g41=ANSWER_G04)
    result = fakes.script("deploy-policypal.sh", "aws", UI_URL)
    assert result.returncode != 0
    assert sorted(fakes.kubectl("rollout", "undo")) == [
        ["rollout", "undo", "deployment/policypal-api", "--to-revision=3"],
        ["rollout", "undo", "deployment/policypal-ui", "--to-revision=2"],
    ]


def test_a_failed_undo_does_not_stop_the_other_rollbacks(fakes: Fakes) -> None:
    # Each Deployment goes back on its own: the UI must not stay on its new revision because the API's undo
    # failed. The deploy still fails, and says which rollback needs a look.
    fakes.eso_synced()
    fakes.revisions("policypal-api", before="3", after="4")
    fakes.revisions("policypal-ui", before="2", after="3")
    fakes.api(g41=ANSWER_G04)
    fakes.flag("fail-undo-policypal-api")
    result = fakes.script("deploy-policypal.sh", "aws", UI_URL)
    assert result.returncode != 0
    assert sorted(fakes.kubectl("rollout", "undo")) == [
        ["rollout", "undo", "deployment/policypal-api", "--to-revision=3"],
        ["rollout", "undo", "deployment/policypal-ui", "--to-revision=2"],
    ]
    assert "rolling deployment/policypal-api back failed" in output(result)


def test_first_deploy_has_nothing_to_roll_back(fakes: Fakes) -> None:
    fakes.eso_synced()
    fakes.revisions("policypal-api", before=None, after="1")
    fakes.flag("fail-rollout-policypal-api")
    result = fakes.script("deploy-policypal.sh", "aws", UI_URL)
    assert result.returncode != 0
    assert ["rollout", "status", "deployment/policypal-api"] in [a[:3] for a in fakes.kubectl("rollout", "status")]
    assert not fakes.kubectl("rollout", "undo")


def test_deploy_stops_when_apply_fails(fakes: Fakes) -> None:
    fakes.eso_synced()
    fakes.flag("fail-apply")
    result = fakes.script("deploy-policypal.sh", "aws", UI_URL)
    assert result.returncode != 0
    assert fakes.kubectl("apply", "-f", "-"), "the apply must have been attempted"
    assert not fakes.kubectl("rollout", "status")


# --- make targets ---------------------------------------------------------------------------------------

def flag_value(args: list[str], flag: str) -> str:
    return args[args.index(flag) + 1]


def test_k8s_check_validates_policypal_and_eso_strictly_against_the_nodes_version(fakes: Fakes) -> None:
    result = fakes.make("k8s-check")
    assert result.returncode == 0, output(result)
    docker = fakes.calls("docker")
    renders = [c["args"][-1] for c in docker if any("kustomize" in a for a in c["args"])]
    assert [r.rsplit("/", 1)[-1] for r in renders] == ["local", "aws"]
    assert [c for c in docker if any(a.startswith("alpine/helm:") for a in c["args"])], "renders the ESO chart"
    validations = [c for c in docker if any("kubeconform" in a for a in c["args"])]
    assert len(validations) == 3
    catalog = re.compile(r"https://raw\.githubusercontent\.com/datreeio/CRDs-catalog/[0-9a-f]{40}/"
                         r"\{\{\.Group\}\}/\{\{\.ResourceKind\}\}_\{\{\.ResourceAPIVersion\}\}\.json")
    for call in validations:
        args = call["args"]
        assert "-strict" in args
        assert flag_value(args, "-kubernetes-version") == "1.36.6"
        # Kubernetes' own schemas, then the CRD schemas (ExternalSecret, ClusterSecretStore) at a pinned commit.
        locations = [args[i + 1] for i, a in enumerate(args) if a == "-schema-location"]
        assert locations[0] == "default" and len(locations) == 2 and catalog.fullmatch(locations[1])
    assert [v["stdin"] for v in validations[:2]] == [MANIFEST, MANIFEST], "kubeconform reads the rendered YAML"
    store = (ESO_DIR / "clustersecretstore.yaml").read_text()
    assert ESO_MANIFEST in validations[2]["stdin"] and store in validations[2]["stdin"], "chart and store"
    # No schema exists for the CustomResourceDefinition kind, so only the ESO run skips it, and nothing else.
    assert all("-skip" not in v["args"] for v in validations[:2])
    eso = validations[2]["args"]
    assert eso.count("-skip") == 1 and flag_value(eso, "-skip") == "CustomResourceDefinition"


def test_policypal_secrets_local_targets_the_k3d_cluster(fakes: Fakes) -> None:
    result = fakes.make("policypal-secrets-local")
    assert result.returncode == 0, output(result)
    calls = fakes.calls("kubectl")
    switch = [i for i, c in enumerate(calls) if c["args"][:3] == ["config", "use-context", "k3d-devops"]]
    create = [i for i, c in enumerate(calls) if c["args"][:2] == ["create", "secret"]]
    assert switch and create and switch[0] < create[0]
    assert all("devops-aws" not in c["kubeconfig"] for c in calls)


def test_policypal_local_deploys_the_local_overlay_and_smoke_tests_it(fakes: Fakes) -> None:
    result = fakes.make("policypal-local")
    assert result.returncode == 0, output(result)
    renders = [c["args"][-1] for c in fakes.calls("docker") if any("kustomize" in a for a in c["args"])]
    assert renders and renders[-1].endswith("/overlays/local")
    urls = {a for c in fakes.calls("curl") for a in c["args"] if a.startswith("http")}
    assert "http://policypal.localhost:8080/_stcore/health" in urls
    assert not fakes.calls("terraform") and not fakes.calls("aws")


def test_cloud_deploy_uses_the_aws_kubeconfig_and_the_nodes_ip(fakes: Fakes) -> None:
    fakes.eso_synced()
    result = fakes.make("cloud-deploy")
    assert result.returncode == 0, output(result)
    aws_kubeconfig = str(fakes.home / ".kube" / "devops-aws.yaml")
    assert fakes.calls("kubectl") and all(c["kubeconfig"] == aws_kubeconfig for c in fakes.calls("kubectl"))
    renders = [c["args"][-1] for c in fakes.calls("docker") if any("kustomize" in a for a in c["args"])]
    assert renders and renders[-1].endswith("/overlays/aws")
    urls = {a for c in fakes.calls("curl") for a in c["args"] if a.startswith("http")}
    assert f"{UI_URL}/_stcore/health" in urls
    assert not fakes.kubectl("create"), "External Secrets Operator makes the Secrets on AWS, not the deploy"
    assert not fakes.calls("k3d"), "never touches the local cluster"
    assert all(c["profile"] == "devops" for c in fakes.calls("terraform"))


def test_cloud_deploy_kubeconfig_is_the_one_ansible_writes(fakes: Fakes) -> None:
    # group_vars/aws.yml decides where cloud-config puts the kubeconfig; cloud-deploy must use that file.
    fakes.make("cloud-deploy")
    written = yaml.safe_load(AWS_GROUP_VARS.read_text())["kubeconfig_path"]
    expected = written.replace("{{ lookup('env', 'HOME') }}", str(fakes.home))
    assert {c["kubeconfig"] for c in fakes.calls("kubectl")} == {expected}


@pytest.mark.parametrize("ip", ["", "not-an-ip", "203.0.113.300"])
def test_cloud_deploy_stops_without_a_running_node(fakes: Fakes, ip: str) -> None:
    fakes.write("tf-public_ip", ip, rc=0 if ip else 1)
    result = fakes.make("cloud-deploy")
    assert result.returncode != 0
    assert fakes.calls("terraform"), "the target must run and read the IP"
    assert not fakes.calls("kubectl")


def test_cloud_smoke_only_reads(fakes: Fakes) -> None:
    result = fakes.make("cloud-smoke")
    assert result.returncode == 0, output(result)
    aws_kubeconfig = str(fakes.home / ".kube" / "devops-aws.yaml")
    assert all(c["kubeconfig"] == aws_kubeconfig for c in fakes.calls("kubectl"))
    assert fakes.kubectl("port-forward")
    assert not fakes.kubectl("apply") and not fakes.kubectl("create", "secret")
    assert not fakes.kubectl("rollout")


# Calls that name no namespace on purpose: the namespace itself, and the rendered manifests, which carry their
# own (test_k8s_policy.py checks that every object is in policypal).
NAMESPACE_FREE = (["get", "namespace"], ["create", "namespace"], ["apply", "-f", "-"])


@pytest.mark.parametrize("target", ["cloud-deploy", "policypal-local"])
def test_every_namespaced_kubectl_call_targets_policypal(fakes: Fakes, target: str) -> None:
    # Without -n, kubectl uses the context's namespace (default): Secrets, rollouts and the port-forward
    # would all look in the wrong place.
    if target == "cloud-deploy":
        fakes.eso_synced()
    result = fakes.make(target)
    assert result.returncode == 0, output(result)
    namespaced = [c for c in fakes.calls("kubectl")
                  if not any(c["args"][:len(p)] == p for p in NAMESPACE_FREE)
                  and c["args"][:2] not in (["config", "use-context"], ["wait", "--for=condition=Ready"])]
    assert namespaced, "the deploy must have run"
    assert [(c["args"][:2], c["ns"]) for c in namespaced if c["ns"] != "policypal"] == []


@pytest.mark.parametrize("target", ["cloud-deploy", "cloud-smoke"])
def test_deploy_targets_never_change_aws(fakes: Fakes, target: str) -> None:
    fakes.eso_synced()
    result = fakes.make(target)
    assert result.returncode == 0, output(result)
    for call in fakes.calls("terraform"):
        assert call["args"][0] in ("init", "output"), call["args"]
    assert not fakes.calls("aws")


# --- External Secrets Operator: scripts/install-eso.sh, scripts/render-eso.sh, make cloud-addons ----------

def test_install_eso_installs_the_pinned_chart_with_our_values_then_the_store(fakes: Fakes) -> None:
    result = fakes.script("install-eso.sh")
    assert result.returncode == 0, output(result)
    pinned = chart_env()
    [helm] = fakes.calls("helm")
    args = helm["args"]
    assert args[:4] == ["upgrade", "--install", "external-secrets", pinned["ESO_CHART"]]
    assert flag_value(args, "--repo") == pinned["ESO_CHART_REPO"]
    assert flag_value(args, "--version") == pinned["ESO_CHART_VERSION"]
    assert flag_value(args, "--namespace") == "external-secrets"
    assert flag_value(args, "--values") == str(ESO_DIR / "values.yaml")
    assert "--create-namespace" in args and "--wait" in args, "returns when the pods are ready"
    calls = fakes.calls()
    helm_at = next(i for i, c in enumerate(calls) if c["tool"] == "helm")
    store = ["apply", "-f", str(ESO_DIR / "clustersecretstore.yaml")]
    store_at = [i for i, c in enumerate(calls) if c["tool"] == "kubectl" and c["args"] == store]
    assert store_at and store_at[0] > helm_at, "the store's CRD and its webhook come from the chart"


def test_install_eso_stops_when_helm_fails(fakes: Fakes) -> None:
    fakes.flag("fail-helm")
    result = fakes.script("install-eso.sh")
    assert result.returncode != 0
    assert fakes.calls("helm"), "helm must have run"
    assert not fakes.kubectl("apply")


def test_render_eso_renders_the_same_pinned_chart_and_values(fakes: Fakes) -> None:
    result = fakes.script("render-eso.sh")
    assert result.returncode == 0, output(result)
    assert result.stdout == ESO_MANIFEST
    pinned = chart_env()
    [call] = fakes.calls("docker")
    args = call["args"]
    image = next(a for a in args if a.startswith("alpine/helm:"))
    assert re.fullmatch(r"alpine/helm:\d+\.\d+\.\d+", image), "a pinned Helm image"
    helm = args[args.index(image) + 1:]
    assert helm[:3] == ["template", "external-secrets", pinned["ESO_CHART"]]
    assert flag_value(helm, "--repo") == pinned["ESO_CHART_REPO"]
    assert flag_value(helm, "--version") == pinned["ESO_CHART_VERSION"]
    assert flag_value(helm, "--namespace") == "external-secrets"
    assert flag_value(args, "-v") == f"{ESO_DIR}:/eso:ro" and flag_value(helm, "--values") == "/eso/values.yaml"


def test_cloud_addons_installs_eso_on_the_aws_node_only(fakes: Fakes) -> None:
    result = fakes.make("cloud-addons")
    assert result.returncode == 0, output(result)
    aws_kubeconfig = str(fakes.home / ".kube" / "devops-aws.yaml")
    assert fakes.calls("helm") and fakes.kubectl("apply")
    assert all(c["kubeconfig"] == aws_kubeconfig for c in fakes.calls("helm") + fakes.calls("kubectl"))
    assert not fakes.calls("k3d") and not fakes.calls("terraform") and not fakes.calls("aws")


# --- scripts/check-state-secrets.sh, make check-state-secrets -------------------------------------------

DB_PARAM = "/policypal/aws/db-password"
ADMIN_PARAM = "/policypal/aws/admin-token"


def parameter_state(name: str, kind: str, **attributes: Any) -> dict[str, Any]:
    return {"mode": "managed", "type": "aws_ssm_parameter", "name": name.rsplit("/", 1)[-1],
            "instances": [{"attributes": {"name": name, "type": kind, **attributes}}]}


def persistent_state(*, extra: list[dict[str, Any]] | None = None, secure: bool = True) -> str:
    resources = [parameter_state("/devops-platform/owner-cidr", "String", value="198.51.100.7/32")]
    if secure:
        resources += [parameter_state(n, "SecureString", value=None, value_wo=None, value_wo_version=1)
                      for n in (DB_PARAM, ADMIN_PARAM)]
    return json.dumps({"version": 4, "resources": resources + (extra or [])}, indent=2)


@pytest.fixture
def live_values(fakes: Fakes) -> dict[str, str]:
    """Random values for the two parameters, made at run time, served by the fake aws."""
    values = {DB_PARAM: secrets.token_hex(16), ADMIN_PARAM: secrets.token_hex(16)}
    for name, value in values.items():
        fakes.parameter(name, value)
    fakes.log_grep()
    return values


def assert_never_shown(fakes: Fakes, result: subprocess.CompletedProcess[str], values: dict[str, str]) -> None:
    grep_args = (fakes.dir / "grep-args").read_text() if (fakes.dir / "grep-args").exists() else ""
    for value in values.values():
        assert value not in output(result), "a value was printed"
        assert all(value not in " ".join(c["args"]) for c in fakes.calls()), "a value was a command argument"
        assert value not in grep_args, "a value was a grep argument (visible in ps)"


def test_state_check_reports_zero_for_each_secret_without_showing_it(fakes: Fakes, live_values: dict) -> None:
    fakes.write("tf-state", persistent_state())
    result = fakes.script("check-state-secrets.sh")
    assert result.returncode == 0, output(result)
    for name in live_values:
        assert re.search(rf"^{re.escape(name)}: 0$", result.stdout, re.M), result.stdout
    read = sorted(flag_value(c["args"], "--name") for c in fakes.calls("aws"))
    assert read == sorted(live_values), "reads exactly the SecureStrings in the state"
    for call in fakes.calls("aws"):
        assert call["args"][:2] == ["ssm", "get-parameter"] and "--with-decryption" in call["args"]
        assert flag_value(call["args"], "--region") == "ap-southeast-1"
    assert [c["args"] for c in fakes.calls("terraform")] == [["state", "pull"]]
    assert_never_shown(fakes, result, live_values)


def test_state_check_fails_when_a_value_is_in_the_state(fakes: Fakes, live_values: dict) -> None:
    leak = {"mode": "managed", "type": "random_password", "name": "token",
            "instances": [{"attributes": {"result": live_values[ADMIN_PARAM]}}]}
    fakes.write("tf-state", persistent_state(extra=[leak]))
    result = fakes.script("check-state-secrets.sh")
    assert result.returncode == 1
    assert re.search(rf"^{re.escape(ADMIN_PARAM)}: 1$", result.stdout, re.M), result.stdout
    assert re.search(rf"^{re.escape(DB_PARAM)}: 0$", result.stdout, re.M), result.stdout
    assert_never_shown(fakes, result, live_values)


def test_state_check_fails_when_it_has_nothing_to_check(fakes: Fakes, live_values: dict) -> None:
    fakes.write("tf-state", persistent_state(secure=False))
    result = fakes.script("check-state-secrets.sh")
    assert result.returncode != 0
    assert "SecureString" in output(result)
    assert fakes.calls("terraform"), "the state must have been read"


def test_make_check_state_secrets_reads_the_persistent_stack_as_devops(fakes: Fakes, live_values: dict) -> None:
    fakes.write("tf-state", persistent_state())
    result = fakes.make("check-state-secrets")
    assert result.returncode == 0, output(result)
    [pull] = fakes.calls("terraform")
    assert pull["cwd"] == str(REPO_ROOT / "terraform" / "persistent")
    assert {c["profile"] for c in fakes.calls("terraform") + fakes.calls("aws")} == {"devops"}
