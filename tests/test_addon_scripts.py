"""Tests for scripts/addon.sh and the Make targets that use it.

An add-on is a pinned Helm chart with our values, in its own folder k8s/platform/NAME: chart.env (CHART,
CHART_REPO, CHART_VERSION, NAMESPACE, and MANIFESTS: files applied after the chart) and one values file per
environment it runs in (values-local.yaml, values-aws.yaml). The release is called NAME.
- addon.sh install NAME ENV: helm upgrade --install with that environment's values, then the manifests.
- addon.sh render NAME ENV: the same chart and values rendered with the pinned Helm image, then the
  manifests: what ci/k8s-checks.sh validates and the platform tests read.
- make cloud-addons: the add-ons of the AWS node. make monitoring-up: kube-prometheus-stack on k3d (homelab).

They run with the fake CLIs of tests/fakecli.py (helm, kubectl, docker), so nothing is installed. The rules
for each chart (host network, ports, sizes, versions) are checked on the real render in
test_eso_platform.py and test_monitoring_platform.py.
"""

import re

import pytest

from conftest import PLATFORM, REPO_ROOT, addon_envs, addon_manifests, chart_env, versions
from fakecli import Fakes, addon_manifest, flag_value, output

# Every add-on and where it runs. External Secrets Operator: AWS only (k3d has no AWS identity, and AWS keys
# in k3d would be long-lived credentials). kube-prometheus-stack: k3d for the homelab.
ADDONS = [("external-secrets", "aws"), ("kube-prometheus-stack", "local")]
MONITORING = REPO_ROOT / "monitoring"


def rendered(name: str) -> str:
    """What `addon.sh render NAME ENV` prints with the fake Helm: the chart, then each manifest."""
    return addon_manifest(name) + "".join(f"---\n{m.read_text()}" for m in addon_manifests(name))


def test_these_are_the_addons_and_where_they_run() -> None:
    assert addon_envs() == ADDONS


@pytest.mark.parametrize("name", sorted({name for name, _ in ADDONS}))
def test_each_addon_pins_one_exact_chart_version(name: str) -> None:
    pinned = chart_env(name)
    assert {"CHART", "CHART_REPO", "CHART_VERSION", "NAMESPACE"} <= set(pinned)
    assert pinned["CHART_REPO"].startswith("https://")
    assert re.fullmatch(r"\d+\.\d+\.\d+", pinned["CHART_VERSION"])
    assert all(m.is_file() for m in addon_manifests(name)), "every manifest it lists exists"


def test_external_secrets_applies_its_store_after_the_chart() -> None:
    # The store's CRD and the webhook that validates it are part of the chart.
    assert addon_manifests("external-secrets") == [PLATFORM / "external-secrets" / "clustersecretstore.yaml"]


@pytest.mark.parametrize("name, env", ADDONS)
def test_install_installs_the_pinned_chart_with_the_environments_values_then_its_manifests(
        fakes: Fakes, name: str, env: str) -> None:
    result = fakes.script("addon.sh", "install", name, env)
    assert result.returncode == 0, output(result)
    pinned = chart_env(name)
    [helm] = fakes.calls("helm")
    args = helm["args"]
    assert args[:4] == ["upgrade", "--install", name, pinned["CHART"]]
    assert flag_value(args, "--repo") == pinned["CHART_REPO"]
    assert flag_value(args, "--version") == pinned["CHART_VERSION"]
    assert flag_value(args, "--namespace") == pinned["NAMESPACE"]
    assert flag_value(args, "--values") == str(PLATFORM / name / f"values-{env}.yaml")
    assert "--create-namespace" in args and "--wait" in args, "returns when the pods are ready"
    calls = fakes.calls()
    helm_at = next(i for i, c in enumerate(calls) if c["tool"] == "helm")
    applied = [(i, c["args"]) for i, c in enumerate(calls) if c["tool"] == "kubectl"]
    assert [a for _, a in applied] == [["apply", "-f", str(m)] for m in addon_manifests(name)]
    assert all(i > helm_at for i, _ in applied), "applied after the chart"


