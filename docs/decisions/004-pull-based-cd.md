# ADR 004: Manual CD to a throwaway cluster

## Status

Accepted. Continuous deployment into a long-lived cluster is deferred.

## Context

Deploying from GitHub Actions to a cluster on a laptop needs a path into that cluster. A GitHub-hosted runner cannot reach `localhost` on the laptop, and opening the cluster to the internet would be unsafe.

The options considered:

1. **Push-based.** A GitHub-hosted job holds a kubeconfig and calls the cluster API. This requires exposing the API or a tunnel.
2. **Pull-based.** A runner or GitOps agent inside the cluster pulls changes and applies them from inside the network. Nothing is exposed, but it adds a component to build and secure.
3. **Throwaway cluster.** The workflow creates its own k3d cluster on the GitHub-hosted runner, deploys there, tests, and discards it.

## Decision

CD is a manual `workflow_dispatch` that uses option 3. It builds the image, creates a k3d cluster on the runner, applies `k8s/overlays/dev`, waits for the rollout, and smoke-tests `/healthz` and `/readyz`. The local cluster is deployed with `make deploy`.

## Consequences

- Every CD run proves that the current image and manifests deploy and serve traffic on a clean k3s cluster, without exposing anything.
- It does not deploy to a long-lived environment. A pull-based agent (option 2) remains the path for that.
- A production approval gate would need GitHub Environments with required reviewers.
