"""Tests for the PolicyPal deploy scripts and their Make targets.

- scripts/policypal-secrets.sh: creates the two Secrets on k3d once, with random values, never printed (on AWS,
  External Secrets Operator makes them from SSM Parameter Store).
- scripts/smoke-test.sh: /health, one answered and cited question, one refusal (the API through a kubectl
  port-forward to its Service), the UI through the ingress, and the API not reachable through it.
- scripts/deploy-policypal.sh: render (or take the file CI rendered), apply, wait for the Secrets and the
  rollout, smoke test, and roll back what it changed on failure.
- scripts/render-k8s.sh with a PolicyPal commit: the overlay with that image tag, the repo left as it is.
- scripts/node-deploy.sh: on the AWS node (sent by CI through SSM Run Command), the deploy with the node's own
  kubeconfig and address.
- make k8s-check, policypal-secrets-local, policypal-local, cloud-deploy, cloud-smoke.
The add-on script (External Secrets Operator, kube-prometheus-stack) is tested in test_addon_scripts.py, the
state check in
test_check_state_secrets.py.

They run with the fake CLIs of tests/fakecli.py first on PATH, so no cluster, container or AWS account is
touched; every call is logged, and the fakes answer what each test sets up.
"""

import json
import re
import subprocess

import pytest
import yaml

from conftest import RENDER, REPO_ROOT, addon_envs, addon_manifests, secret_refs, versions
from fakecli import FORWARD_PORT, MANIFEST, NODE_IP, Fakes, addon_manifest, flag_value, output

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
UI_URL = f"http://{NODE_IP}"  # cloud-deploy builds it from the node's public IP
API = f"http://127.0.0.1:{FORWARD_PORT}"


def serve_api(fakes: Fakes, health: dict | None = None, g04: dict | None = None, g41: dict | None = None,
        health_rc: int | None = None) -> None:
    """How the API answers the smoke test, through the port-forward (any host)."""
    if health is not None:
        fakes.http("/health", json.dumps(health), rc=health_rc)
    if g04 is not None:
        fakes.http("/ask", json.dumps(g04), when="compassionate")
    if g41 is not None:
        fakes.http("/ask", json.dumps(g41), when="bonus")


def serve_ui(fakes: Fakes, health: str | None = None, docs: str | None = None, openapi: str | None = None) -> None:
    """How the UI's host answers through the ingress."""
    for path, body in (("/_stcore/health", health), ("/docs", docs), ("/openapi.json", openapi)):
        if body is not None:
            fakes.http(path, body)


@pytest.fixture
def fakes(fakes: Fakes) -> Fakes:
    """The fake CLIs (tests/fakecli.py), with a healthy PolicyPal behind the port-forward and the ingress."""
    serve_api(fakes, health=HEALTH_OK, g04=ANSWER_G04, g41=ANSWER_G41)
    serve_ui(fakes, health="ok", docs="<html>Streamlit</html>", openapi="<html>Streamlit</html>")
    return fakes


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
    fakes.namespace_exists()
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
    fakes.forward_port("45678")
    result = fakes.script("smoke-test.sh", UI_URL)
    assert result.returncode == 0, output(result)
    api_urls = {a for c in fakes.calls("curl") for a in c["args"] if a.startswith("http://127.0.0.1")}
    assert api_urls == {"http://127.0.0.1:45678/health", "http://127.0.0.1:45678/ask"}


def test_smoke_test_fails_when_the_port_forward_does_not_start(fakes: Fakes) -> None:
    # Then nothing must be asked on localhost: whatever else listens there (another forward, maybe to another
    # cluster) would answer, and the checks would pass for the wrong cluster.
    fakes.fail("kubectl", "port-forward")
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
    serve_api(fakes, **change)
    result = fakes.script("smoke-test.sh", UI_URL)
    assert result.returncode != 0, why
    assert fakes.kubectl("port-forward"), "the check must actually run"


