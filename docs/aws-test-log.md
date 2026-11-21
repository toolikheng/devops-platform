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

| 2026-11-21 18:22 | Rebuild after the code review | Pass | Checks the review changes on a real node: the k3s role without its check-mode guards, and the kubeconfig steps moved from the role into the playbook. Apply 22 s, then `make cloud-config` straight away: configured 147 s after the apply started, `changed=20`, `failed=0`; the kubeconfig was saved 600 pointing at port 16443, with the existing `~/.kube` left alone. Second run `changed=0`. `kubectl get nodes` Ready through the tunnel. **Pipelining measured back to back** on no-change runs, alternating: off 39 s and 44 s, on 35 s and 39 s, all `changed=0`. With `-vvv`: 151 SSH calls and 29 file transfers without pipelining, 39 calls and 3 transfers with it. SSH keeps one shared connection open (`ControlPersist=60s`), so each extra call is cheap and the gain is about 10%. The 16:38 comparison (136 s → 30 s) was two single runs and mostly showed how much Session Manager latency varies. Destroy: 9 destroyed; cost-check clean; no active sessions. About USD 0.005 (including about 6 minutes for the timing runs). |

Total for these runs: about USD 0.013 before tax.

## PolicyPal on the node (Phase C4)

### Local first (k3d, free)

Everything below ran on the local k3d cluster before any AWS time: `make policypal-local` from a new cluster pulled the image (2 min 45 s for 641 MB), started the API (44 s from container start to Ready) and passed the smoke test. On the way it found three things, each fixed test-first:

- **The first smoke test design failed.** `kubectl create --raw` through the API server's service proxy sends no `Content-Type`, so FastAPI rejected the JSON body with a 422 (`kubectl -v=9` showed the missing header). The smoke test now uses `kubectl port-forward` to the API Service and `curl` with the header.
- **A test tag of forty zeros was read as a YAML number** and kustomize refused the file, so the deploy stopped before applying anything. Image tags are now quoted.
- **The secrets script re-applied a bare Namespace** and stripped the label the manifests give it, so every deploy reported the namespace as changed. It now creates the namespace only if it's missing (with `--save-config`).

The rollback was shown on k3d too: a tag that doesn't exist left the new pods in ImagePullBackOff, the rollout timed out, both Deployments went back to their previous revision and the smoke test passed again. The UI kept serving during the bad deploy; the API (`Recreate`) was down until the rollback.

Memory on k3d (steady, then peak from the container's cgroup `memory.peak`): API 910 / 1016 MiB, UI 81 / 90 MiB, Postgres 71 / 101 MiB. With k3s and Ubuntu already using about 1.1 GiB of a t3.small's 2 GiB (C3), the default node became c7i-flex.large (4 GiB).

### Steps

```bash
aws login --profile devops
make cost-check
make cloud-up                     # c7i-flex.large Spot by default
make cloud-config
make cloud-tunnel                 # second terminal; leave it open
make cloud-deploy                 # Secrets, apply, wait, smoke test (rolls back on failure)
# browser: http://<node-ip>/ -> ask g04 and g41
# Ctrl-C in the tunnel terminal, then:
make cloud-down
make cloud-rebuild                # from nothing: cloud-up, then cloud-config
make cloud-tunnel                 # second terminal again
make cloud-deploy                 # the policies are ingested again by themselves
make cloud-down
```

On the node: `kubectl top`, each container's cgroup `memory.peak`, and `free -m` / `df -h` through Ansible. From the laptop: `curl` on 80 for `/`, `/docs`, `/openapi.json`, `/health`, and connection attempts on 22, 443, 5432, 6443, 8000, 8501, 9100, 10250 and 30080.

### Expected results

| Check | Expected |
|---|---|
| `make cloud-deploy` | all rollouts finish; smoke test 6/6: `/health` ok in extractive mode with every policy loaded, g04 answered from Leave Policy 6.3, g41 refused, UI healthy through the ingress, no Swagger or API schema through it |
| Browser (owner) | g04 cited from Leave Policy 6.3; g41 refused; `/docs` shows Streamlit, not Swagger |
| Memory | fits a c7i-flex.large with room for monitoring in C7 |
| Ports | only 80 answers |
| Rebuild | a new node ingests the policies by itself and passes the smoke test with no manual step |
| End | destroy, `make cost-check` exit 0, no active Session Manager sessions |

### Result log

| Date (MYT) | Run | Result | Notes |
|---|---|---|---|
| 2026-11-21 21:52 | Apply, c7i-flex.large Spot | Pass | 9 added in 26 s. Spot price USD 0.0528/h in ap-southeast-1a. |
| 2026-11-21 21:53 | `make cloud-config` | Pass | 62 s, `changed=20`, `failed=0` (147 s on the t3.small in C3). |
| 2026-11-21 21:54 | `make cloud-deploy` | Pass | 2 min 5 s including the image pulls (API image 35 s, the UI's copy 1 min 13 s in parallel, pgvector 8 s). Wait for Postgres 13 s, then 34 s from API start to Ready. Smoke test 6/6. kubectl warned that the namespace had no last-applied annotation; fixed afterwards with `--save-config` (`bab6a35`). |
| 2026-11-21 21:57 | Memory and disk | Pass | Node: 3812 MiB, 1748 used, 2063 available. API 700 MiB steady, 827 MiB peak; UI 57 / 74 MiB; Postgres 39 / 95 MiB. Requests 1420 MiB (37%), limits 3242 MiB (85%). Disk 6.7 of 19 GB. A t3.small would not fit; c7i-flex.large leaves about 2 GiB. |
| 2026-11-21 21:57 | Exposure | Pass | Port 80: `/`, `/docs`, `/openapi.json`, `/health` all returned Streamlit's page (200), never Swagger; `/_stcore/health` "ok". 22, 443, 5432, 6443, 8000, 8501, 9100, 10250, 30080: no connection. |
| 2026-11-21 22:18 | Tunnel | Note | The tunnel closed itself: "Your session timed out due to inactivity" after 20 minutes without kubectl traffic (Session Manager's default idle timeout). Run `make cloud-tunnel` again. |
| 2026-11-21 22:21 | Browser check | Pass | g04: answer cited from Leave Policy 6.3 ("sibling or grandparent: 1 day", score 0.80). g41: refused. `/docs`: Streamlit's "Page not found". |
| 2026-11-21 22:23 | Destroy | Pass | 9 destroyed; cost-check clean. About USD 0.031. |
| 2026-11-21 22:28 | Rebuild from nothing | Pass | Apply 26 s, `make cloud-config` 62 s, `make cloud-deploy` 2 min 2 s with new random Secrets: the API ingested all 8 policies (`ingest.completed`, 8 created), `/health` 8 documents and 119 chunks, nothing left unloaded; smoke test 6/6. **4 min 1 s from apply to a tested app.** |
| 2026-11-21 22:39 | Destroy | Pass | 9 destroyed; cost-check clean. The tunnel was stopped only after the node was gone, so its session stayed listed as connected; terminated with `aws ssm terminate-session`. Close the tunnel before `make cloud-down`. About USD 0.011. |

Total for these runs: about USD 0.042 before tax.

Record each run here, including failures and what was changed.
