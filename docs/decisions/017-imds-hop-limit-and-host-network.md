# ADR 017: The node keeps IMDSv2 hop limit 1; only External Secrets Operator's controller runs on the host network, and the policypal namespace forbids host networking

## Status

Accepted

## Context

External Secrets Operator ([ADR 016](016-parameter-store-and-external-secrets.md)) needs AWS credentials to read Parameter Store. On EC2 the node's instance role hands out short-lived credentials through the instance metadata service (IMDS). The node requires IMDSv2 with a hop limit of 1 (`terraform/modules/compute`, checked by its tests): the token answer leaves IMDS with an IP time-to-live of 1, so a process on the host receives it, but the host can't forward it into a pod's network namespace. Pods in the pod network therefore get no token and no credentials. That is what keeps PolicyPal and Postgres away from the node's AWS access.

| Option | What it costs |
|---|---|
| Access keys in a Kubernetes Secret | long-lived keys in the cluster: against the project's rules |
| Hop limit 2 | every pod on the node, PolicyPal included, gets the node role's credentials |
| Per-pod IAM (IRSA, EKS Pod Identity) | EKS only, or running my own OIDC issuer for k3s: far more than one node needs |
| **Hop limit 1, and the ESO controller on the host network** | everything on the host network shares the node role |

## Decision

- **Hop limit 1 stays.** Only the ESO **controller** runs with `hostNetwork: true` (`k8s/platform/external-secrets/values.yaml`). Its webhook and cert-controller need no AWS access and stay in the pod network; the webhook couldn't run on the host anyway, because it listens on 10250, the kubelet's port.
- **The controller's host ports are written down and checked**: metrics on 8080, health on 8082 with a liveness probe. Tests check that neither is a port the node already uses (the list comes from `ss -ltnp` on the node), and the security group still lets in only port 80.
- **The controller is replaced, not rolled** (`strategy: Recreate`). A rolling update starts the new pod next to the old one, and on one node both need the same host ports.
- **The `policypal` namespace enforces the Pod Security "baseline" level** (`pod-security.kubernetes.io/enforce: baseline`). Kubernetes then rejects any pod there that asks for the host network, host PID or IPC, hostPath volumes, host ports or privileged mode, so nothing in PolicyPal's namespace can reach the node's credentials. ESO runs in its own namespace, which doesn't have the label. A test checks that every PolicyPal workload meets baseline, so the label can't reject PolicyPal's own pods.

## Consequences

- Measured on AWS: the store validated with the node's role through IMDSv2, from the host network. From inside the PolicyPal API pod, an IMDSv2 token request timed out while ESO kept syncing; the same image started with `hostNetwork: true` got a token at once. The node role reading `/devops-platform/owner-cidr` got AccessDenied.
- Measured on k3d with the label: PolicyPal deployed and passed its smoke test; a host-network pod was refused in `policypal` ("violates PodSecurity "baseline:latest": host namespaces (hostNetwork=true)") and admitted in `default`.
- **Everything on the host network shares one role**: the SSM agent, k3s, and now the ESO controller (C7's backup job will join them). The role is small (the SSM agent's actions and `GetParameter` on `/policypal/aws/*`), but anyone who can create a host-network pod in another namespace, or run a process on the node, can use it. On EKS each pod would get its own role.
- The label follows the cluster's version of "baseline" (`baseline:latest`), so a Kubernetes upgrade could tighten it. PolicyPal's pods already run non-root with no capabilities, well inside it.
- The first `helm upgrade` on the node (to add the liveness probe) failed with "context deadline exceeded": the new controller pod stayed Pending ("didn't have free ports for the requested pod ports") until the strategy became `Recreate`. The controller is briefly gone during an upgrade; the Secrets it made stay in the cluster.
