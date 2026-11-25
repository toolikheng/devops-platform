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

After the code review, a new k3d cluster ran the reviewed smoke test end to end. The first deploy stopped with "exceeded its progress deadline": the image pull took 11 minutes on one node (the local-path provisioner's helper also timed out three times behind it), longer than a Deployment's default 10-minute progress deadline, which `kubectl rollout status` obeys whatever the script's own timeout says. Both Deployments now allow 900 s, matching the script (test-first). The next deploy passed the smoke test 6/6 with another port-forward left open on the old fixed port 18000.

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

## Secrets from Parameter Store through External Secrets Operator (Phase C5)

### Local first (k3d, free)

k3d has no AWS identity, so ESO can't read Parameter Store there, but everything else ran locally first:

- `scripts/install-eso.sh` installed the chart in 89 s and the ClusterSecretStore was accepted by ESO's webhook. The controller's pod IP was the node's own address (host network); the webhook and cert-controller had pod-network addresses. The store reported "unable to validate store" (no credentials), as expected.
- A probe ExternalSecret in `default` was refused by the store: "using cluster store "aws-parameter-store" is not allowed from namespace "default": denied by spec.condition". The same probe in `policypal` failed only because the store wasn't ready.
- **The deploy's new wait for the Secrets failed in 0.4 s instead of waiting**: `kubectl wait --for=create secret/a secret/b` returns NotFound at once when it is given more than one resource; with one it waits. The test's fake kubectl had accepted two names. Fixed test-first: one wait per Secret. With no Secrets the deploy then waited 123 s, said where to look and exited 1; with the k3d script's Secrets it passed the smoke test 6/6.
- ESO's memory (cgroup, current / peak): controller 54 / 55 MiB, cert-controller 53 / 54 MiB, webhook 38 / 39 MiB.
- After the AWS session: with `pod-security.kubernetes.io/enforce: baseline` on the namespace, PolicyPal deployed and passed the smoke test, and a host-network pod was refused in `policypal` but admitted in `default`.

### Steps

```bash
aws login --profile devops
make cost-check
cd terraform/persistent && terraform init && terraform plan -out tfplan && terraform apply tfplan && cd -
make check-state-secrets          # every count must be 0
make cloud-up && make cloud-config
make cloud-tunnel                 # second terminal
make cloud-addons                 # External Secrets Operator + the store
make cloud-deploy                 # the ExternalSecrets make the Secrets; smoke test
# checks below, then rotation: admin_token_version 1 -> 2 in variables.tf, plan, apply
kubectl -n policypal annotate externalsecret policypal-admin force-sync="$(date +%s)" --overwrite
kubectl -n policypal rollout restart deployment/policypal-api
make check-state-secrets
# Ctrl-C in the tunnel terminal, then:
make cloud-down
```

Checks: delete `policypal-admin` and time ESO recreating it; an IMDSv2 token request from inside the API pod, and from the same image with `hostNetwork: true`; a probe ExternalSecret on `/devops-platform/owner-cidr`; `aws iam simulate-principal-policy` for the node role; `ss -ltnp` and the containers' `memory.peak` on the node through Ansible; `GET /analytics` with the old token, the new one, none and a random one (tokens on curl's stdin, only status codes printed).

### Expected results

| Check | Expected |
|---|---|
| State | `make check-state-secrets` 0 for both parameters, after the first apply and after a rotation; second plan no changes |
| Store | Ready with the node's role, no credentials of its own |
| Deploy | the Secrets appear right after the apply; smoke test 6/6 |
| Deleted Secret | ESO makes it again |
| IMDS | no token in a pod-network pod while ESO keeps syncing |
| Scope | AccessDenied for any parameter outside `/policypal/aws/` |
| Rotation | new token 200, old token 403 after the API restarts |
| End | destroy, `make cost-check` exit 0, no active Session Manager sessions |

### Result log

