# ADR 004: Manual CD, with a pull-based runner as the target

## Status

Partially implemented. The manual GitHub-hosted workflow is on master. The pull-based self-hosted runner is on the `self-hosted-runner` branch and is not working yet.

## Context

Deploying from GitHub Actions to a cluster on a laptop needs a path into that cluster. A GitHub-hosted runner cannot reach `localhost` on the laptop, and opening the cluster to the internet would be unsafe.

Two options:

1. **Push-based.** A GitHub-hosted job holds a kubeconfig and calls the cluster API. This requires exposing the API or a tunnel.
2. **Pull-based.** A runner inside the cluster polls GitHub for jobs and applies manifests from inside the network. Nothing is exposed.

## Decision

- **On master:** CD is a manual `workflow_dispatch`. It runs on a GitHub-hosted runner, creates an ephemeral k3d cluster, deploys `k8s/overlays/dev`, and runs a smoke test. It proves the manifests and image work end to end, but it does not deploy to a long-lived cluster.
- **On `self-hosted-runner`:** a runner Deployment in the `devops` namespace registers with GitHub and picks up jobs. It is the pull-based option, but its pod cannot reach the Kubernetes API server, so the deploy job fails.

## Consequences

- Master stays runnable on GitHub-hosted runners, so CI and CD are not blocked by the laptop cluster.
- The pull-based path is still open. Finishing it means fixing the pod-to-API-server connection (the NetworkPolicy was removed while debugging) and re-running the CD job.
- Any production approval gate needs GitHub Environments with required reviewers. The earlier echo-only approval job was removed because it did not gate anything.
