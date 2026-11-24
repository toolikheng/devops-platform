"""Project rules for the PolicyPal manifests (k8s/policypal), checked on the rendered YAML of both overlays.

kubeconform (ci/k8s-checks.sh) proves the YAML is valid Kubernetes. These tests pin what kubeconform can't
know: every container is sized and probed, images are pinned, no Secret is committed, the API is a single
writer that is never exposed, PolicyPal runs without an LLM, and the local overlay deploys the same objects
as the AWS one, so testing on k3d first means something. The one difference: on AWS, External Secrets
Operator makes the Secrets from SSM Parameter Store (ExternalSecrets in overlays/aws); on k3d, which has no
AWS identity, scripts/policypal-secrets.sh does.
"""

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from conftest import OVERLAYS, secret_catalog, secret_refs

REPO_ROOT = Path(__file__).resolve().parent.parent
DEPLOY_SCRIPT = REPO_ROOT / "scripts" / "deploy-policypal.sh"
SECRET_STORE = REPO_ROOT / "k8s" / "platform" / "external-secrets" / "clustersecretstore.yaml"

NAMESPACE = "policypal"
POLICYPAL_IMAGE = re.compile(r"^ghcr\.io/toolikheng/policypal(:[0-9a-f]{40}|@sha256:[0-9a-f]{64})$")
POSTGRES_IMAGE = "pgvector/pgvector:0.8.7-pg17"
WORKLOADS = {("StatefulSet", "postgres"), ("Deployment", "policypal-api"), ("Deployment", "policypal-ui")}
EXPECTED = WORKLOADS | {
    ("Namespace", "policypal"),
    ("Service", "postgres"),
    ("Service", "policypal-api"),
    ("Service", "policypal-ui"),
    ("PersistentVolumeClaim", "policypal-uploads"),
    ("Ingress", "policypal"),
}
EXTERNAL_SECRETS = {("ExternalSecret", "policypal-db"), ("ExternalSecret", "policypal-admin")}
EXPECTED_BY_OVERLAY = {"local": EXPECTED, "aws": EXPECTED | EXTERNAL_SECRETS}
overlay_param = pytest.mark.parametrize("overlay", OVERLAYS)


# --- helpers --------------------------------------------------------------------------------------------

def get(docs: list[dict[str, Any]], kind: str, name: str) -> dict[str, Any]:
    found = [d for d in docs if d["kind"] == kind and d["metadata"]["name"] == name]
    assert len(found) == 1, f"expected exactly one {kind}/{name}, found {len(found)}"
    return found[0]


def pod_spec(workload: dict[str, Any]) -> dict[str, Any]:
    return workload["spec"]["template"]["spec"]


def containers(workload: dict[str, Any], init: bool = True) -> list[dict[str, Any]]:
    spec = pod_spec(workload)
    return spec["containers"] + (spec.get("initContainers", []) if init else [])


def container(workload: dict[str, Any], name: str) -> dict[str, Any]:
    found = [c for c in pod_spec(workload)["containers"] if c["name"] == name]
    assert len(found) == 1, f"no container {name!r} in {workload['metadata']['name']}"
    return found[0]


def env(c: dict[str, Any]) -> dict[str, Any]:
    """name -> literal value, or the valueFrom dict."""
    return {e["name"]: e.get("value", e.get("valueFrom")) for e in c.get("env", [])}


