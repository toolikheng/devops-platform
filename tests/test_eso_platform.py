"""Project rules for External Secrets Operator (k8s/platform/external-secrets) on the k3s node.

Checked on the chart as it is installed: scripts/render-eso.sh renders the pinned chart (chart.env) with our
values, the same chart and values scripts/install-eso.sh installs. kubeconform (ci/k8s-checks.sh) proves the
YAML is valid; these tests pin what matters here: only the controller runs on the host network (it needs
the node's AWS credentials, and the node's IMDSv2 hop limit of 1 answers host processes only), it takes no
port the node already uses, every pod is sized, images are the pinned version, and the store reads
Parameter Store with the node's role, for the policypal namespace only.
"""

import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from conftest import chart_env

REPO_ROOT = Path(__file__).resolve().parent.parent
ESO_DIR = REPO_ROOT / "k8s" / "platform" / "external-secrets"
RENDER_ESO = REPO_ROOT / "scripts" / "render-eso.sh"

CONTROLLER = "external-secrets"
NOT_ON_HOST = ("external-secrets-webhook", "external-secrets-cert-controller")
# Ports already taken on the node's host network (`ss -ltnp` on the node, 2026-11-22): sshd 22,
# systemd-resolved 53, Traefik through ServiceLB (80, 443), node_exporter 9100, and k3s (API 6443 and its
# internal 6444, kubelet 10248/10250, kube-proxy 10249/10256, controllers 10257 to 10259, containerd 10010).
NODE_PORTS = {22, 53, 80, 443, 6443, 6444, 9100, 10010, 10248, 10249, 10250, 10256, 10257, 10258, 10259}


@pytest.fixture(scope="module")
def eso() -> list[dict[str, Any]]:
    """The ESO chart rendered with our values. A render that fails is a test failure, not a skip."""
    result = subprocess.run(["bash", str(RENDER_ESO)], capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, f"render-eso.sh failed:\n{result.stderr}"
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def store() -> dict[str, Any]:
    return yaml.safe_load((ESO_DIR / "clustersecretstore.yaml").read_text())


def deployments(docs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {d["metadata"]["name"]: d for d in docs if d["kind"] == "Deployment"}


def pod_spec(deployment: dict[str, Any]) -> dict[str, Any]:
    return deployment["spec"]["template"]["spec"]


# --- the chart ------------------------------------------------------------------------------------------

def test_chart_is_pinned_to_one_exact_version() -> None:
    pinned = chart_env()
    assert pinned.get("ESO_CHART") == "external-secrets"
    assert pinned.get("ESO_CHART_REPO") == "https://charts.external-secrets.io"
    assert re.fullmatch(r"\d+\.\d+\.\d+", pinned.get("ESO_CHART_VERSION", ""))


def test_render_has_the_controller_webhook_and_cert_controller(eso: list) -> None:
    assert set(deployments(eso)) == {CONTROLLER, *NOT_ON_HOST}


def test_only_the_controller_runs_on_the_host_network(eso: list) -> None:
    # The webhook listens on 10250, the kubelet's port: on the host network it could not start.
    found = deployments(eso)
    assert pod_spec(found[CONTROLLER]).get("hostNetwork") is True
    for name in NOT_ON_HOST:
        assert not pod_spec(found[name]).get("hostNetwork"), name


def test_the_controller_sets_every_port_it_listens_on(eso: list) -> None:
    # On the host network a port nobody wrote down can clash. The controller always listens for metrics and
    # for health checks: on the node it held 8080 and 8082 while only --metrics-addr was set, because the
    # health port is a built-in default. Both must be set explicitly.
    [c] = pod_spec(deployments(eso)[CONTROLLER])["containers"]
    flags = {a.split("=", 1)[0] for a in c.get("args", [])}
    assert {"--metrics-addr", "--live-addr"} <= flags


def test_the_controller_is_replaced_not_rolled(eso: list) -> None:
    # A rolling update starts the new pod next to the old one. On the host network both need the same node
    # ports, so on one node the new pod stays Pending ("didn't have free ports"): seen on the node at the
    # first helm upgrade. Recreate stops the old pod first; the Secrets it made stay in the cluster.
    assert deployments(eso)[CONTROLLER]["spec"].get("strategy", {}).get("type") == "Recreate"


def test_the_controller_has_a_liveness_probe(eso: list) -> None:
    # The chart leaves it off: a hung controller would stop syncing Secrets and never be restarted.
    [c] = pod_spec(deployments(eso)[CONTROLLER])["containers"]
    assert c.get("livenessProbe", {}).get("httpGet"), "liveness probe on the health port"


def test_the_controller_takes_no_port_the_node_already_uses(eso: list) -> None:
    spec = pod_spec(deployments(eso)[CONTROLLER])
    ports = set()
    for c in spec["containers"]:
        ports |= {p["containerPort"] for p in c.get("ports", [])}
        ports |= {int(m) for a in c.get("args", []) for m in re.findall(r"^--[a-z-]+-addr=[^:]*:(\d+)$", a)}
    assert ports, "the controller's ports must be visible (containerPort or --*-addr)"
    assert not ports & NODE_PORTS, sorted(ports & NODE_PORTS)


def test_every_eso_container_has_requests_and_a_memory_limit(eso: list) -> None:
    for name, d in deployments(eso).items():
        for c in pod_spec(d)["containers"]:
            res = c.get("resources", {})
            assert {"cpu", "memory"} <= set(res.get("requests", {})), f"{name}/{c['name']} requests"
            assert "memory" in res.get("limits", {}), f"{name}/{c['name']} memory limit"


def test_eso_images_are_the_pinned_chart_version(eso: list) -> None:
    expected = f"ghcr.io/external-secrets/external-secrets:v{chart_env()['ESO_CHART_VERSION']}"
    images = {c["image"] for d in deployments(eso).values() for c in pod_spec(d)["containers"]}
    assert images == {expected}


def test_chart_secrets_hold_no_values(eso: list) -> None:
    # The chart ships an empty Secret for the webhook's certificate; cert-controller fills it in the cluster.
    for doc in eso:
        if doc["kind"] == "Secret":
            assert not doc.get("data") and not doc.get("stringData"), doc["metadata"]["name"]


# --- the store ------------------------------------------------------------------------------------------

def test_store_reads_parameter_store_with_the_nodes_role() -> None:
    # No auth block (no keys, no role to assume): the controller uses the node's instance role via IMDSv2.
    found = store()
    assert found["apiVersion"] == "external-secrets.io/v1"
    assert found["kind"] == "ClusterSecretStore"
    assert found["spec"]["provider"] == {"aws": {"service": "ParameterStore", "region": "ap-southeast-1"}}


def test_only_the_policypal_namespace_may_use_the_store() -> None:
    # Any ExternalSecret could otherwise read whatever the node role may read, from any namespace.
    assert store()["spec"]["conditions"] == [{"namespaces": ["policypal"]}]