@pytest.mark.parametrize("ui, why", [
    ({"health": "not ok"}, "UI unhealthy"),
    ({"docs": '<html><div id="swagger-ui"></div></html>'}, "Swagger reachable from outside"),
    ({"openapi": '{"openapi": "3.1.0", "info": {"title": "PolicyPal"}}'}, "API schema reachable from outside"),
])
def test_smoke_test_fails_when_the_ingress_is_wrong(fakes: Fakes, ui: dict, why: str) -> None:
    serve_ui(fakes, **ui)
    result = fakes.script("smoke-test.sh", UI_URL)
    assert result.returncode != 0, why
    assert "FAIL" in output(result), "the smoke test must report the failed check"


def test_smoke_test_fails_when_the_ui_is_unreachable(fakes: Fakes) -> None:
    fakes.http("/_stcore/health", None)  # the fake curl then exits 7: could not connect
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
    [apply] = [c for c in calls if c["args"][:2] == ["apply", "-f"]]
    assert apply["stdin"] == MANIFEST, "exactly the rendered YAML is applied"
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
    fakes.fail("kubectl", "rollout", "status", "deployment/policypal-api")
    result = fakes.script("deploy-policypal.sh", "aws", UI_URL)
    assert result.returncode != 0
    undos = fakes.kubectl("rollout", "undo")
    assert undos == [["rollout", "undo", "deployment/policypal-api", "--to-revision=3"]]


def test_failed_smoke_test_rolls_back_too(fakes: Fakes) -> None:
    fakes.eso_synced()
    fakes.revisions("policypal-api", before="3", after="4")
    fakes.revisions("policypal-ui", before="2", after="3")
    serve_api(fakes, g41=ANSWER_G04)
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
    serve_api(fakes, g41=ANSWER_G04)
    fakes.fail("kubectl", "rollout", "undo", "deployment/policypal-api")
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
    fakes.fail("kubectl", "rollout", "status", "deployment/policypal-api")
    result = fakes.script("deploy-policypal.sh", "aws", UI_URL)
    assert result.returncode != 0
    assert ["rollout", "status", "deployment/policypal-api"] in [a[:3] for a in fakes.kubectl("rollout", "status")]
    assert not fakes.kubectl("rollout", "undo")


def test_deploy_stops_when_apply_fails(fakes: Fakes) -> None:
    fakes.eso_synced()
    fakes.fail("kubectl", "apply")
    result = fakes.script("deploy-policypal.sh", "aws", UI_URL)
    assert result.returncode != 0
    assert fakes.kubectl("apply", "-f", "-"), "the apply must have been attempted"
    assert not fakes.kubectl("rollout", "status")


def test_deploy_applies_the_file_ci_rendered_instead_of_rendering(fakes: Fakes, tmp_path) -> None:
    # On the node there is no Docker for the pinned kustomize: CI renders and checks, the node applies that.
    fakes.eso_synced()
    sent = tmp_path / "policypal.yaml"
    sent.write_text("apiVersion: v1\nkind: Namespace\nmetadata:\n  name: policypal\n# from CI\n")
    result = fakes.script("deploy-policypal.sh", "aws", UI_URL, env={"DEPLOY_MANIFESTS": str(sent)})
    assert result.returncode == 0, output(result)
    [apply] = [c for c in fakes.calls("kubectl") if c["args"][:2] == ["apply", "-f"]]
    assert apply["stdin"] == sent.read_text()
    assert not fakes.calls("docker"), "nothing is rendered on the node"


def test_deploy_stops_when_the_given_file_is_missing(fakes: Fakes, tmp_path) -> None:
    result = fakes.script("deploy-policypal.sh", "aws", UI_URL, env={"DEPLOY_MANIFESTS": str(tmp_path / "none")})
    assert result.returncode == 2
    assert not fakes.kubectl("apply")


# --- scripts/render-k8s.sh with a PolicyPal commit ------------------------------------------------------

OTHER_TAG = "0" * 40  # the rollback demo's tag: a valid commit hash with no image behind it