def workloads(docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [get(docs, kind, name) for kind, name in sorted(WORKLOADS)]


def memory_bytes(quantity: str) -> int:
    units = {"Ki": 2**10, "Mi": 2**20, "Gi": 2**30}
    match = re.fullmatch(r"(\d+)(Ki|Mi|Gi)", quantity)
    assert match, f"memory {quantity!r}: use Ki, Mi or Gi"
    return int(match[1]) * units[match[2]]


def api(docs: list[dict[str, Any]]) -> dict[str, Any]:
    return get(docs, "Deployment", "policypal-api")


def external_secrets(docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [d for d in docs if d["kind"] == "ExternalSecret"]


# --- what is deployed -----------------------------------------------------------------------------------

@overlay_param
def test_overlay_renders_exactly_the_policypal_objects(rendered: dict, overlay: str) -> None:
    found = {(d["kind"], d["metadata"]["name"]) for d in rendered[overlay]}
    assert found == EXPECTED_BY_OVERLAY[overlay]


@overlay_param
def test_everything_lives_in_the_policypal_namespace(rendered: dict, overlay: str) -> None:
    for doc in rendered[overlay]:
        if doc["kind"] != "Namespace":
            assert doc["metadata"].get("namespace") == NAMESPACE, f"{doc['kind']}/{doc['metadata']['name']}"


def test_local_overlay_deploys_the_same_objects_as_aws(rendered: dict) -> None:
    # Apart from the ExternalSecrets: k3d has no AWS identity, so there the Secrets come from a script.
    def shape(docs: list[dict[str, Any]]) -> set[tuple[str, str]]:
        return {(d["kind"], d["metadata"]["name"]) for d in docs}
    assert shape(rendered["local"]) == shape(rendered["aws"]) - EXTERNAL_SECRETS


@overlay_param
def test_no_secret_objects_are_committed(rendered: dict, overlay: str) -> None:
    assert not [d for d in rendered[overlay] if d["kind"] == "Secret"]


# --- sizing and health ----------------------------------------------------------------------------------

@overlay_param
def test_every_container_has_requests_and_a_memory_limit(rendered: dict, overlay: str) -> None:
    # No CPU limit on purpose: on a 2-vCPU node a CPU limit only throttles the API's start-up (embedding
    # every policy) while the CPU is otherwise idle. Memory is the resource that runs out.
    for workload in workloads(rendered[overlay]):
        for c in containers(workload):
            resources = c.get("resources", {})
            requests, limits = resources.get("requests", {}), resources.get("limits", {})
            where = f"{workload['metadata']['name']}/{c['name']}"
            assert {"cpu", "memory"} <= set(requests), f"{where}: requests.cpu and requests.memory"
            assert "memory" in limits, f"{where}: limits.memory"
            assert memory_bytes(requests["memory"]) <= memory_bytes(limits["memory"]), where


@overlay_param
def test_every_container_has_readiness_and_liveness_probes(rendered: dict, overlay: str) -> None:
    for workload in workloads(rendered[overlay]):
        for c in containers(workload, init=False):
            assert "readinessProbe" in c and "livenessProbe" in c, f"{workload['metadata']['name']}/{c['name']}"


@overlay_param
def test_api_start_up_has_a_long_startup_probe(rendered: dict, overlay: str) -> None:
    # Migrations, ingesting every policy and loading the embedding model: measured 34-44 s; allow 5 min+.
    probe = container(api(rendered[overlay]), "api")["startupProbe"]
    assert probe["periodSeconds"] * probe["failureThreshold"] >= 300


@overlay_param
def test_deployments_wait_as_long_as_the_deploy_script(rendered: dict, overlay: str) -> None:
    # Kubernetes marks a Deployment failed after progressDeadlineSeconds (default 600) without progress, and
    # `kubectl rollout status` stops there, whatever deploy-policypal.sh's ROLLOUT_TIMEOUT says. Found on k3d:
    # a slow first pull of the image stopped the deploy at 10 minutes. The API also needs room for its whole
    # startup-probe budget after the pull.
    rollout_timeout = re.search(r'ROLLOUT_TIMEOUT="\$\{ROLLOUT_TIMEOUT:-(\d+)m\}"', DEPLOY_SCRIPT.read_text())
    assert rollout_timeout, "ROLLOUT_TIMEOUT default (in minutes) not found in deploy-policypal.sh"
    for name in ("policypal-api", "policypal-ui"):
        deadline = get(rendered[overlay], "Deployment", name)["spec"].get("progressDeadlineSeconds", 600)
        assert deadline >= int(rollout_timeout[1]) * 60, name
    startup = container(api(rendered[overlay]), "api")["startupProbe"]
    assert api(rendered[overlay])["spec"]["progressDeadlineSeconds"] > startup["periodSeconds"] * startup["failureThreshold"]


@overlay_param
def test_api_health_is_used_for_readiness_never_liveness(rendered: dict, overlay: str) -> None:
    # /health returns 503 while the database is down. As a liveness probe that would restart a healthy API
    # during every database blip, so liveness only checks that the process is listening.
    c = container(api(rendered[overlay]), "api")
    assert c["readinessProbe"]["httpGet"]["path"] == "/health"
    assert c["readinessProbe"]["httpGet"]["port"] == 8000
    assert "tcpSocket" in c["livenessProbe"] and "httpGet" not in c["livenessProbe"]


@overlay_param
def test_api_and_ui_wait_before_stopping(rendered: dict, overlay: str) -> None:
    # preStop sleep: the pod leaves the Service endpoints before the process gets SIGTERM (v1 lesson).
    for name, cname in (("policypal-api", "api"), ("policypal-ui", "ui")):
        c = container(get(rendered[overlay], "Deployment", name), cname)
        command = c["lifecycle"]["preStop"]["exec"]["command"]
        assert command[0] == "sleep" and int(command[1]) >= 5, name


def test_postgres_readiness_asks_postgres_itself(rendered: dict) -> None:
    pg = container(get(rendered["aws"], "StatefulSet", "postgres"), "postgres")
    assert "pg_isready" in " ".join(pg["readinessProbe"]["exec"]["command"])


# --- images ---------------------------------------------------------------------------------------------

@overlay_param
def test_every_image_is_pinned(rendered: dict, overlay: str) -> None:
    for workload in workloads(rendered[overlay]):
        for c in containers(workload):
            image = c["image"]
            assert ("@sha256:" in image or ":" in image.rsplit("/", 1)[-1]) and not image.endswith(":latest"), image


@overlay_param
def test_image_tags_are_quoted_in_the_overlay(overlay: str) -> None:
    # Unquoted, a commit hash made only of digits (or digits and one "e") is a YAML number, and kustomize
    # refuses the file. Found on k3d with a test tag of forty zeros.
    text = (REPO_ROOT / "k8s" / "policypal" / "overlays" / overlay / "kustomization.yaml").read_text()
    tags = re.findall(r"^\s*newTag:\s*(.+?)\s*$", text, re.M)
    assert tags and all(re.fullmatch(r'"[0-9a-f]{40}"', t) for t in tags), tags


@overlay_param
def test_api_and_ui_run_the_same_pinned_policypal_image(rendered: dict, overlay: str) -> None:
    images = {container(api(rendered[overlay]), "api")["image"],
              container(get(rendered[overlay], "Deployment", "policypal-ui"), "ui")["image"]}
    assert len(images) == 1
    assert POLICYPAL_IMAGE.match(images.pop())


@overlay_param
def test_postgres_runs_pgvector_as_in_policypals_compose_file(rendered: dict, overlay: str) -> None:
    for c in containers(get(rendered[overlay], "StatefulSet", "postgres")):
        assert c["image"] == POSTGRES_IMAGE


# --- the API: single writer, extractive mode, persistent uploads ----------------------------------------

@overlay_param
def test_api_is_a_single_replica_replaced_with_recreate(rendered: dict, overlay: str) -> None:
    # One process owns the upload lock and the ReadWriteOnce uploads volume: never two pods at once.
    spec = api(rendered[overlay])["spec"]
    assert spec["replicas"] == 1
    assert spec["strategy"]["type"] == "Recreate"


@overlay_param
def test_api_answers_without_an_llm(rendered: dict, overlay: str) -> None:
    assert env(container(api(rendered[overlay]), "api"))["LLM_PROVIDER"] == "extractive"


@overlay_param
def test_uploads_are_kept_on_the_uploads_volume(rendered: dict, overlay: str) -> None:
    # Ingestion deletes rows whose upload file is missing, so uploads must persist like the database.
    docs = rendered[overlay]
    deployment = api(docs)
    c = container(deployment, "api")
    assert env(c)["UPLOAD_DIR"] == "/app/uploads"
    volumes = {v["name"]: v for v in pod_spec(deployment)["volumes"]}
    mounts = [m for m in c["volumeMounts"] if m["mountPath"] == "/app/uploads"]
    assert len(mounts) == 1
    claim = volumes[mounts[0]["name"]]["persistentVolumeClaim"]["claimName"]
    assert claim == "policypal-uploads"
    assert get(docs, "PersistentVolumeClaim", claim)["spec"]["accessModes"] == ["ReadWriteOnce"]


@overlay_param
def test_postgres_keeps_its_data_on_a_5gi_claim(rendered: dict, overlay: str) -> None:
    sts = get(rendered[overlay], "StatefulSet", "postgres")
    templates = sts["spec"]["volumeClaimTemplates"]
    assert len(templates) == 1
    assert templates[0]["spec"]["resources"]["requests"]["storage"] == "5Gi"
    pg = container(sts, "postgres")
    mount = [m for m in pg["volumeMounts"] if m["name"] == templates[0]["metadata"]["name"]][0]["mountPath"]
    # A subdirectory: initdb wants an empty directory it owns, not the volume's root.
    assert env(pg)["PGDATA"].startswith(mount.rstrip("/") + "/")


# --- secrets: referenced, never written down ------------------------------------------------------------

@overlay_param
def test_secrets_come_only_from_the_expected_secret_keys(rendered: dict, overlay: str) -> None:
    # The Secrets are created outside Git (scripts/policypal-secrets.sh now, External Secrets Operator from
    # C5); the manifests only reference them. test_policypal_scripts.py checks the script against these refs.
    docs = rendered[overlay]
    expected = {
        ("policypal-api", "api", "POSTGRES_PASSWORD"): ("policypal-db", "POSTGRES_PASSWORD"),
        ("policypal-api", "api", "ADMIN_TOKEN"): ("policypal-admin", "ADMIN_TOKEN"),
        ("postgres", "postgres", "POSTGRES_PASSWORD"): ("policypal-db", "POSTGRES_PASSWORD"),
    }
    found = {}
    for workload in workloads(docs):
        for c in containers(workload):
            assert "envFrom" not in c, "reference single keys, so each container gets only what it needs"
            from_secrets = set()
            for name, value in env(c).items():
                if isinstance(value, dict) and "secretKeyRef" in value:
                    ref = value["secretKeyRef"]
                    found[(workload["metadata"]["name"], c["name"], name)] = (ref["name"], ref["key"])
                    from_secrets.add(name)
                elif re.search(r"PASSWORD|TOKEN|SECRET|DATABASE_URL", name):
                    # The one other form: a value built only from Secret variables defined above it, $(NAME).
                    built_from = set(re.findall(r"\$\((\w+)\)", value)) if isinstance(value, str) else set()
                    assert built_from and built_from <= from_secrets, \
                        f"{name} in {c['name']} is a literal value: it must come from a Secret"
    assert found == expected


@overlay_param
def test_api_builds_the_database_url_from_the_postgres_password(rendered: dict, overlay: str) -> None:
    # The Secret holds only the password and the URL is written once, here, for both overlays. Kubernetes
    # expands $(POSTGRES_PASSWORD) only if it is defined earlier in the same container; otherwise the API gets
    # the text "$(POSTGRES_PASSWORD)" as its password. User and database are the ones Postgres creates.
    docs = rendered[overlay]
    api = container(get(docs, "Deployment", "policypal-api"), "api")
    pg = env(container(get(docs, "StatefulSet", "postgres"), "postgres"))
    names = [e["name"] for e in api["env"]]
    assert names.index("POSTGRES_PASSWORD") < names.index("DATABASE_URL")
    assert env(api)["DATABASE_URL"] == (
        f"postgresql+psycopg://{pg['POSTGRES_USER']}:$(POSTGRES_PASSWORD)@postgres:5432/{pg['POSTGRES_DB']}")


# --- secrets on AWS: External Secrets Operator, from SSM Parameter Store --------------------------------

def test_aws_secrets_are_made_by_external_secrets_with_exactly_the_keys_the_pods_use(rendered: dict) -> None:
    # No template: each parameter's value lands in the Secret as it is, under its secretKey.
    docs = rendered["aws"]
    for es in external_secrets(docs):
        assert "template" not in es["spec"]["target"], es["metadata"]["name"]
    made = {(es["spec"]["target"]["name"], d["secretKey"]) for es in external_secrets(docs) for d in es["spec"]["data"]}
    assert made == secret_refs(docs)


def test_each_catalogued_secret_is_read_by_one_external_secret_in_its_namespace(rendered: dict) -> None:
    # terraform/persistent/secret-catalog.yaml lists every parameter Terraform creates and who reads it. One
    # with a namespace is read there by exactly one ExternalSecret; one without is read by the node itself,
    # never through ESO. Nothing else is read. Every parameter is named: dataFrom would read whatever matches.
    reads: dict[str, list[str]] = {}
    for es in external_secrets(rendered["aws"]):
        assert "dataFrom" not in es["spec"], es["metadata"]["name"]
        for d in es["spec"]["data"]:
            reads.setdefault(d["remoteRef"]["key"], []).append(es["metadata"]["namespace"])
    catalog = secret_catalog()
    expected = {f"/policypal/aws/{name}": [entry["namespace"]]
                for name, entry in catalog.items() if entry.get("namespace")}
    assert catalog and reads == expected


def test_external_secrets_use_the_cluster_store_and_refresh_hourly(rendered: dict) -> None:
    store = yaml.safe_load(SECRET_STORE.read_text())
    found = external_secrets(rendered["aws"])
    assert found
    for es in found:
        name = es["metadata"]["name"]
        assert es["apiVersion"] == "external-secrets.io/v1", name
        assert es["spec"]["secretStoreRef"] == {"kind": "ClusterSecretStore", "name": store["metadata"]["name"]}, name
        assert es["spec"]["refreshInterval"] == "1h", name
        # Owner: ESO owns the Secret, so deleting the ExternalSecret removes it (and a deleted Secret comes back).
        assert es["spec"]["target"].get("creationPolicy", "Owner") == "Owner", name


# --- exposure: only the UI, through Traefik -------------------------------------------------------------

@overlay_param
def test_every_service_is_cluster_internal(rendered: dict, overlay: str) -> None:
    # k3s would publish a LoadBalancer service on the node's ports (ServiceLB), and a NodePort on 30000+.
    for svc in (d for d in rendered[overlay] if d["kind"] == "Service"):
        assert svc["spec"].get("type", "ClusterIP") == "ClusterIP", svc["metadata"]["name"]


@overlay_param
def test_ingress_routes_only_to_the_ui(rendered: dict, overlay: str) -> None:
    # The API (Swagger at /docs, admin endpoints) stays reachable only inside the cluster.
    ingress = get(rendered[overlay], "Ingress", "policypal")
    assert ingress["spec"]["ingressClassName"] == "traefik"
    assert "defaultBackend" not in ingress["spec"]
    backends = [p["backend"]["service"] for rule in ingress["spec"]["rules"] for p in rule["http"]["paths"]]
    assert backends and all(b == {"name": "policypal-ui", "port": {"number": 8501}} for b in backends)


def test_aws_ingress_answers_on_any_host(rendered: dict) -> None:
    # The node's public IP changes every session and there is no DNS name: no host rule.
    rules = get(rendered["aws"], "Ingress", "policypal")["spec"]["rules"]
    assert all("host" not in r for r in rules)


def test_local_ingress_uses_its_own_host_name(rendered: dict) -> None:
    # k3d's load balancer on localhost:8080 also serves the v1 app (devops-app.localhost).
    rules = get(rendered["local"], "Ingress", "policypal")["spec"]["rules"]
    assert [r.get("host") for r in rules] == ["policypal.localhost"]


@overlay_param
def test_services_point_at_the_ports_the_apps_listen_on(rendered: dict, overlay: str) -> None:
    docs = rendered[overlay]
    for name, port in (("policypal-api", 8000), ("policypal-ui", 8501), ("postgres", 5432)):
        ports = get(docs, "Service", name)["spec"]["ports"]
        assert [(p["port"], p.get("targetPort", p["port"])) for p in ports] == [(port, port)], name
    assert get(docs, "Service", "postgres")["spec"].get("clusterIP") == "None", "headless, for the StatefulSet"


@overlay_param
def test_ui_reaches_the_api_through_its_service(rendered: dict, overlay: str) -> None:
    ui = container(get(rendered[overlay], "Deployment", "policypal-ui"), "ui")
    assert env(ui)["API_URL"] == "http://policypal-api:8000"
    assert ui["command"][:2] == ["streamlit", "run"]


# --- pod security ---------------------------------------------------------------------------------------

@overlay_param
def test_policypal_namespace_enforces_pod_security_baseline(rendered: dict, overlay: str) -> None:
    # The node's IMDSv2 hop limit keeps pods in the pod network away from the node role's credentials, but a
    # pod on the host network gets them (shown on the node in C5). The baseline Pod Security level rejects
    # host-network pods (and privileged ones, hostPath, hostPort, host PID/IPC) in this namespace.
    labels = get(rendered[overlay], "Namespace", NAMESPACE)["metadata"].get("labels", {})
    assert labels.get("pod-security.kubernetes.io/enforce") == "baseline"


@overlay_param
def test_every_workload_meets_pod_security_baseline(rendered: dict, overlay: str) -> None:
    # Otherwise the namespace label would reject PolicyPal's own pods.
    for w in workloads(rendered[overlay]):
        name, spec = w["metadata"]["name"], pod_spec(w)
        for field in ("hostNetwork", "hostPID", "hostIPC"):
            assert not spec.get(field), f"{name}: {field}"
        assert not [v for v in spec.get("volumes", []) if "hostPath" in v], f"{name}: hostPath volume"
        for c in containers(w):
            assert not c.get("securityContext", {}).get("privileged"), f"{name}/{c['name']}: privileged"
            assert not [p for p in c.get("ports", []) if p.get("hostPort")], f"{name}/{c['name']}: hostPort"


@overlay_param
def test_containers_run_unprivileged(rendered: dict, overlay: str) -> None:
    for workload in workloads(rendered[overlay]):
        spec = pod_spec(workload)
        name = workload["metadata"]["name"]
        assert spec.get("securityContext", {}).get("runAsNonRoot") is True, name
        # No pod here talks to the Kubernetes API, so none gets a service account token.
        assert spec.get("automountServiceAccountToken") is False, name
        for c in containers(workload):
            sc = c.get("securityContext", {})
            assert sc.get("allowPrivilegeEscalation") is False, f"{name}/{c['name']}"
            assert sc.get("capabilities", {}).get("drop") == ["ALL"], f"{name}/{c['name']}"
