# ADR 021: GitHub Actions deploys PolicyPal through SSM Run Command, with the YAML CI checked

## Status

Accepted

## Context

`make cloud-deploy` deploys from my laptop through a Session Manager tunnel to the Kubernetes API. From C6 a GitHub workflow should deploy a chosen PolicyPal commit. A GitHub-hosted runner can't reach the cluster: the Kubernetes API is not public, and the security group lets only my IP in on port 80 ([ADR 015](015-only-the-ui-is-exposed.md)). v1 decided the same question for the laptop cluster with a throwaway cluster in CI ([ADR 004](004-pull-based-cd.md)).

| Option | Problem |
|---|---|
| Self-hosted runner on the node | a runner process with a GitHub registration on the node, running workflow code as a privileged user; one more service to install and update every session |
| Runner tunnels to the API (Session Manager port forwarding) and runs kubectl | the cluster-admin kubeconfig has to reach GitHub; the runner needs the plugin and `ssm:StartSession` |
| GitOps agent in the cluster (Argo CD, Flux) | another component on a 4 GiB node that is already 61% full, and nothing new to learn about AWS |
| **SSM Run Command** | the SSM agent is already on the node (Ansible and the tunnel use it), it polls AWS outbound, and IAM can limit who may send what to which instance |

## Decision

- **`deploy.yml` sends one shell script to the node with `ssm:SendCommand`** (`ci/deploy-over-ssm.sh`). The node is found by its tags (`Project=devops-platform`, `Stack=ephemeral`, running), and exactly one must match. On the node the script fetches this repo at the workflow's commit and runs `scripts/node-deploy.sh`, which calls the same `deploy-policypal.sh` as the laptop: apply, wait for the Secrets and the rollout, smoke test, roll back on failure. kubectl uses k3s's own kubeconfig, and the smoke test reaches the UI through the ingress at the node's private address (from IMDSv2).
- **CI renders, the node applies those bytes.** The node has no Docker, so it can't run the pinned kustomize image the checks use. The `render` job, which has no AWS access, renders the aws overlay with the chosen commit as the image tag (`scripts/render-k8s.sh aws <commit>`, a temporary copy) and validates that file with kubeconform (`ci/deploy-render.sh`). The file then travels inside the command, compressed and base64-encoded: 8,812 bytes of YAML, 2,352 encoded.
- **A size ceiling instead of a guess.** AWS documents no quota for Run Command parameters; the nearest is 64 KB per SSM document. The script refuses a payload over 32 KB before any AWS call. If the overlay ever grows past it, the fallback is to render on the node with `kubectl kustomize`, whose kustomize version differs from the pinned one.
- **The commit is an input**, checked to be a full 40-character hash before anything renders. The overlay's pinned commit stays the default for `make cloud-deploy`.
- **The deploy role** (`gha-deploy`, [ADR 020](020-one-oidc-role-per-ci-job.md)) may send only the `AWS-RunShellScript` document, only to instances tagged as the ephemeral stack, and read the result.

## Consequences

- Nothing connects to the node, and no cluster credential ever leaves it or enters GitHub.
- Whoever may send this command is root on the node: the SSM agent runs commands as root. The role is limited to one document and the tagged node, and only jobs in `aws-deploy` (master only) can assume it.
- The node needs GitHub on port 443 to fetch the repo. The security group already allows it, and the repo is public, so no token is needed.
- The job sees the node's output only after the command ends, and Run Command returns at most its first 24,000 characters of output and 8,000 of errors.
- Rollback reverts the API and UI images only. Database migrations go forward only, and while a bad API version is being tried the API is down (`Recreate`; the UI keeps serving).
- Measured on k3d before AWS: a broken commit (`000…0`) deployed from a rendered file went to `ImagePullBackOff`, both Deployments rolled back to revision 1's image, the script exited 1 (145 s with a 2-minute rollout timeout), and the smoke test passed 6 of 6 afterwards. A render with the pinned commit given as the tag was byte-for-byte the plain render.
- Not yet measured on AWS: a deploy and a rollback through `deploy.yml` (C6 live sessions).
