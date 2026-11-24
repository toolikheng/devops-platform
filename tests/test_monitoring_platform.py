"""Project rules for kube-prometheus-stack (k8s/platform/kube-prometheus-stack).

Checked on the chart as it is installed: `scripts/addon.sh render kube-prometheus-stack ENV` renders the
pinned chart with that environment's values, the same chart and values `addon.sh install` uses.

The alert rules (monitoring/rules) are unit-tested by ci/rules-test.sh with promtool from a pinned Prometheus
image. That must be the Prometheus this chart runs, or the tests prove the rules on another version.
"""

import base64
import configparser
import re
import subprocess
from typing import Any

import pytest
import yaml

from conftest import REPO_ROOT

ADDON = REPO_ROOT / "scripts" / "addon.sh"
RULES_TEST = REPO_ROOT / "ci" / "rules-test.sh"
# The environments kube-prometheus-stack runs in (test_addon_scripts.py checks the full list).
ENVIRONMENTS = ["local", "aws"]


@pytest.fixture(scope="module", params=ENVIRONMENTS)
def kps(request: pytest.FixtureRequest) -> list[dict[str, Any]]:
    """The chart rendered with one environment's values. A render that fails is a test failure, not a skip."""
    result = subprocess.run(["bash", str(ADDON), "render", "kube-prometheus-stack", request.param],
                            capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, f"addon.sh render kube-prometheus-stack {request.param} failed:\n{result.stderr}"
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def test_rules_are_tested_with_the_prometheus_the_chart_runs(kps: list) -> None:
    # The chart's own image, registry and variant included: it carries promtool too.
    [prometheus] = [d for d in kps if d["kind"] == "Prometheus"]
    runs = prometheus["spec"]["image"]
    found = re.search(r'^PROMETHEUS_IMAGE="([^"]+)"', RULES_TEST.read_text(), re.M)
    assert found, "ci/rules-test.sh must name its Prometheus image in PROMETHEUS_IMAGE"
    assert found.group(1) == runs, f"the rules are tested with {found.group(1)}, the chart runs {runs}"


# --- the AWS node: one 4 GiB node next to PolicyPal -------------------------------------------------------

@pytest.fixture(scope="module")
def aws() -> list[dict[str, Any]]:
    result = subprocess.run(["bash", str(ADDON), "render", "kube-prometheus-stack", "aws"], capture_output=True,
                            text=True, timeout=300)
    assert result.returncode == 0, f"addon.sh render kube-prometheus-stack aws failed:\n{result.stderr}"
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def of_kind(docs: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [d for d in docs if d["kind"] == kind]


def pod_specs(docs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Every pod template in the render: Deployments, StatefulSets, DaemonSets and Jobs (hooks included)."""
    return {f"{d['kind']}/{d['metadata']['name']}": d["spec"]["template"]["spec"]
            for d in docs if d["kind"] in ("Deployment", "StatefulSet", "DaemonSet", "Job")}


def grafana(docs: list[dict[str, Any]]) -> dict[str, Any]:
    [spec] = [s for name, s in pod_specs(docs).items() if name == "Deployment/kube-prometheus-stack-grafana"]
    return spec


def test_aws_runs_no_pod_on_the_host_network_and_takes_no_host_port(aws: list) -> None:
    # The chart's node-exporter (a host-network DaemonSet on 9100) would clash with the node_exporter Ansible
    # installs, and on the host network a pod could reach the node's AWS credentials (ADR 017).
    assert of_kind(aws, "DaemonSet") == []
    for name, spec in pod_specs(aws).items():
        assert not spec.get("hostNetwork"), name
        for c in spec["containers"] + spec.get("initContainers", []):
            assert all("hostPort" not in p for p in c.get("ports", [])), f"{name}/{c['name']}"


def test_aws_keeps_one_day_of_metrics_and_nothing_on_disk(aws: list) -> None:
    # Every session starts a new node, so stored metrics would be lost anyway: no volume to bill or fill.
    [prometheus] = of_kind(aws, "Prometheus")
    assert prometheus["spec"]["retention"] == "1d"
    assert "storage" not in prometheus["spec"], "emptyDir: no PersistentVolumeClaim"
    assert of_kind(aws, "PersistentVolumeClaim") == []
    assert of_kind(aws, "Alertmanager") == [], "no receiver is configured, so alerts are read in Prometheus"


def test_aws_scrapes_the_ansible_node_exporter_on_each_node(aws: list) -> None:
    # Found through the Kubernetes API (role: node), so the node's address is never written down; the port is
    # the node_exporter's (9100) instead of the kubelet's.
    [secret] = [s for s in of_kind(aws, "Secret") if "additional-scrape-configs.yaml" in s.get("data", {})]
    jobs = yaml.safe_load(base64.b64decode(secret["data"]["additional-scrape-configs.yaml"]))
    [job] = [j for j in jobs if j["job_name"] == "node-exporter"]
    assert [sd["role"] for sd in job["kubernetes_sd_configs"]] == ["node"]
    assert any(r.get("target_label") == "__address__" and r.get("replacement", "").endswith(":9100")
               for r in job["relabel_configs"])


def test_aws_scrapes_nothing_k3s_does_not_run_on_its_own(aws: list) -> None:
    # k3s runs the controller manager, scheduler and kube-proxy inside one process and uses no etcd here.
    names = {d["metadata"]["name"] for d in of_kind(aws, "ServiceMonitor") + of_kind(aws, "Service")}
    for part in ("kube-controller-manager", "kube-scheduler", "kube-etcd", "kube-proxy"):
        assert not any(part in n for n in names), part


def test_aws_loads_only_the_projects_alert_rules(aws: list) -> None:
    # The chart's default rule groups are written for a full cluster; the project's rules come separately.
    assert of_kind(aws, "PrometheusRule") == []
    [prometheus] = of_kind(aws, "Prometheus")
    assert prometheus["spec"]["ruleSelector"] == {} and prometheus["spec"]["serviceMonitorSelector"] == {}, \
        "rules and ServiceMonitors without the chart's release label are loaded too"


def test_aws_grafana_runs_only_what_it_needs(aws: list) -> None:
    spec = grafana(aws)
    assert {c["name"] for c in spec["containers"]} == {"grafana", "grafana-sc-dashboard"}, \
        "one sidecar, for the project's dashboard"
    assert {c["name"] for c in spec.get("initContainers", [])} == {"grafana-init-sc-datasources"}, \
        "the datasource is read once at start"
    env = {c["name"]: {e["name"]: e.get("value") for e in c.get("env", [])}
           for c in spec["containers"] + spec.get("initContainers", [])}
    # An init container must finish: LIST reads once and exits, WATCH never returns (seen on k3d: the pod
    # stayed in PodInitializing). The datasource is the chart's, in this namespace; the dashboard ships with
    # PolicyPal (k8s/policypal/overlays/aws), so the sidecar watches the namespace that ConfigMap is in.
    assert env["grafana-init-sc-datasources"].get("METHOD") == "LIST"
    assert env["grafana-init-sc-datasources"].get("NAMESPACE") == "monitoring"
    board = yaml.safe_load((REPO_ROOT / "k8s" / "policypal" / "base" / "namespace.yaml").read_text())
    assert env["grafana-sc-dashboard"].get("NAMESPACE") == board["metadata"]["name"] == "policypal"
    for name in ("grafana-init-sc-datasources", "grafana-sc-dashboard"):
        assert env[name].get("RESOURCE") == "configmap", name
    [main] = [c for c in spec["containers"] if c["name"] == "grafana"]
    assert all(e["name"] != "GF_INSTALL_PLUGINS" for e in main.get("env", [])), "no plugins"
    assert all("plugins" not in cm.get("data", {}) for cm in of_kind(aws, "ConfigMap")), "no plugins"
    # Grafana 12 and later download a list of app plugins at every start unless told not to (seen on k3d: the
    # downloads slowed the start until the liveness probe restarted it). It also checks for updates and reports
    # usage to grafana.com; the node has no need to reach it.
    [ini] = [cm["data"]["grafana.ini"] for cm in of_kind(aws, "ConfigMap") if "grafana.ini" in cm.get("data", {})]
    config = configparser.ConfigParser()
    config.read_string(ini)
    assert config.get("plugins", "preinstall_disabled") == "true"
    for key in ("reporting_enabled", "check_for_updates", "check_for_plugin_updates"):
        assert config.get("analytics", key) == "false", key
    assert not [cm for cm in of_kind(aws, "ConfigMap") if "grafana_dashboard" in cm["metadata"].get("labels", {})], \
        "none of the chart's own dashboards"
    assert not any("persistentVolumeClaim" in v for v in spec.get("volumes", [])), "no Grafana volume"


def test_aws_grafana_has_only_the_prometheus_datasource(aws: list) -> None:
    # The chart also adds an Alertmanager datasource, which would point at nothing here (no Alertmanager).
    [cm] = [c for c in of_kind(aws, "ConfigMap") if c["metadata"].get("labels", {}).get("grafana_datasource")]
    sources = [s for text in cm["data"].values() for s in yaml.safe_load(text)["datasources"]]
    assert [(s["name"], s["type"]) for s in sources] == [("Prometheus", "prometheus")]


def test_aws_grafana_password_is_random_and_made_in_the_cluster(aws: list) -> None:
    # Nothing in the values: the chart makes a random password on the first install and keeps it on upgrades.
    values = yaml.safe_load((REPO_ROOT / "k8s" / "platform" / "kube-prometheus-stack" / "values-aws.yaml").read_text())
    assert "adminPassword" not in values["grafana"] and not values["grafana"].get("admin", {}).get("existingSecret")
    [secret] = [s for s in of_kind(aws, "Secret") if "admin-password" in s.get("data", {})]
    password = base64.b64decode(secret["data"]["admin-password"]).decode()
    assert re.fullmatch(r"[A-Za-z0-9]{40}", password) and password not in ("prom-operator", "admin")


def test_aws_every_container_has_requests_and_a_memory_limit(aws: list) -> None:
    for name, spec in pod_specs(aws).items():
        for c in spec["containers"] + spec.get("initContainers", []):
            res = c.get("resources", {})
            assert {"cpu", "memory"} <= set(res.get("requests", {})), f"{name}/{c['name']} requests"
            assert "memory" in res.get("limits", {}), f"{name}/{c['name']} memory limit"
    [prometheus] = of_kind(aws, "Prometheus")
    res = prometheus["spec"].get("resources", {})
    assert {"cpu", "memory"} <= set(res.get("requests", {})) and "memory" in res.get("limits", {}), "Prometheus"



# --- every alert rule is unit-tested ---------------------------------------------------------------------

def test_ci_rules_test_covers_every_prometheus_rule_in_the_repo() -> None:
    # ci/rules-test.sh extracts each PrometheusRule's groups and runs its promtool tests; a rule file it doesn't
    # name would be deployed untested.
    script = RULES_TEST.read_text()
    found = [p for p in REPO_ROOT.rglob("*.yaml") if ".git" not in p.parts and "planning" not in p.parts
             and re.search(r"^kind: PrometheusRule$", p.read_text(errors="ignore"), re.M)]
    assert len(found) >= 2, "the homelab rules and PolicyPal's"
    for path in found:
        assert str(path.relative_to(REPO_ROOT)) in script, path