| Date (MYT) | Run | Result | Notes |
|---|---|---|---|
| 2026-11-22 13:04 | Persistent apply | Pass | 3 added (the two parameters, the node's `policypal-parameters` policy), 1 changed (the role's description). The plan showed `value_wo = (write-only attribute)` and no value. `make check-state-secrets`: 0 and 0. Second plan: no changes. |
| 2026-11-22 13:11 | Apply, c7i-flex.large Spot | Pass | 9 added in 23 s. Spot price USD 0.0534/h in ap-southeast-1a. |
| 2026-11-22 13:12 | `make cloud-config` | Pass | 63 s, `changed=20`, `failed=0`. k3s's Traefik install job failed its first try and succeeded on its own retry. |
| 2026-11-22 13:14 | `make cloud-addons` | Pass | 51 s. Store `Ready`: "store validated", with the node's role through IMDSv2 from the host network. |
| 2026-11-22 13:16 | `make cloud-deploy` | Pass | 2 min 4 s. Both ExternalSecrets `SecretSynced`; the Secrets were created in the same second as the ExternalSecrets and are owned by them. Smoke test 6/6. |
| 2026-11-22 ~13:18 | Deleted Secret | Pass | `policypal-admin` back after 0.2 s (new object, same key). |
| 2026-11-22 ~13:19 | IMDS | Pass | From the API pod: token request timed out. Same image with `hostNetwork: true`: token received. ESO stayed synced. The second result showed that any host-network pod gets the node's credentials, so the `policypal` namespace now enforces Pod Security "baseline" ([ADR 017](decisions/017-imds-hop-limit-and-host-network.md)). |
| 2026-11-22 ~13:20 | Scope | Pass | Probe ExternalSecret on `/devops-platform/owner-cidr`: `AccessDeniedException` for `assumed-role/devops-platform-node`. Policy simulator: `GetParameter` on both PolicyPal parameters allowed; on owner-cidr, and `GetParameters`, `GetParametersByPath`, `PutParameter`, `DeleteParameter`: implicit deny. |
| 2026-11-22 ~13:21 | Ports and memory | Pass | Host ports: 22, 53 (loopback), 6443, 6444, 9100, 10010, 10248–10259, and ESO's 8080 (metrics) and 8082 (health). ESO current / peak: controller 37 / 39 MiB, cert-controller 41 / 54, webhook 32 / 32. |
| 2026-11-22 ~13:22 | `helm upgrade` | Fail, then pass | Adding the controller's liveness probe (which also sets its health port 8082 explicitly; the tests hadn't seen it) failed after 5 minutes: the new controller pod stayed Pending, "didn't have free ports for the requested pod ports". A rolling update needs the old and the new host-network pod on one node at once. Fixed test-first with `strategy: Recreate` (`d5706e0`); the next upgrade finished in 23 s. |
| 2026-11-22 13:31 | Rotation | Pass | Before: old token 200, none 401, random 403. `admin_token_version` 1 → 2 (`1336013`), applied: SSM version 2, state check 0 and 0. After a forced sync the Secret held version 2, but the running API still took the old token (200) and refused the new one (403). After `rollout restart` (32 s): new 200, old 403, none 401. Smoke test 6/6. |
| 2026-11-22 ~13:36 | Node memory | Note | 2341 MiB of 3812 (61%), with the API just restarted. C7 checks this again before adding monitoring. |
| 2026-11-22 13:44 | Destroy | Pass | Tunnel closed first. 9 destroyed; cost-check clean; no active sessions. About USD 0.034. |

Total for this run: about USD 0.034 before tax.

## CI/CD with GitHub OIDC (Phase C6)

### Local first (k3d and offline, free)

