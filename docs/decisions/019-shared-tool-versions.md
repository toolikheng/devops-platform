# ADR 019: Tool versions that several scripts use are pinned once, in ci/versions.env

## Status

Accepted

## Context

Every tool here runs at a pinned version, mostly as a Docker image tag written in the script that uses it. Two versions were written more than once:

- The node's **Kubernetes version** (1.36.6, from k3s): in `scripts/render-eso.sh` (Helm renders the ESO chart for it), `ci/k8s-checks.sh` (kubeconform validates against it) and a test literal that the drift test didn't read. The drift test found the versions by matching a regex against the bash scripts.
- **Trivy** (0.75.0): in `ci/scan.sh`, `ci/terraform-checks.sh` and two `trivy-action` inputs in `.github/workflows/ci.yml`, with no test.

A bump meant finding every copy, and a missed copy passed silently. The ESO chart already had a file of this kind (`k8s/platform/external-secrets/chart.env`, ADR 016).

## Decision

- **`ci/versions.env`** holds `KUBERNETES_VERSION` and `TRIVY_VERSION` as `KEY=value` lines. The four scripts `source` it; the tests read it through one helper (`versions()` in `tests/conftest.py`, the same parser as `chart_env()`).
- **What can't source it is checked against it** (`tests/test_versions.py`): the k3s role's version, and the `version` input of every `trivy-action` step in CI.
- **No script or the Makefile may write a pinned version itself.** A test looks for each pinned value in `scripts/*.sh`, `ci/*.sh` and the Makefile.
- **Only shared pins go there.** A version used by one script (kustomize, kubeconform, Helm, gitleaks, tflint) stays next to its `docker run`: one copy can't drift.

## Consequences

- A Kubernetes or Trivy bump is one edit in `ci/versions.env`, plus the k3s role or the CI inputs, which a test then demands.
- `ci/scan.sh` and `ci/terraform-checks.sh` now run in the tests too, with fake `docker` and `terraform`, to show they pass the pinned Trivy image.
- The scripts need ShellCheck's `-x` to follow the `source` (already on since C5).
- The copy rule matches the version string anywhere in a script, comments included, so a comment must name the file, not the number.
