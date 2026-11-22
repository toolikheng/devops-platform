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
| 2026-11-23 00:29 | Overnight stack for the schedule | Running | t3.micro Spot left for the 02:00 MYT schedule (my choice, about USD 0.012/h). |

Total so far: about USD 0.049 (live #1) and about USD 0.033 (live #2, before the overnight stack) before tax.

After the code review (2026-11-23) the workflows' Terraform steps moved into one script, `ci/ephemeral-stack.sh`, and `terraform.yml` lost its duplicate static-checks job (the same checks run in `ci.yml`). The runs above, and the 02:00 MYT scheduled destroy that night, tested the workflows as they were on `master` before that change (`4f12a7b`); the scheduled run counts as the schedule test. The new script is re-proved live after it merges: a t3.micro `terraform.yml` apply and destroy, and a `nightly-destroy.yml` started by hand.

Record each run here, including failures and what was changed.
