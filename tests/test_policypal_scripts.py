"""Tests for the PolicyPal deploy scripts and their Make targets.

- scripts/policypal-secrets.sh: creates the two temporary Secrets once, with random values, never printed.
- scripts/smoke-test.sh: /health, one answered and cited question, one refusal (the API through a kubectl
  port-forward to its Service), the UI through the ingress, and the API not reachable through it.
- scripts/deploy-policypal.sh: render, apply, wait, smoke test, and roll back what it changed on failure.
- make k8s-check, policypal-secrets-local, policypal-local, cloud-deploy, cloud-smoke.

They run with fake `kubectl`, `curl`, `docker`, `k3d`, `terraform` and `aws` first on PATH, so no cluster,
container or AWS account is touched. Each fake appends one JSON line per call (tool, args, KUBECONFIG,
AWS_PROFILE; for kubectl also the namespace, taken out of the args) to a log, and answers from files the test
writes (secrets that exist, revisions, API replies).
"""

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
AWS_GROUP_VARS = REPO_ROOT / "ansible" / "inventory" / "group_vars" / "aws.yml"

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
                          "profile": os.environ.get("AWS_PROFILE", ""), "stdin": stdin}) + "\n")

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
    if a[:1] == ["apply"]:
        (d / "applied").write_text(stdin)
        sys.exit(1 if (d / "fail-apply").exists() else 0)
    if a[:2] == ["rollout", "status"]:
        sys.exit(1 if (d / f"fail-rollout-{a[2].split('/')[-1]}").exists() else 0)
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
    sys.exit(0)
if tool == "terraform" and args[:2] == ["output", "-raw"]:
    answer(f"tf-{args[2]}", 1)
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
        for tool in ("kubectl", "curl", "docker", "k3d", "terraform", "aws"):
            path = self.bin / tool
            path.write_text(FAKE)
            path.chmod(0o755)
        self.write("manifest", MANIFEST)
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

    def created_secrets(self) -> dict[str, set[str]]:
        """Secret name -> keys, for every Secret the scripts created."""
        return {p.name.removeprefix("secret-"): set(self.created_secret(p.name.removeprefix("secret-")))
                for p in self.dir.glob("secret-*")}

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


def secret_keys_referenced(docs: list[dict[str, Any]]) -> dict[str, set[str]]:
    """Secret name -> keys, from every secretKeyRef in rendered manifests: what the cluster must hold."""
    keys: dict[str, set[str]] = {}
    for doc in docs:
        if doc["kind"] in ("Deployment", "StatefulSet"):
            spec = doc["spec"]["template"]["spec"]
            for c in spec["containers"] + spec.get("initContainers", []):
                for e in c.get("env", []):
                    ref = e.get("valueFrom", {}).get("secretKeyRef")
                    if ref:
                        keys.setdefault(ref["name"], set()).add(ref["key"])
    return keys


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
        assert fakes.created_secrets() == secret_keys_referenced(docs), overlay
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


def test_database_url_carries_the_same_random_password(fakes: Fakes) -> None:
    fakes.script("policypal-secrets.sh")
    db = fakes.created_secret("policypal-db")
    password = db["POSTGRES_PASSWORD"]
    assert re.fullmatch(r"[0-9a-f]{48}", password), "48 hex characters: safe inside a URL"
    assert db["DATABASE_URL"] == f"postgresql+psycopg://policypal:{password}@postgres:5432/policypal"
    assert re.fullmatch(r"[0-9a-f]{32,}", fakes.created_secret("policypal-admin")["ADMIN_TOKEN"])


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
    fakes.secret_exists("policypal-db")
    fakes.secret_exists("policypal-admin")
    result = fakes.script("deploy-policypal.sh", "aws", UI_URL)
    assert result.returncode == 0, output(result)

    checked = [sorted(a[2:]) for a in fakes.kubectl("get", "secret")]
    assert checked == [sorted(secret_keys_referenced(rendered["aws"]))], "checks exactly the Secrets the pods need"

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


def test_deploy_stops_before_applying_when_the_secrets_are_missing(fakes: Fakes) -> None:
    fakes.secret_exists("policypal-admin")
    result = fakes.script("deploy-policypal.sh", "aws", UI_URL)
    assert result.returncode != 0
    assert "policypal-secrets" in output(result)
    assert not fakes.kubectl("apply")


