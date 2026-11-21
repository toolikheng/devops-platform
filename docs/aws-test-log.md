# AWS test log

Measured results of live runs against the AWS account (ap-southeast-1). Offline checks (`make tf-check`, `make test-infra`) run on every push; this file records what only a real apply can show. Every run ends with a destroy and a clean `make cost-check`.

Costs are estimates from the published hourly rates, before tax. The Billing console is the source of truth.

## Ephemeral stack: network and compute (Phase C2)

### Steps

```bash
aws login --profile devops
make cost-check                                   # clean before starting
cd terraform/persistent && terraform plan -out=tfplan && terraform apply tfplan && cd -   # node role, profile, owner-cidr parameter
make cloud-up                                     # stores this machine's IP as a /32, then applies (on-demand)
#   Spot: make cloud-up CLOUD_ARGS='-var use_spot=true'
aws ssm start-session --target "$(cd terraform/envs/aws && terraform output -raw instance_id)"
```

Inside the Session Manager shell:

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://169.254.169.254/latest/meta-data/instance-id     # 401: IMDSv1 refused
TOKEN=$(curl -s -X PUT http://169.254.169.254/latest/api/token -H 'X-aws-ec2-metadata-token-ttl-seconds: 60')
curl -s -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/instance-id   # the instance ID
getent hosts ssm.ap-southeast-1.amazonaws.com ghcr.io github.com                                     # DNS works
```

Then, from the laptop: a second `terraform plan` (must show no changes), `aws ec2 describe-instance-credit-specifications`, port probes against the public IP, and finally `make cloud-down`.

### Expected results

| Check | Expected |
|---|---|
| Apply | 9 resources added (VPC, subnet, internet gateway, route table, route, association, security group, key pair, instance) |
| SSM agent | `Online` in `aws ssm describe-instance-information`, with only the node role's 11 SSM-agent actions |
| IMDS | v1 (no token) returns 401; v2 returns the instance ID |
| DNS and outbound | names resolve through the VPC resolver; HTTPS and HTTP out work; other ports out are blocked |
| Inbound from the owner's IP | port 80 allowed; 22 and 6443 dropped |
| Credits | `standard` |
| Root volume | gp3, 20 GB, encrypted, deleted on termination |
| Second plan | no changes |
| Destroy | 9 destroyed; `make cost-check` exit 0 |

### Result log

| Date (MYT) | Run | Result | Notes |
|---|---|---|---|
| 2026-11-20 23:35 | Persistent stack additions | Pass | 4 added (node role, inline `ssm-agent` policy, instance profile, `/devops-platform/owner-cidr`). Second plan: no changes. The role has no managed policies attached. `make owner-ip` then set the parameter to the owner's /32 (version 2). |
| 2026-11-20 23:40–23:59 | t3.micro on-demand | Pass | 9 added in 23 s. SSM agent `Online` about a minute after launch with the minimal policy, so the managed policy was not needed. IMDSv1 401, IMDSv2 returned the instance ID, the role name was visible through IMDS. DNS through the VPC resolver resolved SSM, ghcr.io, github.com and archive.ubuntu.com. Outbound: ghcr.io:443 answered (401, as expected without a login), archive.ubuntu.com:80 returned 200, github.com:22 was blocked. From the owner's IP: port 80 "connection refused" (allowed, nothing listening yet), ports 22 and 6443 timed out although sshd was listening. Credits `standard`; root volume gp3 20 GB, encrypted with the AWS-managed key, deleted on termination; shutdown behaviour `terminate`; tags on the instance and the volume. Second plan: no changes. Destroy: 9 destroyed, cost-check clean. About USD 0.007. |
| 2026-11-21 00:03–00:11 | t3.micro Spot | **Fail** | The apply stopped after launching the instance: `UnsupportedOperation: Modifying 'instanceInitiatedShutdownBehavior' is not supported for spot instances`. The AWS provider sets that attribute with a second API call after launch, and AWS refuses it for Spot. The instance was in Terraform state (marked tainted), so `terraform destroy` removed everything; cost-check clean. AWS already reported `terminate` for the one-time Spot instance. Fixed by setting the attribute for on-demand nodes only (`2ee3cee`), with a test that launches a fresh Spot instance and checks it is left alone. About USD 0.002. |
| 2026-11-21 00:27–00:36 | t3.micro Spot, after the fix | Pass | 9 added. `InstanceLifecycle` spot; one-time request with interruption behaviour `terminate` and a price cap of USD 0.0132 (the on-demand price, because no max price is set). Shutdown behaviour `terminate`, set by AWS. SSM, IMDS, DNS and outbound results as in the on-demand run. Second plan: no changes. Destroy: 9 destroyed, Spot request cancelled, cost-check clean. About USD 0.002. |

Total for these runs: about USD 0.011 before tax.

## Ansible and k3s on the node (Phase C3)

### Steps

```bash
aws login --profile devops
make cost-check                                   # clean before starting
make cloud-up                                     # t3.small Spot by default
make cloud-config ANSIBLE_ARGS='--check --diff'   # optional preview (see below for a fresh node)
make cloud-config                                 # common, node_exporter, k3s; copies the kubeconfig
make cloud-config                                 # second run: must report changed=0
make cloud-tunnel                                 # in a second terminal; Ctrl-C closes it
KUBECONFIG=~/.kube/devops-aws.yaml kubectl get nodes
make cloud-down
make cloud-rebuild                                # from nothing: cloud-up, then cloud-config
make cloud-down
```

On the node, through `ansible -i ansible/inventory/aws.yml aws -m shell -a '...'` (the same SSH-over-Session-Manager path): `k3s secrets-encrypt status`, `stat` on the k3s files, `sshd -T`, `systemctl` for ssh and node_exporter, `free -m`. From the laptop: connection attempts to the public IP on 22, 6443, 9100 and 10250, and `curl` on 80.

### Expected results

| Check | Expected |
|---|---|
| First `make cloud-config` | `failed=0`; k3s installed and the node Ready inside the run |
| Second run | `changed=0` |
| `--check --diff` on the configured node | `changed=0`, no diff |
| On the node | k3s v1.36.6+k3s1; secrets encryption enabled; `k3s.yaml` and `config.yaml` mode 600, owner root; `permitrootlogin no`, `passwordauthentication no`; node_exporter active under systemd |
| Laptop | `~/.kube/devops-aws.yaml` mode 600 with server `https://127.0.0.1:16443`; `kubectl get nodes` Ready through the tunnel with TLS verified |
| From the laptop to the public IP | 22, 6443, 9100, 10250 no connection; 80 answered by Traefik (404, nothing deployed yet) |
| Rebuild | `make cloud-rebuild` from an empty stack gives a Ready node with no manual steps |
| End | destroy, `make cost-check` exit 0, no active Session Manager sessions |

### Result log

| Date (MYT) | Run | Result | Notes |
|---|---|---|---|
| 2026-11-21 15:29 | Apply, t3.small Spot | Pass | 9 added in 21 s. Spot price USD 0.0097/h in ap-southeast-1a. SSM agent Online right after the apply finished. |
| 2026-11-21 15:29 | `--check --diff` on the fresh node | **Fail**, then fixed | SSH through Session Manager worked on the first try (facts gathered). In check mode Ansible skipped both wait tasks, and apt found no packages: a fresh Ubuntu cloud image has empty package lists, and check mode only pretended to refresh them. Fixed test-first: the two waits and the package-list refresh run in check mode too (`2f9e518`). The rerun went as far as the node_exporter download into a directory check mode had only pretended to create. That is how check mode works on a fresh node, so it is used on configured nodes, where it shows drift. |
| 2026-11-21 15:31 | First real run | Pass | 103 s, `changed=19`, `failed=0`, including the k3s install, a restart for its config and the wait for Ready. Standard CPU credits did not noticeably slow the install. |
| 2026-11-21 15:33 | Second run | Pass | `changed=0` (126 s). `--check --diff` afterwards: `changed=0`, no diff (43 s). |
| 2026-11-21 15:36 | Checks on the node | Pass | k3s v1.36.6+k3s1; "Encryption Status: Enabled"; `k3s.yaml` and `config.yaml` 600 root; `sshd -T`: permitrootlogin no, passwordauthentication no, pubkeyauthentication yes. node_exporter active and enabled under systemd, serving 724 `node_` metrics: the role's systemd path ran for the first time. Memory with k3s idle: 1112 of 1905 MiB used, 793 MiB available. `ssh.socket` and `ssh.service` both enabled and active (see the runbook). |
| 2026-11-21 15:37 | Tunnel and kubectl | Pass | Tunnel up in 10 s. `kubectl get nodes`: Ready, v1.36.6+k3s1 (kubectl v1.37.0), TLS verified. CoreDNS, Traefik, ServiceLB, local-path and metrics-server Running. From the laptop: 22, 6443, 9100 and 10250 no connection; 80 returned Traefik's 404. |
| 2026-11-21 15:41 | Destroy | Pass | 9 destroyed; cost-check clean. The tunnel's session stayed listed as active because I had stopped the plugin with SIGTERM instead of Ctrl-C; it was terminated with `aws ssm terminate-session` at the end of the session. About USD 0.004. |
| 2026-11-21 16:38 | Rebuild from nothing | Pass | Apply, then `make cloud-config` straight away with no manual waiting: a configured, Ready node 175 s after the apply started (`changed=20`: the package-list refresh is the extra change). Second run `changed=0` (136 s). With Ansible's per-task timing every task took 6–13 s, all of it round trips through Session Manager. The same run with pipelining took 30 s, still `changed=0`; pipelining is now on for the aws group. Tunnel, `kubectl get nodes` Ready. Closing the tunnel with SIGINT (Ctrl-C) ended its session on the AWS side. |
| 2026-11-21 16:51 | Destroy | Pass | 9 destroyed; cost-check clean; no active Session Manager sessions. About USD 0.004. |

Total for these runs: about USD 0.008 before tax.

Record each run here, including failures and what was changed.