@pytest.mark.parametrize("tag", ["latest", OTHER_TAG.replace("0", "A"), OTHER_TAG[:39], "0;id"])
def test_render_takes_only_a_full_lowercase_commit_as_the_tag(fakes: Fakes, tag: str) -> None:
    result = fakes.script("render-k8s.sh", "aws", tag)
    assert result.returncode == 2
    assert not fakes.calls("docker")


def test_render_with_a_tag_reads_the_overlay_unchanged_through_a_wrapper(fakes: Fakes) -> None:
    # Kustomize's own images field sets the tag, in a two-line kustomization on top of the overlay: the repo's
    # k8s/ is mounted read-only as it is (no copy, no text editing of YAML).
    overlay = REPO_ROOT / "k8s" / "policypal" / "overlays" / "aws" / "kustomization.yaml"
    before = overlay.read_text()
    result = fakes.script("render-k8s.sh", "aws", OTHER_TAG)
    assert result.returncode == 0, output(result)
    assert overlay.read_text() == before
    [render] = [c for c in fakes.calls("docker") if any("kustomize" in a for a in c["args"])]
    args = render["args"]
    mounts = [args[i + 1] for i, a in enumerate(args) if a == "-v"]
    assert mounts[0] == f"{REPO_ROOT / 'k8s'}:/k8s:ro", "the repo's manifests, read-only"
    assert len(mounts) == 2 and mounts[1].endswith(":/tag:ro") and args[-1] == "/tag", "built through the wrapper"