def test_failed_rollout_rolls_back_what_this_deploy_changed(fakes: Fakes) -> None:
    fakes.secret_exists("policypal-db")
    fakes.secret_exists("policypal-admin")
    fakes.revisions("policypal-api", before="3", after="4")
    fakes.revisions("policypal-ui", before="2", after="2")
    fakes.flag("fail-rollout-policypal-api")
    result = fakes.script("deploy-policypal.sh", "aws", UI_URL)
    assert result.returncode != 0
    undos = fakes.kubectl("rollout", "undo")
    assert undos == [["rollout", "undo", "deployment/policypal-api", "--to-revision=3"]]


def test_failed_smoke_test_rolls_back_too(fakes: Fakes) -> None:
    fakes.secret_exists("policypal-db")
    fakes.secret_exists("policypal-admin")
    fakes.revisions("policypal-api", before="3", after="4")
    fakes.revisions("policypal-ui", before="2", after="3")
    fakes.api(g41=ANSWER_G04)
    result = fakes.script("deploy-policypal.sh", "aws", UI_URL)
    assert result.returncode != 0
    assert sorted(fakes.kubectl("rollout", "undo")) == [
        ["rollout", "undo", "deployment/policypal-api", "--to-revision=3"],
        ["rollout", "undo", "deployment/policypal-ui", "--to-revision=2"],
    ]


def test_first_deploy_has_nothing_to_roll_back(fakes: Fakes) -> None:
    fakes.secret_exists("policypal-db")
    fakes.secret_exists("policypal-admin")
    fakes.revisions("policypal-api", before=None, after="1")
    fakes.flag("fail-rollout-policypal-api")
    result = fakes.script("deploy-policypal.sh", "aws", UI_URL)
    assert result.returncode != 0
    assert ["rollout", "status", "deployment/policypal-api"] in [a[:3] for a in fakes.kubectl("rollout", "status")]
    assert not fakes.kubectl("rollout", "undo")


def test_deploy_stops_when_apply_fails(fakes: Fakes) -> None:
    fakes.secret_exists("policypal-db")
    fakes.secret_exists("policypal-admin")
    fakes.flag("fail-apply")
    result = fakes.script("deploy-policypal.sh", "aws", UI_URL)
    assert result.returncode != 0
    assert fakes.kubectl("apply", "-f", "-"), "the apply must have been attempted"
    assert not fakes.kubectl("rollout", "status")


# --- make targets ---------------------------------------------------------------------------------------

def test_k8s_check_validates_both_overlays_strictly_against_the_nodes_version(fakes: Fakes) -> None:
    result = fakes.make("k8s-check")
    assert result.returncode == 0, output(result)
    docker = fakes.calls("docker")
    renders = [c["args"][-1] for c in docker if any("kustomize" in a for a in c["args"])]
    assert [r.rsplit("/", 1)[-1] for r in renders] == ["local", "aws"]
    validations = [c for c in docker if any("kubeconform" in a for a in c["args"])]
    assert len(validations) == 2
    for call in validations:
        assert "-strict" in call["args"]
        assert call["args"][call["args"].index("-kubernetes-version") + 1] == "1.36.6"
        assert call["stdin"] == MANIFEST, "kubeconform reads the rendered YAML"


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
    result = fakes.make("cloud-deploy")
    assert result.returncode == 0, output(result)
    aws_kubeconfig = str(fakes.home / ".kube" / "devops-aws.yaml")
    assert fakes.calls("kubectl") and all(c["kubeconfig"] == aws_kubeconfig for c in fakes.calls("kubectl"))
    renders = [c["args"][-1] for c in fakes.calls("docker") if any("kustomize" in a for a in c["args"])]
    assert renders and renders[-1].endswith("/overlays/aws")
    urls = {a for c in fakes.calls("curl") for a in c["args"] if a.startswith("http")}
    assert f"{UI_URL}/_stcore/health" in urls
    assert fakes.created_secret("policypal-db"), "temporary Secrets are created on a new cluster"
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
    result = fakes.make(target)
    assert result.returncode == 0, output(result)
    namespaced = [c for c in fakes.calls("kubectl")
                  if not any(c["args"][:len(p)] == p for p in NAMESPACE_FREE)
                  and c["args"][:2] not in (["config", "use-context"], ["wait", "--for=condition=Ready"])]
    assert namespaced, "the deploy must have run"
    assert [(c["args"][:2], c["ns"]) for c in namespaced if c["ns"] != "policypal"] == []


@pytest.mark.parametrize("target", ["cloud-deploy", "cloud-smoke"])
def test_deploy_targets_never_change_aws(fakes: Fakes, target: str) -> None:
    result = fakes.make(target)
    assert result.returncode == 0, output(result)
    for call in fakes.calls("terraform"):
        assert call["args"][0] in ("init", "output"), call["args"]
    assert not fakes.calls("aws")
