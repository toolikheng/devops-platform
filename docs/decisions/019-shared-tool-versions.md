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

## Python tools (2026-11-24)

The same question came up for Python. The Makefile has two near-identical venv recipes (`app/.venv` from `app/pyproject.toml`, `tests/.venv` from `tests/requirements.txt`), and CI installs the same tools its own way. A review before Phase C7 proposed that each check script build its own venv from one pinned requirements file, so that `make` and CI run the same command.

Kept as it is:

- The two recipes are four lines each and changed twice since Phase C0. A Make macro that merges them would be harder to read than the copy, and the larger change isn't worth it at that rate.
- One exception: CI pins `ruff` and `mypy` in the lint job (checked by `tests/test_workflows.py`). Unpinned, a release with new rules could fail CI overnight with no change in the repo. Each is written once, in the workflow, so they don't go in `ci/versions.env`.
- The app's `[dev]` extra keeps its `>=` ranges, so a local venv can be newer or older than CI. Revisit if that difference ever hides a failure.