def test_render_with_a_tag_changes_only_the_policypal_image_tag(rendered: dict) -> None:
    # Real render (pinned kustomize image), compared with the overlay's own render.
    result = subprocess.run(["bash", str(RENDER), "aws", OTHER_TAG], capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    docs = [d for d in yaml.safe_load_all(result.stdout) if d]
    pinned = next(c["image"] for d in rendered["aws"] if d["kind"] == "Deployment"
                  for c in d["spec"]["template"]["spec"]["containers"] if "policypal" in c["image"])
    images = [c["image"] for d in docs if d["kind"] in ("Deployment", "StatefulSet")
              for c in d["spec"]["template"]["spec"]["containers"] + d["spec"]["template"]["spec"].get("initContainers", [])]
    assert sorted({i for i in images if i.startswith("ghcr.io/toolikheng/policypal")}) == [f"ghcr.io/toolikheng/policypal:{OTHER_TAG}"]
    assert yaml.safe_dump_all(docs).replace(OTHER_TAG, pinned.rsplit(":", 1)[1]) == yaml.safe_dump_all(rendered["aws"])


# --- scripts/node-deploy.sh (on the AWS node) -----------------------------------------------------------

NODE_PRIVATE_IP = "10.20.1.17"
IMDS = "http://169.254.169.254/latest"


@pytest.fixture
def on_node(fakes: Fakes, tmp_path):
    """The node: instance metadata (IMDSv2), ESO-made Secrets, and the manifest file CI sent."""
    fakes.http(f"{IMDS}/api/token", "TOKEN-from-imds", when="PUT")
    fakes.http(f"{IMDS}/meta-data/local-ipv4", NODE_PRIVATE_IP)
    fakes.eso_synced()
    sent = tmp_path / "policypal.yaml"
    sent.write_text(MANIFEST)
    return sent


def test_node_deploy_applies_the_sent_file_and_checks_the_ui_on_the_nodes_own_address(fakes: Fakes, on_node) -> None:
    # The security group lets only the owner's IP in on port 80; the node reaches its own ingress through
    # its private address, which never leaves the machine.
    result = fakes.script("node-deploy.sh", str(on_node))
    assert result.returncode == 0, output(result)
    [apply] = [c for c in fakes.calls("kubectl") if c["args"][:2] == ["apply", "-f"]]
    assert apply["stdin"] == MANIFEST and not fakes.calls("docker")
    ui = [c for c in fakes.calls("curl") if any(a.startswith(f"http://{NODE_PRIVATE_IP}/") for a in c["args"])]
    assert ui, "the smoke test reaches the UI through the ingress on the node's address"


def test_node_deploy_asks_imds_v2_for_the_address(fakes: Fakes, on_node) -> None:
    fakes.script("node-deploy.sh", str(on_node))
    [token] = [c["args"] for c in fakes.calls("curl") if f"{IMDS}/api/token" in c["args"]]
    assert "PUT" in token and any(a.startswith("X-aws-ec2-metadata-token-ttl-seconds:") for a in token)
    [address] = [c["args"] for c in fakes.calls("curl") if f"{IMDS}/meta-data/local-ipv4" in c["args"]]
    assert "X-aws-ec2-metadata-token: TOKEN-from-imds" in address


def test_node_deploy_uses_the_k3s_admin_kubeconfig(fakes: Fakes, on_node) -> None:
    fakes.script("node-deploy.sh", str(on_node))
    assert {c["kubeconfig"] for c in fakes.calls("kubectl")} == {"/etc/rancher/k3s/k3s.yaml"}


def test_node_deploy_stops_without_instance_metadata(fakes: Fakes, on_node) -> None:
    fakes.http(f"{IMDS}/api/token", None, when="PUT")
    result = fakes.script("node-deploy.sh", str(on_node))
    assert result.returncode == 1
    assert "metadata" in output(result)
    assert not fakes.kubectl("apply")


@pytest.mark.parametrize("args", [[], ["/no/such/file.yaml"]])
def test_node_deploy_needs_the_manifest_file(fakes: Fakes, args: list[str]) -> None:
    result = fakes.script("node-deploy.sh", *args)
    assert result.returncode == 2
    assert not fakes.calls("kubectl") and not fakes.calls("curl")


# --- make targets ---------------------------------------------------------------------------------------

def test_k8s_check_validates_policypal_and_every_addon_strictly_against_the_nodes_version(fakes: Fakes) -> None:
    result = fakes.make("k8s-check")
    assert result.returncode == 0, output(result)
    docker = fakes.calls("docker")
    renders = [c["args"][-1] for c in docker if any("kustomize" in a for a in c["args"])]
    assert [r.rsplit("/", 1)[-1] for r in renders] == ["local", "aws"]
    helm = [c["args"] for c in docker if any(a.startswith("alpine/helm:") for a in c["args"])]
    addons = addon_envs()
    assert addons, "the add-ons must be found"
    assert [(a[a.index("template") + 1], flag_value(a, "--values")) for a in helm] == \
        [(name, f"/addon/values-{env}.yaml") for name, env in addons], "one render per add-on and environment"
    validations = [c for c in docker if any("kubeconform" in a for a in c["args"])]
    assert len(validations) == 2 + len(addons)
    catalog = re.compile(r"https://raw\.githubusercontent\.com/datreeio/CRDs-catalog/[0-9a-f]{40}/"
                         r"\{\{\.Group\}\}/\{\{\.ResourceKind\}\}_\{\{\.ResourceAPIVersion\}\}\.json")
    for call in validations:
        args = call["args"]
        assert "-strict" in args
        assert flag_value(args, "-kubernetes-version") == versions()["KUBERNETES_VERSION"]
        # Kubernetes' own schemas, then the CRD schemas (ExternalSecret, ClusterSecretStore, ServiceMonitor, ...)
        # at a pinned commit.
        locations = [args[i + 1] for i, a in enumerate(args) if a == "-schema-location"]
        assert locations[0] == "default" and len(locations) == 2 and catalog.fullmatch(locations[1])
    assert [v["stdin"] for v in validations[:2]] == [MANIFEST, MANIFEST], "kubeconform reads the rendered YAML"
    for v, (name, _) in zip(validations[2:], addons):
        expected = addon_manifest(name) + "".join(f"---\n{m.read_text()}" for m in addon_manifests(name))
        assert v["stdin"] == expected, f"{name}: the chart, then its manifests"
    # No schema exists for the CustomResourceDefinition kind, so only the add-on runs skip it, and nothing else.
    assert all("-skip" not in v["args"] for v in validations[:2])
    for v in validations[2:]:
        assert v["args"].count("-skip") == 1 and flag_value(v["args"], "-skip") == "CustomResourceDefinition"


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
    fakes.tf_output("public_ip", ip or None)
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
