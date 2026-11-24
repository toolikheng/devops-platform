# ADR 022: One add-on script installs and renders every Helm chart on the cluster

## Status

Accepted

## Context

Phase C5 installed External Secrets Operator (ESO) with two scripts: `install-eso.sh` (Helm install, then the `ClusterSecretStore`) and `render-eso.sh` (the same chart rendered for kubeconform and the pytest rules), both reading the pin from `k8s/platform/external-secrets/chart.env`. Both scripts had the release name, the namespace and ESO's variable names written in, and `ci/k8s-checks.sh` had a block written for ESO alone.

Phase C7 brings a second chart to the AWS node: kube-prometheus-stack. The homelab already installed it a different way: `make monitoring-up` ran `helm repo add` and took the version from a Makefile variable, and nothing ever rendered it or checked it with kubeconform. `ci/rules-test.sh` claimed in a comment that it tested the alert rules with "the same Prometheus as kube-prometheus-stack 92.16.0".

Adding the second chart the ESO way would have meant two more copies of the scripts and another hand-written block in the checks; adding it the homelab way would have left it unchecked.

## Decision

- **An add-on is a folder `k8s/platform/NAME`**: `chart.env` (`CHART`, `CHART_REPO`, `CHART_VERSION`, `NAMESPACE`, and `MANIFESTS`, the files applied after the chart because they need its CRDs) and **one values file per environment it runs in** (`values-local.yaml` for k3d, `values-aws.yaml` for the AWS node). The Helm release is called NAME.
- **`scripts/addon.sh install|render NAME ENV`** is the only thing that runs Helm. `install` is `helm upgrade --install` with `--repo`, `--version` and `--wait`, then the manifests. `render` is `helm template` in a pinned Helm image against the node's Kubernetes version (`ci/versions.env`), then the manifests. An environment without a values file is refused, so ESO (values for AWS only) can't be installed on k3d by mistake.
- **Callers:** `make cloud-addons` installs ESO for aws; `make monitoring-up` installs kube-prometheus-stack for local, then applies what `monitoring/` monitors (ServiceMonitor, rules, dashboard); `ci/k8s-checks.sh` renders and validates every values file it finds.
- **The alert rules are tested with the chart's own Prometheus image.** A test renders the chart and requires `ci/rules-test.sh` to use the same image.

## Consequences

- A new chart is a folder with a `chart.env` and a values file, plus one line in the Make target that installs it. The checks pick it up from the folder.
- kube-prometheus-stack is now rendered and checked with kubeconform (127 objects, all valid). The image test found that the rules had been tested with Prometheus v3.15.0 while the chart runs v3.15.1 (`-distroless`), so the comment had been wrong; `rules-test.sh` now uses `quay.io/prometheus/prometheus:v3.15.1-distroless`, which includes promtool, and the rules pass with it.
- `monitoring-up` no longer adds a Helm repository to the machine (`--repo` instead), and now waits for the pods (`--wait`, up to 10 minutes). The release name and namespace didn't change, so an existing homelab install upgrades in place.
- Every add-on has the same shape, so one shared limit applies to all: a chart whose CRDs are rendered as templates (ESO) has them skipped by kubeconform, which has no schema for the CRD kind itself.
- The 25 ESO CRDs are still installed by the chart, and kube-prometheus-stack still installs its CRDs only on the first install: bump its version deliberately (noted in its `chart.env`).
