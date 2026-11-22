"""Tests for the External Secrets Operator scripts and their Make target.

- scripts/install-eso.sh: installs the pinned chart (k8s/platform/external-secrets/chart.env) with our
  values, then the ClusterSecretStore, whose CRD and webhook come from the chart.
- scripts/render-eso.sh: renders the same chart and values for the checks.
- make cloud-addons: the install, on the AWS node only.

They run with the fake CLIs of tests/fakecli.py (helm, kubectl, docker), so nothing is installed. The rules
for the chart itself (host network, ports, sizes) are checked on the real render in test_eso_platform.py.
"""

import re

from conftest import REPO_ROOT, chart_env
from fakecli import ESO_MANIFEST, Fakes, flag_value, output

ESO_DIR = REPO_ROOT / "k8s" / "platform" / "external-secrets"


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
    fakes.fail("helm")
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