- The roles were checked offline before any apply: 36 `terraform test` runs on the persistent stack (exact trust conditions and permission lists), 27 tests tying each workflow job to a role that trusts it, and 58 workflow rules. A mutation check broke 57 rules one at a time (wildcard subject, a job without its environment, a missing Deny, an apply without the hash check, …); every one was caught.
- The deploy path on k3d: the `local` overlay rendered with the broken tag `000…0` and deployed as a given file (`DEPLOY_MANIFESTS`, the way the node gets it) went to `ImagePullBackOff`; both Deployments rolled back to revision 1's image; the script exited 1 after 145 s (2-minute rollout timeout); the smoke test passed 6/6 afterwards. The tagged render with the pinned commit was byte for byte the plain render.
- The EC2 actions in the apply and destroy roles came from CloudTrail event history of earlier real runs (Terraform's own calls), not from memory.

### Steps

```bash
aws login --profile devops
make cost-check
cd terraform/persistent && terraform plan -out tfplan && terraform apply tfplan && cd -   # OIDC provider + 4 roles
# once: the GitHub settings in docs/runbook.md (environments, AWS_ACCOUNT_ID secret, SSH_PUBLIC_KEY variable)
# a pull request touching terraform/  -> Terraform workflow: plan comment
gh workflow run terraform.yml -f action=apply -f instance_type=t3.micro -f use_spot=true   # approve in aws-apply
gh workflow run terraform.yml -f action=destroy -f instance_type=t3.micro -f use_spot=true # approve in aws-apply
make cloud-rebuild && make cloud-tunnel   # (second terminal) && make cloud-addons
gh workflow run deploy.yml -f policypal_tag=16d4edb48b0508a75680831c186d435ae565bccf
gh workflow run deploy.yml -f policypal_tag=0000000000000000000000000000000000000000      # rollback test (before the GHCR check existed)
gh workflow run nightly-destroy.yml
make cost-check
```

### Expected results

| Check | Expected |
|---|---|
| PR | read-only plan through OIDC, a comment with addresses and actions only; no account ID, IP or plan detail in the public log |
| Apply | waits for my approval; plans again; applies the saved plan only if the hash matches |
| Destroy (CI) | removes everything, deleting only tagged resources |
| Deploy | smoke test 6/6 on the node through SSM Run Command, no inbound access |
| Broken commit | rollback on the node, workflow red |
| Nightly | skip check, destroy, cost check clean; also by schedule at 02:00 MYT |
| End | `make cost-check` exit 0 |

### Result log

| Date (MYT) | Run | Result | Notes |
|---|---|---|---|
| 2026-11-22 20:02 | Persistent apply | Pass | 9 added (OIDC provider, 4 roles, 4 policies), 2 changed (the budget action now also targets `gha-tf-apply`). `make check-state-secrets` 0 and 0; second plan no changes. IAM policy simulator: 39 of 39 decisions as expected. |
| 2026-11-22 20:14–20:19 | C5 carry-over, c7i-flex.large Spot | Pass | `make cloud-deploy` 6/6 with the one-key ExternalSecrets; the API's database URL is built from the Secret's password (checked in the pod, not printed); a host-network pod is refused in `policypal` on AWS too. |
| 2026-11-22 20:28 | `ci/deploy-over-ssm.sh` from the laptop | Pass | 14 s: the node fetched the repo at the pushed commit, ran the plain `sh` commands, got its private address from IMDSv2, applied (all objects unchanged) and passed the smoke test 6/6. |
| 2026-11-22 20:30 | PR #7 plan | Fail, then pass | STS refused the token: "Not authorized to perform sts:AssumeRoleWithWebIdentity". The repo uses GitHub's immutable subject (`repo:toolikheng@149467084/devops-platform@1406132198:…`), seen in CloudTrail; the trust policies had the plain name. Fixed test-first (`38c0a8b`), applied (4 changed); the rerun commented "No changes." with the hash. Public log: no account ID, no IP, no plan. |
| 2026-11-22 21:02 | Destroy (laptop) | Pass | 9 destroyed; cost-check clean; no sessions. About USD 0.049 for the session. |
| 2026-11-22 23:03 | `terraform.yml` apply, t3.micro Spot | Fail | After my approval: 8 resources created (network, key pair; free), then `RunInstances` refused by the apply role's own image Deny. `aws sts decode-authorization-message`: `ec2:Owner = amazon` (Canonical's images carry that alias; the Deny allowed only Canonical's account ID). Fixed test-first (`bcfa6d1`), applied (1 changed). |
| 2026-11-22 23:23 | `terraform.yml` apply, retry | Pass | Plan: 1 to add (the instance). Approved; the apply job planned again, the hash matched, and the saved plan created a t3.micro Spot node with IMDSv2 required. Public log: no account ID, no IP. |
| 2026-11-22 23:30 | `terraform.yml` destroy | Pass | Approved; 9 destroyed through the tag-limited delete permissions; state empty, instance terminated, no VPC, no Spot request. |
| 2026-11-22 23:41 | `deploy.yml`, pinned commit | Pass | Render 9 s with no AWS access; deploy 2 min 48 s through `gha-deploy` (first image pull on a new node); smoke test 6/6 on the node. The encoded manifests showed in every step header (job-level env): moved to the one step that uses it (`055ba64`). |
| 2026-11-22 23:46 | `deploy.yml`, `000…0` | Pass (red, as intended) | `ImagePullBackOff`, rollout timed out after 15 minutes, API and UI rolled back to revision 1, the job failed after 15 min 48 s. From the laptop afterwards: both Deployments on the pinned image, smoke test 6/6. The API was down for those 15 minutes, so the render job now checks the tag on GHCR first (`8ccc187`): `000…0` is refused in 0.7 s, before the node. |
| 2026-11-23 00:05 | `nightly-destroy.yml` by hand | Pass | "No skip set: destroying."; destroyed with `gha-tf-destroy`; the cost check in CI ran 72 checks, nothing billable. From the laptop: state empty, instance terminated. |
| 2026-11-23 00:29 | Overnight stack for the schedule | Pass | t3.micro Spot from a laptop apply, left running on purpose for the 02:00 MYT schedule (about USD 0.012/h). |
| 2026-11-23 02:00 | `nightly-destroy.yml` by schedule | Late | Not started by 03:00: `gh run list --workflow nightly-destroy.yml --event schedule` showed none since 18:00 UTC. The workflow was active, on `master` since 21:15 the evening before, with `cron: "0 18 * * *"`. The stack kept billing (about USD 0.012 for the extra hour). I first recorded this as "never started"; the 07:30 row below shows it was late. |
| 2026-11-23 03:00 | `nightly-destroy.yml` by hand | Pass | Run 45058281234: "No skip set: destroying.", destroyed (instance terminated 03:01:32), the cost check in the job clean (72 checks). From the laptop at 03:12: state empty, no VPC, no sessions, `make cost-check` clean. |
| 2026-11-23 07:30 | The 02:00 scheduled run, 5 h 30 min late | Pass | Run 45083620960 (`event=schedule`, commit `4f12a7b`, cron `0 18 * * *`) started at 07:30:34 MYT, 330 minutes late. Skip check, destroy with nothing left to delete, cost check clean. |

Total: about USD 0.049 (live #1), about USD 0.064 (live #2, including the overnight stack) and about USD 0.002 (the re-check below) before tax.

After the code review (2026-11-23) the workflows' Terraform steps moved into one script, `ci/ephemeral-stack.sh`, and `terraform.yml` lost its duplicate static-checks job (the same checks run in `ci.yml`). The runs above tested the workflows as they were on `master` before that change (`4f12a7b`). The first scheduled destroy ran 5 h 30 min late; an hour's delay already kept a stack billing, which is the case the budgets and `make cost-check` are there for. The cron moved to `17 18 * * *` (02:17 MYT): GitHub's docs name the start of every hour as a busy time when scheduled runs can be delayed or dropped; the next run was even later (below). The schedule test after the merge needs no stack: the next day `gh run list --workflow nightly-destroy.yml --event schedule` must show a successful run (with nothing to destroy, it still runs the cost check).

### Re-check of `ci/ephemeral-stack.sh` on `master` (`fe30b9f`)

| Date (MYT) | Run | Result | Notes |
|---|---|---|---|
| 2026-11-23 13:28–13:44 | `terraform.yml` apply, t3.micro Spot | Pass | Run 45117938021. The plan job's log showed the 9 changes and the same hash as a local plan; after my approval the job planned again and applied the saved plan. IMDSv2 required. Public logs: no account ID, owner IP or node IP. |
| 2026-11-23 13:45–13:49 | `terraform.yml` destroy | Pass | Run 45119721026. Local and CI hashes equal; after my approval all 9 deleted. State empty, instance terminated, no VPC, no Spot request. |
| 2026-11-23 13:50–13:57 | `terraform.yml` apply again | Pass | Run 45120264857, so the nightly job had a real stack to delete. |
| 2026-11-23 13:59–14:02 | `nightly-destroy.yml` by hand | Pass | Run 45121194386: "No skip set: destroying.", the 9 deletes listed in the log, cost check in the job clean (72 checks). From the laptop at 14:05: state empty, instance terminated, no VPC, Spot request or session, `make cost-check` clean. |

About USD 0.002 before tax (11 minutes of t3.micro Spot).

### The schedule at 02:17 MYT

| Date (MYT) | Run | Result | Notes |
|---|---|---|---|
| 2026-11-24 11:57 | The 02:17 scheduled run, 9 h 40 min late | Pass | Run 45323027935 (`event=schedule`, commit `544d41a`, cron `17 18 * * *`) started at 11:57:30 MYT, 9 h 40 min after 18:17 UTC, and ended at 11:59:39. Skip check, nothing to destroy (the stack was already empty), cost check clean (72 checks). From the laptop at 15:53: state empty, no sessions, `make cost-check` clean. |

Both scheduled runs so far were hours late: 5 h 30 min with the cron on the hour, 9 h 40 min off the hour. Moving off the hour didn't help in this sample. The nightly destroy is a safety net for a forgotten session; `make cloud-down` at the end of every session is the cleanup. The budget alerts and `make cost-check` stay as the backup for both.

Record each run here, including failures and what was changed.

## Observability, backup and demo (Phase C7)

### Local first (k3d and offline, free)

- **One add-on script for every chart ([ADR 022](decisions/022-one-addon-script-for-every-chart.md)), on k3d (2026-11-24, 3 nodes, k3s v1.35.5):** `make monitoring-up` from `master` installed kube-prometheus-stack 92.16.0 the old way (revision 1; on a fresh cluster it stops at the ServiceMonitor until `make deploy` has created the `devops` namespace, the order `make all` uses). `make deploy` from the branch created `backup-credentials` from stdin (one key, 48 characters, not printed). `make monitoring-up` from the branch then upgraded the release in place: revision 3, same chart, `--wait` returned in 17 s, the existing `grafana-admin` Secret kept (same uid), the ServiceMonitor unchanged (generation 1; kubectl said `configured`, the cosmetic message also seen for Postgres). Prometheus ran `quay.io/prometheus/prometheus:v3.15.1-distroless`, the image `ci/rules-test.sh` now uses, had loaded the `devops-app` group (AppDown, HighErrorRate, HighLatency, NodeExporterDown) and scraped the app (`up`).
- **ESO through the same script on k3d:** `scripts/addon.sh install external-secrets aws` in 49 s, controller, webhook and cert-controller Running, the `ClusterSecretStore` created and `Ready=False` (`InvalidProviderConfig`): k3d has no AWS identity. `Ready=True` is checked on the AWS node.
- **Memory of the homelab monitoring (v1 values, no limits), about 4 minutes after the install:** Grafana 762 MiB, Prometheus 376 MiB, Alertmanager 54 MiB, operator 48 MiB, kube-state-metrics 46 MiB, node-exporter 12–13 MiB on each of 3 nodes: 1,324 MiB in all. This is above the 0.6–0.9 GB the plan estimated for the trimmed AWS stack, so the AWS values are measured on k3d before the instance size is chosen.
- kube-prometheus-stack rendered and checked with kubeconform for the first time: 127 objects, all valid.
- **The monitoring's AWS values on k3d (2026-11-24):** three problems found and fixed before AWS: Grafana's datasource init container used `WATCH`, which never exits, and was OOMKilled at 96 MiB (now `LIST`, ConfigMaps in one namespace, 160 MiB); Grafana downloaded about six app plugins at every start, so the liveness probe restarted it twice (now `preinstall_disabled`, ready in 61 s with no restart); the chart added an Alertmanager datasource with no Alertmanager behind it (now off). Pod memory with PolicyPal, ESO and the monitoring: 1,956–2,069 MiB.
- **PolicyPal's alert and dashboard on k3d, with the AWS values (2026-11-25):** Grafana's sidecar loaded the dashboard from the `policypal` namespace and Grafana served it (uid `policypal`, 6 panels); five panel queries returned data (the node memory panel has no node_exporter to read on k3d; on AWS the same metric was scraped). With the API scaled to 0 the alert was pending after 31 s and firing after 3 min 2 s; with the API back it cleared within 61 s.
- **The backup script against k3d (2026-11-25):** the volume's directory was in `spec.local.path` (this k3s's local-path provisioner), and `pg_dump` in the Postgres pod with the pod's own user worked (output starts `PGDMP`). k3d's nodes have no bash, so the script ran in a container on the k3d network with a copy of the uploads directory at the same path, restic 0.19.1 and a local repository. First run: restic reported no repository (exit 10), the script created it, saved the dump (265 KiB) and the uploads, applied retention, pruned and checked with no errors, exit 0. Second run: two more snapshots, exit 0. The four snapshots are grouped under host `policypal` with one `run-` tag per run, and the stored dump starts `PGDMP`. S3, SSM and the systemd units are checked on the node.
- **The restore against k3d (2026-11-25):** the same container setup. A question and an upload file before a backup (exit 0, the textfile metric written), then another question and another file after it. `policypal-backup restore latest` (48 s, exit 0): the API scaled to 0, the database recreated and the dump loaded, restic kept the unchanged file and deleted the later one (`--delete`), the API back. Afterwards the earlier question was in PolicyPal's question history and the later one was not, the earlier file was there with the same sha256 and the later one was gone, and the smoke test passed. (PolicyPal keeps question history in lower case, which the restore test's sentinels are written in.) `scripts/restore-test.sh` itself needs SSM Run Command, so it runs for the first time on AWS.
- **PolicyPal image `5e6ecf1` (2026-11-25):** the image both overlays pinned (`16d4edb`, built 2026-11-03) failed the image gate's settings (Trivy 0.75.0, HIGH and CRITICAL, fixable only): 18 HIGH, 8 in Debian packages (libzstd1, zlib1g, OpenSSL, PCRE2, apt) and 10 in pypdf 6.19.0. PolicyPal's rebuilt image `5e6ecf1` (digest `sha256:66941d7c…`) has none. Docker Desktop couldn't export the image to Trivy, and Trivy's registry mode lost its connection on the largest layer every time, so the new image's root filesystem was exported from a container and scanned with `trivy rootfs` and the same settings: 0 findings; the same method on the old image found the same 18. On k3d (`make policypal-local`), the first deploy stopped at the UI's 15-minute progress deadline while its node was still pulling the image (655 MB to download, about 2.1 GB unpacked: 21 min 16 s; the API's node took 9 min 18 s); once pulled, the deploy and the smoke test passed 6/6.

### Result log

| Date (MYT) | Run | Result | Notes |
|---|---|---|---|
| 2026-11-24 20:28 | Persistent apply: the secret catalog ([ADR 023](decisions/023-one-catalog-of-secrets.md)) | Pass | Saved plan: 0 to add, 0 to change, 0 to destroy, two moves (`aws_ssm_parameter.db_password` and `.admin_token` to `aws_ssm_parameter.secret["db-password"]` and `["admin-token"]`); in its JSON all 28 resources no-op, nothing replaced or deleted. It also listed one drift: the `gha-tf-apply` role's own copy of its inline policy in the state was the version from before the C6 image-owner fix; the policy in AWS was exactly the one Terraform manages (same hash as the code), so nothing had changed outside Terraform, and the apply refreshed the copy. Applied in 5 s; second plan no changes; `make check-state-secrets` 0 and 0; in SSM the admin token is still version 2 (last changed by the C5 rotation) and the database password version 1: the values were not touched. IAM policy simulator, node role: `ssm:GetParameter` on `/policypal/aws/restic-password` allowed (the path covers it, no IAM change), `GetParameters`, `PutParameter` and the owner-cidr parameter not. |
| 2026-11-24 23:02–23:10 | Monitoring on AWS, c7i-flex.large Spot | Pass | `make cloud-addons` through `scripts/addon.sh` in 3 min 2 s: External Secrets Operator, then kube-prometheus-stack with `values-aws.yaml`. The `ClusterSecretStore` became Ready ("store validated"). Prometheus found the node through the API and scraped the node_exporter Ansible installed (target up); every other target up. |
| 2026-11-24 23:10 | `make cloud-deploy` | 5 of 6 | The UI check through the ingress got no answer: my public IP had changed since the session started, and the security group still allowed the old /32. The API checks passed (they go through the Session Manager tunnel). `make owner-ip` and a plan that changed only the rule's address fixed it (below). |
| 2026-11-24 23:10–23:20 | Node memory, c7i-flex.large (3,812 MiB) | Measured | `1 - MemAvailable/MemTotal`, `max_over_time` per window: idle (3 minutes) 75.9%; 16 questions (8 in a row, 8 at once; answers in about 0.1 s) 76.3%; `rollout restart` of the API (ready in 43 s) 76.9%, and 76.9% from a once-a-second read of node_exporter inside the cluster (296 reads). Above the 75% rule: the default became m7i-flex.large ([ADR 024](decisions/024-m7i-flex-node-for-monitoring.md)). |
| 2026-11-24 23:40 | Node replaced by an m7i-flex.large | Pass | With the new default, Terraform planned an in-place type change, which a one-time Spot instance can't do (it can't be stopped). `-replace=module.compute.aws_instance.node` plus the new IP: 1 added, 1 changed (the rule), 1 destroyed, in 53 s. Rebuild: `cloud-config` 64 s, `cloud-addons` 3 min, `cloud-deploy` 2 min with the smoke test 6 of 6. |
| 2026-11-24 23:48–23:56 | Node memory, m7i-flex.large (7,776 MiB) | Measured | Idle 38.3%; 16 questions 38.3%; API restart (ready in 32 s) 38.1%, and 38.3% from the once-a-second read (197 reads). |
| 2026-11-25 00:24 | Destroy | Pass | 9 destroyed; both instances terminated, no VPC, Spot request or Session Manager session left; `make cost-check` clean. Session about USD 0.08. |
| 2026-11-25 01:52 | Persistent apply: the backups bucket and the restic password | Pass | Saved plan, made again from the same commit just before the apply and identical to the one reviewed: 8 to add, 0 to change, 0 to destroy. Applied in 64 s (the lifecycle rule took 56). Second plan no changes; `make check-state-secrets` 0 for all three parameters. Read back from AWS: versioning on, AES256, all four public-access settings on, the TLS-only policy, one lifecycle rule with no expiry of current objects (old versions 7 days, unfinished uploads 1 day), the bucket empty. IAM policy simulator, node role: list the `restic/` prefix, get, put and delete under it, and read `/policypal/aws/restic-password` allowed; another prefix, another object, the state bucket, a bucket policy change and deleting the bucket not. The first plan for this change had also updated the admin token's description; it showed the parameter's version changing, and the provider's code showed why that would have failed or emptied the token (ADR 023). That change was taken out before the apply. |
| 2026-11-25 17:06 | Ephemeral apply, m7i-flex.large Spot, then `make cloud-config` with the restic role | Pass | My IP had changed again, so `make owner-ip` first (read back: stored = current). Saved plan: 9 to add. First `cloud-config` 86 s (ok=42, changed=32, failed=0): restic 0.19.1 with its checksum, `python3-boto3`, the backup script, the SSM reader, the settings file, the units and node_exporter's textfile directory. **Second run: changed=0.** `cloud-addons` 3 min (store Ready), `cloud-deploy` 2 min 15 s, smoke test 6 of 6. |
| 2026-11-25 17:16 | First backup to S3 from the node | Pass | `systemctl start policypal-backup`, 13 s, exit 0. restic said `Fatal: repository does not exist` (expected), the script created the repository in `s3://toolikheng-devops-platform-backups/restic` with the node role (IMDSv2; no `s3:GetBucketLocation` needed with `AWS_DEFAULT_REGION`), saved the dump (265 KiB), then the uploads, applied retention, pruned and checked with no errors. The journal has no password, secret or key in it; the settings file has none either. The runbook's `restic snapshots` commands listed both snapshots; `policypal-backup show latest` printed the run and its two snapshot IDs. |
| 2026-11-25 17:17 | The backup metric and the dashboard on AWS | Pass | The textfile was written and node_exporter served it (`node_textfile_scrape_error 0`); Prometheus scraped `policypal_backup_last_success_timestamp_seconds` from the node_exporter target. Every target up. Grafana served the dashboard (uid `policypal`, 7 panels, provisioned) with only the Prometheus datasource, and **all 7 panel queries returned data**, including node memory, which k3d can't show. |
| 2026-11-25 17:18 | The alert on AWS | Pass | API scaled to 0 at 17:18:01: `PolicyPalApiUnavailable` pending after 25 s, firing after 2 min 26 s (2 min after pending, the rule's `for`). API back to 1: ready in 21 s, the alert inactive within 60 s. Smoke test 6 of 6 afterwards. |
| 2026-11-25 17:21 | `make cloud-restore-test-before` | Pass | 40 s: the sentinel question and policy, a backup through SSM Run Command, the run `run-20261125T092159Z`, its two snapshots, the node and the policy's sha256 recorded in `~/.cache/devops-platform/restore-test.env` (mode 600). |
| 2026-11-25 17:30 | Destroy, then rebuild | Pass | 9 destroyed in 29 s; `make cost-check` clean. New saved plan, 9 added in 25 s (a different instance); `cloud-config` 82 s, `cloud-addons` 2 min 54 s, `cloud-deploy` 1 min 56 s with the smoke test 6 of 6: an empty PolicyPal on a new node. |
| 2026-11-25 17:48 | `make cloud-restore-test-after` | **Pass** | 74 s: the sentinels absent on the new node; `policypal-backup restore run-20261125T092159Z` reported the recorded database and uploads snapshots; afterwards the question was in PolicyPal's history, the policy listed and its file's sha256 the same. `make cloud-smoke` 6 of 6. |
| 2026-11-25 17:50 | Retention with a rebuilt node (found in the restore test) | **Problem** | The timer was on from the moment Ansible finished, and it backs up every hour at :00 UTC. Retention keeps the newest snapshot of each day (`restic forget --dry-run` showed this; while there are fewer than 7 days, restic 0.19 also keeps the oldest). So the new, empty node's first hourly backup would have become today's kept snapshot, and the run I was about to restore would have been forgotten and pruned. The restore ran 11 minutes before the timer. In a real loss of the node, the same thing would replace the day's good backup and make `restore latest` pick an empty one. Fixed afterwards (offline): a new node's timer stays off until a restore or `policypal-backup enable` ([ADR 025](decisions/025-restic-backups-from-the-node.md)); `tests/test_backup_retention.py` reproduces the problem with restic 0.19.1. Not yet run on AWS. |
| 2026-11-25 17:53 | Destroy | Pass | 9 destroyed; both instances terminated, no VPC, Spot request or Session Manager session left (Ansible's SSH connection kept one Session Manager session open for its 60-second reuse; closed with `ssh -O exit`); `make cost-check` clean. The timer's own hourly run wasn't seen (destroyed at 17:53, before 18:00). Session about USD 0.04. |