def test_install_stops_when_helm_fails(fakes: Fakes) -> None:
    fakes.fail("helm")
    result = fakes.script("addon.sh", "install", "external-secrets", "aws")
    assert result.returncode != 0
    assert fakes.calls("helm"), "helm must have run"
    assert not fakes.kubectl("apply")


@pytest.mark.parametrize("args, message", [
    (["install", "external-secrets", "local"], "external-secrets has no values for local"),
    (["render", "no-such-addon", "aws"], "no add-on no-such-addon"),
    (["install", "../external-secrets", "aws"], "usage:"),
    (["install", "external-secrets", "../aws"], "usage:"),
    (["upgrade", "external-secrets", "aws"], "usage:"),
    (["install", "external-secrets"], "usage:"),
])
def test_refuses_what_it_cannot_do(fakes: Fakes, args: list[str], message: str) -> None:
    result = fakes.script("addon.sh", *args)
    assert result.returncode != 0
    assert message in result.stderr
    assert not fakes.calls("helm") and not fakes.calls("kubectl") and not fakes.calls("docker")


@pytest.mark.parametrize("name, env", ADDONS)
def test_render_renders_the_same_pinned_chart_and_values_then_the_manifests(
        fakes: Fakes, name: str, env: str) -> None:
    result = fakes.script("addon.sh", "render", name, env)
    assert result.returncode == 0, output(result)
    assert result.stdout == rendered(name)
    pinned = chart_env(name)
    [call] = fakes.calls("docker")
    args = call["args"]
    image = next(a for a in args if a.startswith("alpine/helm:"))
    assert re.fullmatch(r"alpine/helm:\d+\.\d+\.\d+", image), "a pinned Helm image"
    helm = args[args.index(image) + 1:]
    assert helm[:3] == ["template", name, pinned["CHART"]]
    assert flag_value(helm, "--repo") == pinned["CHART_REPO"]
    assert flag_value(helm, "--version") == pinned["CHART_VERSION"]
    assert flag_value(helm, "--namespace") == pinned["NAMESPACE"]
    assert flag_value(helm, "--kube-version") == versions()["KUBERNETES_VERSION"], "the node's Kubernetes"
    assert flag_value(args, "-v") == f"{PLATFORM / name}:/addon:ro"
    assert flag_value(helm, "--values") == f"/addon/values-{env}.yaml"


# --- make targets ---------------------------------------------------------------------------------------

def test_cloud_addons_installs_external_secrets_on_the_aws_node_only(fakes: Fakes) -> None:
    result = fakes.make("cloud-addons")
    assert result.returncode == 0, output(result)
    aws_kubeconfig = str(fakes.home / ".kube" / "devops-aws.yaml")
    [helm] = fakes.calls("helm")
    assert helm["args"][:3] == ["upgrade", "--install", "external-secrets"]
    assert flag_value(helm["args"], "--values") == str(PLATFORM / "external-secrets" / "values-aws.yaml")
    assert fakes.kubectl("apply"), "the store"
    assert all(c["kubeconfig"] == aws_kubeconfig for c in fakes.calls("helm") + fakes.calls("kubectl"))
    assert not fakes.calls("k3d") and not fakes.calls("terraform") and not fakes.calls("aws")


def test_monitoring_up_installs_kube_prometheus_stack_on_k3d_through_the_addon(fakes: Fakes) -> None:
    result = fakes.make("monitoring-up")
    assert result.returncode == 0, output(result)
    # One helm call: the chart's repository is a flag, so no `helm repo add` or `helm repo update`.
    [helm] = fakes.calls("helm")
    assert helm["args"][:3] == ["upgrade", "--install", "kube-prometheus-stack"]
    assert flag_value(helm["args"], "--values") == str(PLATFORM / "kube-prometheus-stack" / "values-local.yaml")
    calls = fakes.calls()
    helm_at = next(i for i, c in enumerate(calls) if c["tool"] == "helm")
    after = [c["args"] for c in calls[helm_at + 1:] if c["tool"] == "kubectl"]
    assert after == [
        ["apply", "-f", str(MONITORING / "servicemonitor.yaml"), "-f", str(MONITORING / "rules" / "alert-rules.yaml")],
        ["apply", "-k", str(MONITORING / "dashboards")],
    ], "what is monitored comes after the chart that brings the CRDs"
