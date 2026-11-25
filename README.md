# DevOps Platform: a WSL2 homelab, extended to AWS

[![CI](https://github.com/toolikheng/devops-platform/actions/workflows/ci.yml/badge.svg?branch=master)](https://github.com/toolikheng/devops-platform/actions/workflows/ci.yml)

A portfolio project in two parts:

- **Homelab (v1).** On one WSL2 machine, Ansible provisions three Docker "nodes", a FastAPI service is built, scanned and deployed to a k3d (k3s) cluster, Prometheus and Grafana monitor it, restic backs it up, and GitHub Actions gates every push.
- **AWS (v2).** The same practices on a real account. Terraform creates a one-node k3s environment for each working session and destroys it at the end. Ansible configures the node through Session Manager, with no inbound admin port. It runs [PolicyPal](https://github.com/toolikheng/policypal-hr-rag), my HR-policy question-answering app, with secrets from SSM Parameter Store, monitoring and an alert, hourly backups to S3, and a restore tested on a rebuilt node. GitHub Actions reaches AWS through OIDC, with no stored keys. All the AWS work so far cost about USD 0.51 before tax ([What it cost](#what-it-cost)).

Every claim in the [What's verified](#whats-verified) table was checked by running it. Gaps are listed under [Limitations](#limitations).

## Quick start

### Homelab (local, free)

On WSL2 Ubuntu with Docker Desktop running:

```bash
git clone https://github.com/toolikheng/devops-platform.git && cd devops-platform
make check     # verifies the toolchain; `make install` fixes what it can
make all       # nodes -> Ansible -> tests -> image build + scan -> k3d -> deploy -> monitoring (~3.5 min)
```

Then:

```bash
curl -H "Host: devops-app.localhost" http://localhost:8080/healthz   # {"status":"ok"}
make restore-test   # back up, wipe and restore the app's database, then verify it (dev cluster only)
make destroy        # delete the cluster and the nodes
```

`make help` lists every target.

### AWS working session

Needs the one-time setup in [docs/runbook.md](docs/runbook.md) (the persistent stack, applied from the laptop) and `aws login --profile devops`. Everything the session creates is billed by the hour (about USD 0.06/h on Spot) until `make cloud-down`.

```bash
make cost-check        # nothing billable left from last time
make cloud-rebuild     # store my IP, show the Terraform plan and wait for "yes", then Ansible: a Ready k3s node (~3 min)
make cloud-tunnel      # in a second terminal: the Kubernetes API on localhost:16443, through Session Manager
make cloud-addons      # External Secrets Operator, then the monitoring (~3 min)
make cloud-deploy      # PolicyPal, its Secrets from Parameter Store, then the smoke test (~2 min)
# once, on the node (aws ssm start-session --target "$(bash scripts/node-id.sh)", then sudo -i):
#   policypal-backup restore latest    # bring back the last backup, then hourly backups start
#   policypal-backup enable            # or, with nothing to restore, just start them
make cloud-down        # destroy (asks first), then the cost check
```

## Architecture

### AWS (v2)

```
 Laptop (WSL2)                              AWS, ap-southeast-1
 ─────────────                              ─────────────────────────────────────────────────────────────
 make cloud-rebuild ── Terraform ─────────► persistent stack ($0 at rest): state bucket, budgets + deny
                                              action, IAM (node role, 4 CI roles, OIDC provider),
                                              SSM parameters, backups bucket
                                            ephemeral stack (one per session):
                                              VPC ─ public subnet ─ internet gateway (no NAT, no EIP)
                                              └─ EC2 m7i-flex.large, Spot, IMDSv2, SG: port 80 from my /32
                                                   └─ k3s
                                                       ├─ policypal: UI ◄─ Traefik :80 ◄── my browser
                                                       │             API, Postgres + pgvector, uploads
                                                       ├─ external-secrets ─ node role ─► Parameter Store
                                                       ├─ monitoring: Prometheus, Grafana (port-forward)
                                                       └─ on the node: node_exporter; restic timer
                                                                       ─ node role ─► S3 backups bucket
 make cloud-config ── Ansible, SSH inside Session Manager (port 22 closed) ──► node
 make cloud-tunnel ── Session Manager port forward ──► k3s API :6443 (never public)

 GitHub Actions ── OIDC, one role per job ──► PR plan comment · approved apply/destroy · 02:17 MYT
                                              destroy · deploy through SSM Run Command
```

### Homelab (v1) and CI

```
 git push ──► GitHub Actions CI ──(gates pass)──► GHCR  (master, sha-<commit>)
                 lint · tests · rule tests · hadolint · build · Trivy

 Manual CD ──► throwaway k3d on a GitHub-hosted runner ──► deploy dev overlay ──► smoke test

 AWS (OIDC, one role per job):  PR ──► read-only plan ──► PR comment
                                apply/destroy ──► my approval ──► plan again, hash check ──► saved plan
                                deploy.yml ──► GHCR check, render ──► SSM Run Command ──► node deploys itself
                                02:17 MYT ──► destroy ──► cost check

 WSL2 machine
 ├─ Docker nodes node1-3  ◄── Ansible over SSH (localhost:2201-2203)
 │    └─ node_exporter :9100-9102 ──────────────────────────────┐
 └─ k3d cluster "devops"                                         │
      ├─ devops:     Traefik ─► Service ─► app pod ─► SQLite on PVC
      │              backup CronJob ─► restic repo on PVC        │
      └─ monitoring: Prometheus ◄─ scrapes app + nodes ──────────┘
                     Alertmanager, Grafana (dashboard provisioned)
```

The local cluster runs an image built on the machine (`make deploy` imports it into k3d); it does not pull from GHCR. See [docs/architecture.md](docs/architecture.md) for components and data flows, and [docs/decisions/](docs/decisions/) for the design decisions.

## What's verified

| Claim | How it was checked |
|-------|--------------------|
| Fresh clone works | Cluster and nodes destroyed, repo cloned from GitHub into an empty directory, `make all` passed every step on the first run in 3m26s. Afterwards all pods were Ready, the app answered through the ingress, and Prometheus showed the app and all three nodes UP. |
| Provisioning is idempotent | `make provision` run twice against freshly created nodes: the first run changed 12 tasks per node, the second reported `changed=0`. node_exporter comes back on its own after `docker restart`. Rechecked after the roles were changed for AWS: `changed=0` on running nodes and the same 12 changes, then `changed=0`, on fresh ones. |
| CI gates every push | `ci.yml` runs ruff + mypy, 13 pytest tests, promtool alert-rule tests and hadolint. The image is built, scanned by Trivy, and pushed to GHCR only if there are no fixable HIGH/CRITICAL vulnerabilities. The lint, test, rule-test and Trivy gates were each shown to fail on a real problem before being trusted. |
| CD smoke test | `cd.yml` (manual) builds the image, creates a throwaway k3d cluster on a GitHub-hosted runner, deploys the dev overlay, and checks `/healthz` and `/readyz`. Last run passed. |
| Alert rules load and fire | All four rules are loaded by Prometheus. AppDown fired when the app was scaled to 0 pods (pending, then firing within about two minutes) and cleared when it came back. NodeExporterDown fired while node_exporter was missing and cleared once the nodes were provisioned. |
| Dashboard is provisioned | `make monitoring-up` loads the app dashboard into Grafana with no manual import. Its panels were checked through Grafana's query API under real traffic, and the success / 4xx / 5xx shares add up to 100%. |
| Backups are consistent | `make backup` was run while 656 writes hit the app; the SQLite copy passed `PRAGMA integrity_check` and was saved as a restic snapshot. |
| Restore test catches a broken restore | `make restore-test` passes only if the app serves exactly the backed-up items afterwards and an item written after the backup is gone. A deliberately broken restore (data never copied back) makes it fail. |
| Self-healing | Deleting the app pod: a replacement was Ready 14 seconds later. |
| AWS cost check is read-only and fails safe | `make cost-check` lists anything billable left in the account: the full list in ap-southeast-1 (instances, volumes, snapshots, Elastic IPs, public IPv4, NAT gateways, VPC endpoints, load balancers, EKS, RDS, Secrets Manager, open or active Spot requests, Route 53 hosted zones, ECR repositories, Lightsail instances and databases, SageMaker notebooks and endpoints, customer-managed KMS keys) plus instances, volumes and Elastic IPs in every other enabled region. 40 tests run it against a fake `aws` CLI: each kind of leftover is reported, only describe/list/get calls are made, the Cost Explorer API is never called, the full scan stays in ap-southeast-1 whatever `AWS_REGION` says, and a check that can't run is an error, never a clean result. Run against the real account after every session: clean, and it ignored the two cancelled one-time Spot requests and the free AWS-managed `aws/ebs` key. |
| Terraform state is private, TLS-only and locked | 21 `terraform test` runs check the persistent stack offline with a mock provider, and each one was shown to fail on a deliberately broken copy. Live: the apply created the state bucket and budgets; a second plan through the S3 backend shows no changes; a plain-HTTP request to the bucket gets AccessDenied even as the admin user; with two plans started at once, the second fails with "Error acquiring the state lock". |
| The AWS node is reachable only the way it should be | Live, on-demand and Spot: a shell through SSM Session Manager worked with a role allowed only the 11 SSM-agent actions. From my IP, port 80 was allowed while 22 and 6443 were dropped even though sshd was listening. IMDSv1 got 401 and IMDSv2 worked. Outbound HTTPS and HTTP worked; other ports were blocked. Details in [docs/aws-test-log.md](docs/aws-test-log.md). |
| The node's rules hold before anything is created | 33 offline `terraform test` runs on the network and compute modules and the ephemeral stack: allowlisted instance types only, x86 only, encrypted gp3 root volume of at most 30 GB deleted with the instance, IMDSv2 with hop limit 1, standard CPU credits, no inbound 22 or 6443, owner IP must be one /32. A mutation check broke 39 rules one at a time across the Terraform code; every one was caught. |
| Spot and on-demand both work, and both leave nothing behind | Live: each run was created, checked, destroyed and followed by a clean `make cost-check`; a second plan after each apply showed no changes. The first Spot run failed on a real AWS restriction (see [ADR 009](docs/decisions/009-spot-and-on-demand.md)); it was fixed test-first and the retry passed. The Spot request was one-time with the on-demand price as its cap. |
| Ansible configures the AWS node with no inbound admin port | Live, twice: `make cloud-config` reached the node with SSH inside a Session Manager session and ran `common`, `node_exporter` and `k3s` in 103 s; the second run reported `changed=0`, and `--check --diff` afterwards showed no drift. On the node: k3s Secrets encryption enabled, the k3s kubeconfig and config readable by root only, root and password SSH logins off, node_exporter running under systemd. From my laptop, 22, 6443, 9100 and 10250 were dropped. |
| kubectl works only through a tunnel | Live: `make cloud-tunnel` forwards the node's 6443 to `localhost:16443` through Session Manager, and `kubectl get nodes` showed the node Ready with TLS verified. The kubeconfig Ansible writes is mode 600 and outside the repo. |
| One command from nothing to a Ready cluster | Live: after a destroy, the stack was applied and configured straight away with no manual step; the node was Ready 175 s after the apply started, and a second run reported `changed=0`. Destroy and a clean `make cost-check` followed. |
| The rules for the AWS node's Ansible code hold | 13 pytest rules (Session Manager only, `ubuntu` with sudo, root login off on AWS but unchanged for the homelab, k3s pinned and its installer checksum-checked, Secrets encryption, the k3s role touching only the node, the kubeconfig copied without logging into a 600 file pointing at the tunnel, pipelining, check mode) and 18 tests of the Make targets against fake `terraform`, `aws` and `ansible-playbook`. A mutation check broke 45 rules one at a time; every one was caught. The laptop-side kubeconfig steps were also run on their own against a temporary home directory: a missing `~/.kube` is created 700, an existing one is left alone, and a second run changes nothing. ansible-lint passes on its strictest profile. |
| PolicyPal runs on AWS and answers correctly | Live, on a c7i-flex.large Spot node: `make cloud-deploy` applied the manifests, waited for every rollout and passed the smoke test 6/6 (`/health` ok in extractive mode with every policy loaded, golden question g04 answered from Leave Policy 6.3, g41 refused). In my browser, g04 cited "sibling or grandparent: 1 day" from Leave Policy 6.3 and g41 was refused. Measured on the node: 1748 of 3812 MiB used, the API peaking at 827 MiB. |
| Only the UI is reachable | Live, from my laptop: `/docs`, `/openapi.json` and `/health` on port 80 return Streamlit's page, never the API's Swagger page or schema (the smoke test checks the content, because Streamlit answers 200 for any path). 22, 443, 5432, 6443, 8000, 8501, 9100, 10250 and 30080 got no connection. |
| A rebuilt node fills itself | Live: after a destroy, apply → `make cloud-config` → `make cloud-deploy` on a new node with new random Secrets; the API ingested all 8 policies (119 chunks) by itself and the smoke test passed 4 min 1 s after the apply started. |
| A failed deploy rolls back | On k3d, and live on AWS through `deploy.yml`: a deploy with an image tag that doesn't exist timed out, both Deployments went back to their previous revision, the run failed, and the smoke test passed again. 88 tests run the deploy, smoke-test, Secrets, add-on and state-check scripts against fake `kubectl`, `curl`, `docker`, `helm`, `terraform` and `aws` (`tests/fakecli.py`): every failing check fails the smoke test, the smoke test stops if its own port-forward doesn't start (so it can never test some other cluster), only Deployments this deploy changed are rolled back, and one failed rollback doesn't stop the others, every namespaced `kubectl` call targets `policypal`, the Secrets created hold exactly the keys the manifests reference, and Secret values never appear in arguments or output. |
| The PolicyPal manifests follow the project rules | Both overlays are rendered with a pinned kustomize and validated with kubeconform `-strict` against Kubernetes 1.36 (ExternalSecrets against their CRD schemas at a pinned commit). 59 pytest rules on the rendered YAML: requests and a memory limit everywhere, probes (API health for readiness only), a rollout deadline no shorter than the deploy script's timeout, pinned and quoted image tags, no Secret objects, one API replica with `Recreate`, extractive mode, persistent uploads, Secrets referenced key by key, ExternalSecrets on AWS that make exactly those keys from the parameters Terraform creates, ClusterIP Services only, an ingress that reaches only the UI, non-root pods with no capabilities and no service account token, and Pod Security "baseline" on the namespace. Mutation checks broke 91 rules (C4) and 71 more (C5: Terraform, ESO, ExternalSecrets, the deploy's wait, the state check, secret scanning, Pod Security) one at a time; every one was caught. |
| Secrets never reach Terraform state | Terraform writes PolicyPal's two SSM parameters with write-only arguments from ephemeral random values. Offline: a test proves the configuration never sets a stored value, and a text check over every `.tf` file allows random values only as ephemeral resources. Live: `make check-state-secrets` counted 0 copies of each live value in the state, after the first apply and again after a rotation; the second plan showed no changes ([ADR 016](docs/decisions/016-parameter-store-and-external-secrets.md)). |
| External Secrets Operator makes PolicyPal's Secrets with the node's own role | Live: the store validated with no credentials of its own; `make cloud-deploy` applied the ExternalSecrets, both Secrets appeared in the same second, and the smoke test passed 6/6. A deleted Secret was back in 0.2 s. The node role got AccessDenied reading a parameter outside `/policypal/aws/`; the IAM policy simulator allows only `GetParameter` there. On k3d: the store refuses ExternalSecrets from other namespaces. |
| Pods can't use the node's AWS credentials | Live: an IMDSv2 token request from inside the PolicyPal API pod timed out while ESO (the only host-network controller) kept syncing; the same image on the host network got a token. So the `policypal` namespace enforces Pod Security "baseline": on k3d a host-network pod was refused there and admitted elsewhere, and PolicyPal still deployed and passed its smoke test ([ADR 017](docs/decisions/017-imds-hop-limit-and-host-network.md)). |
| Rotating a secret is a one-line commit | Live: `admin_token_version` 1 → 2, plan (one in-place change), apply. After a forced sync the running API still took the old token until a restart; then the new token got 200 and the old one 403. The commit records the rotation; the value appears nowhere. |
| Secret scanning catches what the defaults missed | gitleaks' default rules miss the two restic passwords committed long ago in `7ca581f`; tests prove the added rule finds exactly those two, flags or skips 16 lines written at run time as expected, and that the full history is clean with only those two fingerprints ignored. The pre-commit hook's first real run blocked one of my own commits (a comment reading like a password assignment), which led to a narrower rule. |
| GitHub Actions reaches AWS with no stored key | Live: a pull request's plan job got one-hour credentials for its read-only role through OIDC and commented the planned changes (addresses and actions only); the public log shows no account ID, no IP and no plan detail, and `gh secret list` holds only `AWS_ACCOUNT_ID`. The first attempt was refused because this repo's tokens use GitHub's immutable subject (owner and repo with their numeric IDs); CloudTrail showed the subject, and the trust policies now match it exactly. Offline: 36 `terraform test` runs check every trust condition and permission list, 27 tests tie each workflow job to a role that trusts it, and a mutation check broke 57 rules one at a time; every one was caught ([ADR 020](docs/decisions/020-one-oidc-role-per-ci-job.md)). |
| An apply makes only the changes I approved | Live: `terraform.yml` showed the plan (which resources, and how) and its hash, waited for my approval, planned again, and applied the saved plan only after the hashes matched (values aren't hashed: that would leak short ones like my IP); a CI destroy removed all 9 resources through delete permissions limited to the stack's tags. The first CI apply was stopped by the role's own image Deny: `decode-authorization-message` showed Canonical's images are listed under the owner alias `amazon`, and the Deny now allows that alias (Marketplace and other accounts' images stay denied). |
| Deploys from GitHub need no inbound access | Live: `deploy.yml` rendered and validated the manifests with no AWS access, sent them inside one SSM Run Command to the tagged node, and the node deployed and passed the smoke test 6/6 (2 min 48 s on a new node). A commit with no image rolled back on the node and turned the run red after 15 minutes, with the API down meanwhile; the render job now asks GHCR first and refuses such a commit in 0.7 s, before the node is touched. Sending and waiting is one script, `scripts/ssm-run.sh`, which stops at the first AWS error instead of waiting it out; `deploy.yml` passed through it live in 25 s on a running node ([ADR 021](docs/decisions/021-deploy-through-ssm-run-command.md)). |
| The nightly destroy cleans up, when it runs | Live, started by hand (three times, the last through the final `ci/ephemeral-stack.sh`): the skip check passed, the destroy role removed the stack, and the cost check inside the job found nothing billable (72 checks). By schedule: the first night's 02:00 MYT run started **5 h 30 min late**, at 07:30, and passed. I had found the stack still up at 03:00 and destroyed it by hand, so the late run had nothing left to delete. GitHub's docs name the start of every hour as a busy time when scheduled runs can be delayed or dropped, so the schedule moved to 02:17 MYT; the next run started 9 h 40 min late, at 11:57, and passed. Moving off the hour didn't help: the nightly destroy is a safety net, and `make cloud-down` at the end of each session is the cleanup. |
| The budget action blocks new spend, never clean-up | IAM policy simulator with the deny policy added to the admin user: starting instances, NAT gateways or RDS is explicitly denied. Terminating, deleting, detaching the policy and reading or writing the state file stay allowed. The action's role can only attach that one policy, and only to that user and to the CI apply role `gha-tf-apply`. |
| Rolling restarts drop no requests | Continuous requests through the ingress during a rolling restart: 1,549 and 1,520 requests in two runs, 0 failures. Before the `preStop` delay was added, the same test failed 24 of 1,533 requests (502s and a timeout). |
| The node is sized by measurement | Live, with PolicyPal, External Secrets Operator and the trimmed monitoring: a 4 GiB c7i-flex.large used 75.9% of its memory idle and 76.9% at its peak, when the API restarted (Prometheus, and a once-a-second read of node_exporter). My rule was under 75%, so the default became an m7i-flex.large: 38.3% at the same peak ([ADR 024](docs/decisions/024-m7i-flex-node-for-monitoring.md)). On k3d first, the trimmed values showed three Grafana problems (an init container that never exited and was OOMKilled, plugin downloads at every start that tripped the liveness probe, a datasource with nothing behind it), all fixed before AWS. |
| PolicyPal's alert fires on AWS | Live: with the API scaled to 0, `PolicyPalApiUnavailable` was pending after 25 s and firing after 2 min 26 s, and it cleared within a minute of the API coming back. promtool tests, with the Prometheus image the chart runs: pending at 1 minute, firing at 3, never for a replica count that stays at 1, and also when the metric disappears. |
| The dashboard works on AWS | Live: Grafana's sidecar loaded "PolicyPal on AWS" from a ConfigMap in the `policypal` namespace, and every one of its 7 panels returned data, including node memory and the time since the last backup. Grafana and Prometheus are reached only with `kubectl port-forward`. |
| Backups reach S3 with no stored key | Live: the node's first backup created the restic repository in the backups bucket with the node's instance role, saved the Postgres dump and then the uploads, applied retention, pruned and checked with no errors, in 13 s. The repository password is read from Parameter Store at run time: the journal has no trace of it, and the settings file holds only the command that fetches it. The time of the success reached Prometheus through node_exporter's textfile collector. Offline: 50 tests check the role and run the script against fake `kubectl`, `restic` and `systemctl`, and 4 more run it with restic itself ([ADR 025](docs/decisions/025-restic-backups-from-the-node.md)). |
| A backup restores on a new node | Live, a disaster-recovery test: `make cloud-restore-test-before` wrote a sentinel question and policy and backed up; the stack was destroyed and rebuilt from nothing (a new instance with empty data); `make cloud-restore-test-after` found the sentinels absent, restored that run and passed: the restore reported the recorded snapshots, the question and the policy were back, and the policy file's sha256 matched, in 74 s. The smoke test passed afterwards. |
| A new node can't push out the last good backup | Found during that restore test. The rebuilt node's hourly timer was already on, and restic keeps only the newest snapshot of each day, so its first backup of empty data would have replaced the run I was about to restore; the restore finished 11 minutes before it. `tests/test_backup_retention.py` reproduces this with restic 0.19.1 itself. Now a new node's timer stays off until a successful restore or `policypal-backup enable`, and a restore turns the timer off, lets a running backup finish, and only then looks the run up (that backup's retention can delete it: found reading the script, and reproduced with restic itself too). (On a real systemd, a running one-shot service is `activating`, which `systemctl is-active` reports with the same exit code as `inactive`; the script reads the state.) 19 deliberately broken versions, all caught. Live on AWS: the timer off on a new node, on after the restore and still on after a second Ansible run; a restore started during a backup waited, then refused the run that backup had deleted with nothing touched, and `restore latest` took the newest run; the timer later ran its own hourly backup. |

## What it cost

The AWS account is on the Paid plan, so every hour of EC2, public IPv4 and EBS is billed. Each session's cost is estimated in my log from the hours the instance ran and the prices at the time (Spot or on-demand price, public IPv4, 20 GB gp3), before tax. The Billing console is the authority. S3, SSM Standard parameters, IAM, budgets and Session Manager cost nothing or a fraction of a cent here.

| Phase | What ran on AWS | Instance hours | Cost (USD, before tax) |
|---|---|---|---|
| C0 baseline and account guardrails | read-only calls | 0 | 0.00 |
| C1 persistent stack | state bucket, budgets, IAM: $0 at rest | 0 | ≈ 0.00 |
| C2 network and node | three short applies, t3.micro on-demand and Spot | 0.62 | ≈ 0.011 |
| C3 k3s through Ansible | three rebuilds, t3.small Spot | 0.72 | ≈ 0.013 |
| C4 PolicyPal | two runs, c7i-flex.large Spot | 0.71 | ≈ 0.042 |
| C5 secrets | one run, c7i-flex.large Spot | 0.55 | ≈ 0.034 |
| C6 CI/CD with OIDC | live workflow tests, including one t3.micro left overnight for the scheduled destroy | 4.16 | ≈ 0.115 |
| C7 monitoring and backups | memory sizing on c7i-flex then m7i-flex; the backup, alert and restore-test session; the live checks of the backup fixes and `deploy.yml` (m7i-flex.large on-demand) | 3.30 | ≈ 0.290 |
| **Total so far** | | **10.06** | **≈ 0.51** |

The plan estimated about USD 3.2 for the whole project on Spot. What kept it low: everything billed by the hour lives in the ephemeral stack and is destroyed at the end of every session, Spot by default ([ADR 009](docs/decisions/009-spot-and-on-demand.md)), no NAT gateway, load balancer, EKS or RDS ([ADR 010](docs/decisions/010-public-subnet-without-nat.md), [ADR 008](docs/decisions/008-k3s-on-ec2-over-eks.md), [ADR 014](docs/decisions/014-postgres-in-the-cluster.md)), and `make cost-check` after every destroy. The guards behind that: budgets that email at USD 1, 5 and 10 and attach a deny policy at USD 15 ([ADR 006](docs/decisions/006-budgets-as-code.md)), and a nightly destroy in case a session is left running.

## Security

- **No long-lived AWS keys.** I use `aws login` (short-lived credentials); GitHub Actions gets one-hour credentials through OIDC, with one role per job and exact trust conditions ([ADR 020](docs/decisions/020-one-oidc-role-per-ci-job.md)); the node uses its instance role. IAM changes only from my laptop, so no workflow can grant itself more.
- **No inbound admin port.** The security group allows port 80 from my /32 and nothing else. SSH (for Ansible) and kubectl run inside Session Manager sessions, which IAM authorises ([ADR 010](docs/decisions/010-public-subnet-without-nat.md), [ADR 013](docs/decisions/013-ansible-over-session-manager.md)). Only PolicyPal's UI is published; its API, Grafana and Prometheus stay inside the cluster ([ADR 015](docs/decisions/015-only-the-ui-is-exposed.md)).
- **Pods can't borrow the node's credentials.** IMDSv2 with hop limit 1 answers only host-network processes, and PolicyPal's namespace enforces Pod Security "baseline", which refuses host networking ([ADR 017](docs/decisions/017-imds-hop-limit-and-host-network.md)).
- **Secrets stay out of Git and Terraform state.** Terraform writes them to SSM SecureStrings through write-only arguments from ephemeral random values; External Secrets Operator copies PolicyPal's into the cluster; the backup reads restic's at run time ([ADR 016](docs/decisions/016-parameter-store-and-external-secrets.md), [ADR 023](docs/decisions/023-one-catalog-of-secrets.md)). gitleaks scans the full history in CI and staged changes before each commit.
- **Least privilege, written down.** The node role has the SSM agent's actions, `GetParameter` under `/policypal/aws/`, and the `restic/` prefix of the backups bucket. The CI roles are read-only (plan), create-and-delete with tag and instance-type conditions (apply), delete-only (nightly) and one SSM document on the tagged node (deploy).
- **One accepted finding.** The Postgres image, `pgvector/pgvector:0.8.7-pg17`, fails the image gate's settings with 26 findings (1 CRITICAL, 25 HIGH), all in `gosu`, a small helper built with Go 1.24.6 whose standard library has them. `gosu` isn't executed here: the image's entrypoint runs it only when the container starts as root, Postgres runs as uid 999, and the init container that uses the image replaces the entrypoint (verified on k3d). Not checked: whether any of them is reachable from `gosu`'s code. To revisit when pgvector publishes a rebuilt image ([ADR 014](docs/decisions/014-postgres-in-the-cluster.md)).
- **Pinned and checked.** Terraform providers, Helm charts, container images and GitHub Actions are pinned; k3s's installer, node_exporter and restic are checked against recorded sha256 sums; the homelab app's image is scanned by Trivy before it is pushed.

## What's included

### Infrastructure as code (Ansible)
- Three Ubuntu 24.04 containers stand in for VMs. They run `sshd` as PID 1 (no systemd), and your `~/.ssh/id_ed25519.pub` is mounted in at startup, so no key is committed.
- `ansible-playbook` runs from WSL over real SSH to `localhost:2201-2203`.
- Roles: `common` (packages, timezone, SSH hardening, motd) and `node_exporter` (checksum-verified download, run as a service). The role uses systemd on hosts that have it and a SysV init script on hosts that don't, such as these containers.
- The same two roles, plus `k3s`, configure the AWS node (`playbooks/aws-node.yml`, see below). `make ansible-check` runs ansible-lint and syntax checks on both playbooks.

### Application (FastAPI)
- `/`, `/healthz` (liveness), `/readyz` (checks the database), `/metrics`, CRUD on `/items`, SQLite on a PVC.
- Database endpoints run in a thread pool, so a slow query doesn't block other requests.
- Metrics are recorded in one middleware with bounded labels (`route="/items/{item_id}"`, not one series per id) and the real status code, including 500s.
- Tests use a throwaway database and include regression tests for concurrency, 500 counting, label cardinality and test isolation.
- Multi-stage image, non-root user, `HEALTHCHECK`, no pip/setuptools/wheel in the runtime image.

### Kubernetes (k3d)
- k3s in Docker: 1 server, 2 agents, Traefik ingress, local-path storage.
- Deployment with liveness/readiness probes, resource limits, read-only root filesystem, dropped capabilities.
- Rolling restarts with no dropped requests: a new pod must be Ready before the old one stops, and the old pod keeps serving for 10 seconds (`preStop` sleep) while Traefik stops routing to it.
- kustomize overlays: `dev` (deployed locally) and `prod` (3 replicas, pinned GHCR image tag; a template, not deployed anywhere).

### Monitoring (kube-prometheus-stack)
- Helm chart pinned to 92.16.0 (`k8s/platform/kube-prometheus-stack/chart.env`), scraping the app (ServiceMonitor) and the three nodes' node_exporter.
- Alert rules: AppDown (scrape failing or no pods at all), HighErrorRate (5xx share of requests above 5%), HighLatency (p95 above 0.5 s), NodeExporterDown. Each has promtool unit tests except NodeExporterDown, run with the Prometheus image the chart runs (a test keeps the two the same).
- Grafana dashboard loaded automatically: request rate by route and status, p95/p99 latency, and success / client error / server error shares.
- On AWS the same chart runs with its own values (one day in memory, no Alertmanager, node_exporter scraped from the node itself, every container sized), installed by `make cloud-addons` through `scripts/addon.sh` ([ADR 022](docs/decisions/022-one-addon-script-for-every-chart.md)). PolicyPal ships one alert, PolicyPalApiUnavailable (no available API replica, or no count for it, for 2 minutes; promtool-tested), and a Grafana dashboard. On AWS the alert fired 2 min 26 s after the API was scaled to 0.

### Backup and recovery (restic)
- Daily CronJob. An init container takes a SQLite online backup (safe while the app is writing) and checks it; restic 0.19.1 then backs up that copy, runs `restic check`, and applies 7 daily / 4 weekly / 12 monthly retention.
- `make restore-test` proves the round trip end to end (see above).
- On AWS, restic runs on the node itself as an hourly systemd timer, installed by Ansible (role `restic_backup`), because a pod that could reach both the uploads volume and the node's AWS credentials would break the Pod Security rule above ([ADR 025](docs/decisions/025-restic-backups-from-the-node.md)). Each run saves the Postgres dump, then the uploads, to the backups bucket, then applies 7 daily / 4 weekly / 12 monthly retention, prunes and checks. The bucket is versioned, encrypted, TLS-only and never expires current objects: only restic deletes, because a restic data file is shared by later snapshots.
- `policypal-backup restore RUN` brings a run back on any node; `make cloud-restore-test-before` / `-after` prove it across a destroy and rebuild. A new node's backups stay off until a restore or `policypal-backup enable`, so it can't replace the last good backup with its empty data.

### AWS (Terraform)
The homelab is extended to AWS (ap-southeast-1) with two Terraform stacks. The **persistent** stack (`terraform/persistent`) holds only resources that cost about $0 at rest. The **ephemeral** stack (`terraform/envs/aws`, below) holds everything billed by the hour and is destroyed after every session.
- **State.** Terraform's record of what it manages lives in an S3 bucket created by this stack:
  - versioned, so a bad write can be rolled back
  - encrypted with SSE-S3
  - all public access blocked, and every request without TLS denied
  - old versions expire after 90 days
  - Terraform is not allowed to delete it
- **Bootstrap.** The bucket was first created with local state, then the state was moved into it with `terraform init -migrate-state`.
- **Locking.** `use_lockfile = true` makes Terraform create a `.tflock` object next to the state before it writes. A second run can't take the lock and stops instead of overwriting the state. No DynamoDB table is needed.
- **Budgets.**
  - `zero-spend` emails on the first cent of the month. It was created in the console and adopted with an `import` block, not recreated.
  - `monthly-cap` emails at USD 1, 5 and 10.
  - At USD 15 an automatic budget action attaches a deny policy to the admin user and to the CI apply role that blocks starting anything billable. It never blocks deleting, IAM or state writes, so a clean-up still works.
  - Budgets only stop *new* spend, and their data lags by hours, so destroying the hourly stack after every session stays the main guard.
- **What the node needs.** The persistent stack also holds the node's IAM role and instance profile, the SSM parameter `/devops-platform/owner-cidr` with my IP, PolicyPal's secrets and the backup's password in SSM (below), and the backups bucket. All of it is free, and keeping IAM here means creating or destroying the node never needs IAM permissions.
- **Checks.** `make tf-check` runs `terraform fmt`, `validate`, `terraform test` (mock provider, no AWS calls), tflint and trivy. CI runs it on every push.

See [ADR 005](docs/decisions/005-terraform-state-in-s3.md), [ADR 006](docs/decisions/006-budgets-as-code.md) and [ADR 007](docs/decisions/007-anomaly-detection-in-console.md).

#### The ephemeral stack: one node, created per session
`terraform/envs/aws` holds everything billed by the hour. `make cloud-up` creates it at the start of a session and `make cloud-down` destroys it at the end, then runs `make cost-check`.
- **Network** (`terraform/modules/network`):
  - one VPC with one public subnet, an internet gateway and a default route
  - no NAT gateway, Elastic IP or VPC endpoints, because each costs more per hour than the node
- **Node** (`terraform/modules/compute`), including everything about how it is reached:
  - security group: TCP 80 from my /32 only, nothing on 22 or 6443, outbound 443 and 80 only ([ADR 011](docs/decisions/011-node-owns-its-security-group.md))
  - Ubuntu 24.04 amd64 from Canonical; the instance type must be on the allowlist and support x86_64
  - IMDSv2 only, with hop limit 1
  - encrypted 20 GB gp3 root volume, deleted with the instance
  - standard CPU credits, so a busy T instance slows down instead of billing extra
  - on-demand or Spot from one variable
- **Access:** no SSH from the internet. The SSM agent on the node connects out to AWS, so `aws ssm start-session` gives a shell with no inbound port open. Its role allows only the 11 actions the agent needs; the usual managed policy would also let the node read every SSM parameter.
- **Your IP:** `make cloud-up` first stores this machine's public IP as a /32 (`make owner-ip` does only that step), so every session starts with your current IP. If no real IP is stored, the plan fails rather than open the port to a made-up address.

Cost while the stack exists (ap-southeast-1, before tax). Nothing is billed once it is destroyed.

| Node | On-demand | Spot (typical) |
|---|---|---|
| t3.micro + public IPv4 + 20 GB gp3 | USD 0.021/h | USD 0.012/h |
| t3.small + public IPv4 + 20 GB gp3 | USD 0.034/h | USD 0.019/h |
| c7i-flex.large + public IPv4 + 20 GB gp3 | USD 0.105/h | USD 0.061/h |
| m7i-flex.large + public IPv4 + 20 GB gp3 (default from C7) | USD 0.127/h | USD 0.060/h |

See [ADR 008](docs/decisions/008-k3s-on-ec2-over-eks.md) (k3s on EC2, not EKS), [ADR 009](docs/decisions/009-spot-and-on-demand.md) (Spot and on-demand), [ADR 010](docs/decisions/010-public-subnet-without-nat.md) (network and access) and the live results in [docs/aws-test-log.md](docs/aws-test-log.md).

#### k3s on the node, configured by Ansible
`make cloud-config` runs `ansible/playbooks/aws-node.yml` against the running node: the homelab's `common` and `node_exporter` roles, then a `k3s` role.
- **Connection:** the inventory is generated from `terraform output` and names the host by its instance ID. SSH runs inside a Session Manager session to that ID, so port 22 stays closed: IAM decides who may open the session, and my SSH key logs in as `ubuntu` ([ADR 013](docs/decisions/013-ansible-over-session-manager.md)). Pipelining cuts a run with nothing to change from 151 SSH calls to 39; because SSH already reuses one connection, that saves only about 10% of the time (35–39 s against 39–44 s, back to back).
- **k3s:** a single-server k3s pinned to v1.36.6+k3s1 with its bundled Traefik, ServiceLB, local-path storage and metrics-server. The install script is fetched for that release tag and checked against a recorded sha256; the script then checks the k3s binary. Secrets are encrypted at rest, and the admin kubeconfig on the node is readable by root only.
- **kubectl from the laptop:** after the roles, the playbook copies the kubeconfig to `~/.kube/devops-aws.yaml` (mode 600, in a `~/.kube` created 700 if missing), pointing at `localhost:16443`. The tunnel port and the path sit in `ansible/inventory/group_vars/aws.yml` with the other Session Manager settings, so the `k3s` role only ever touches the node. `make cloud-tunnel` forwards that port to the node's 6443 through Session Manager, so the Kubernetes API is never public.
- **Same roles as the homelab:** `PermitRootLogin` and the login banner became variables. The homelab keeps root login with keys; the AWS node turns root login off.
- **One command:** `make cloud-rebuild` (cloud-up, then cloud-config) goes from nothing to a Ready node in about three minutes.

#### PolicyPal on the node
[PolicyPal](https://github.com/toolikheng/policypal-hr-rag), my HR-policy question-answering app, runs on the k3s node from its public GHCR image, with no changes to its repository.
- **Manifests** (`k8s/policypal/`): one kustomize base and two overlays, `local` (k3d, tested first) and `aws`, which deploy the same objects and pin the same image commit.
  - **Postgres + pgvector** as a one-replica StatefulSet with a 5Gi volume, not RDS ([ADR 014](docs/decisions/014-postgres-in-the-cluster.md)).
  - **The API**: one replica, replaced with `Recreate` (one process owns the upload lock and the uploads volume). `LLM_PROVIDER=extractive`: no LLM on AWS, answers quote the policy text. A startup probe allows six minutes for migrations, ingestion and the embedding model; `/health` is used for readiness only, because it returns 503 whenever the database is down.
  - **The UI** (Streamlit) is the only thing behind Traefik. The API is ClusterIP, so its Swagger page and admin endpoints can't be reached from outside ([ADR 015](docs/decisions/015-only-the-ui-is-exposed.md)).
  - Pods run as non-root with no capabilities and no service account token. Requests and limits come from measurements (API 1016 MiB peak on k3d, 827 MiB on AWS).
- **Secrets**: the manifests only reference `policypal-db` (the Postgres password; the API builds its database URL from it) and `policypal-admin`. On AWS, External Secrets Operator makes them from SSM Parameter Store (below); on k3d, which has no AWS identity, `scripts/policypal-secrets.sh` creates random local values once.
- **Deploy** (`make policypal-local`, `make cloud-deploy`): render with the pinned kustomize the checks use, apply, wait up to 2 minutes for the Secrets, wait for every rollout, then the smoke test. If anything fails, each Deployment this deploy changed goes back to its previous revision.
- **Smoke test** (`scripts/smoke-test.sh`): `/health`, one answered and cited question and one refusal from PolicyPal's golden set, through `kubectl port-forward` to the API; then the UI through the ingress, and that the API's pages are not reachable there.
- **Sizing**: with PolicyPal running, the node uses about 1.75 GiB, more than a t3.small has, so the node was a c7i-flex.large (4 GiB). With External Secrets Operator and the monitoring added in C7, that node peaked at 76.9% of its memory when the API restarted, above the 75% I allow, so the default is now an m7i-flex.large (8 GiB, 38% at the same peak; [ADR 024](docs/decisions/024-m7i-flex-node-for-monitoring.md)).

#### Secrets: Parameter Store and External Secrets Operator
- **Where they live:** `/policypal/aws/db-password`, `/policypal/aws/admin-token` and `/policypal/aws/restic-password` (read by the node's backup, not by a pod), SSM SecureStrings (Standard tier, AWS managed key `aws/ssm`: free), in the persistent stack. One catalog, `terraform/persistent/secret-catalog.yaml`, lists each one and who reads it ([ADR 023](docs/decisions/023-one-catalog-of-secrets.md)).
- **How they're made:** Terraform generates each value with an ephemeral `random_password` and writes it through a write-only argument, so it's in neither the plan nor the state. Only a version number is stored; raising it in a commit rotates the secret. `make check-state-secrets` reads each live value and counts its copies in the state, printing only the counts.
- **How they reach the pods:** External Secrets Operator (`make cloud-addons`, pinned chart) reads them with the node's instance role through a `ClusterSecretStore` that has no credentials of its own and serves only the `policypal` namespace. Two `ExternalSecret`s make the Secrets and refresh them hourly. The node role may only `GetParameter` under `/policypal/aws/`. See [ADR 016](docs/decisions/016-parameter-store-and-external-secrets.md).
- **Who can reach the node's credentials:** IMDSv2 with hop limit 1 answers only processes on the host network, so only ESO's controller runs there. PolicyPal's namespace enforces Pod Security "baseline", which refuses host-network pods ([ADR 017](docs/decisions/017-imds-hop-limit-and-host-network.md)).
- **Secret scanning:** gitleaks with its default rules plus one for passwords assigned as plain text, run on the whole history in CI and weekly, and on staged changes by a pre-commit hook.

### CI/CD (GitHub Actions)
- On every push and pull request: lint, tests, alert-rule tests, hadolint, repo-level tests, ShellCheck, the Kubernetes checks (kustomize + kubeconform), the Ansible checks and the offline Terraform checks, then build and Trivy scan.
- Secret scan (`secret-scan.yml`): the full history with the repo's gitleaks rules on every pull request, every Monday and on demand; read-only, no secrets.
- On master: the image is pushed to GHCR as `master` and `sha-<commit>`. Actions are pinned to current major versions; trivy-action is pinned to a commit SHA.
- Manual CD with an ephemeral k3d cluster and a smoke test.
- For AWS, each job gets one-hour credentials for its own role through GitHub OIDC; no AWS key is stored ([ADR 020](docs/decisions/020-one-oidc-role-per-ci-job.md)):
  - `terraform.yml`: on pull requests touching `terraform/`, a read-only plan posted as a comment (addresses and actions only); on demand, apply or destroy of the ephemeral stack after my approval, applying the saved plan only if the change list's hash matches the one I approved.
  - `deploy.yml`: a PolicyPal commit, checked on GHCR, rendered and validated with no AWS access, then deployed on the node through SSM Run Command; the node rolls back and the run fails if the smoke test fails ([ADR 021](docs/decisions/021-deploy-through-ssm-run-command.md)).
  - `nightly-destroy.yml`: destroys the ephemeral stack at 02:17 MYT unless `make skip-nightly-destroy` set a skip, then runs the cost check.

## Design decisions

Each significant choice has a decision record in [docs/decisions/](docs/decisions/): the context, what I chose, the alternatives and the consequences.

| ADR | Decision |
|---|---|
| [001](docs/decisions/001-docker-nodes-over-multipass.md) | Docker containers instead of Multipass VMs |
| [002](docs/decisions/002-k3d-over-kind.md) | k3d instead of kind |
| [003](docs/decisions/003-ansible-over-terraform.md) | Ansible for node configuration, Terraform deferred |
| [004](docs/decisions/004-pull-based-cd.md) | Manual CD to a throwaway cluster |
| [005](docs/decisions/005-terraform-state-in-s3.md) | Terraform state in S3 with native locking, split into persistent and ephemeral stacks |
| [006](docs/decisions/006-budgets-as-code.md) | Budgets as code, with an automatic deny action at the monthly cap |
| [007](docs/decisions/007-anomaly-detection-in-console.md) | Cost Anomaly Detection stays in the console, not in Terraform |
| [008](docs/decisions/008-k3s-on-ec2-over-eks.md) | k3s on one EC2 instance instead of EKS |
| [009](docs/decisions/009-spot-and-on-demand.md) | Spot by default, on-demand when an interruption would waste my time |
| [010](docs/decisions/010-public-subnet-without-nat.md) | A public subnet with an internet gateway; no NAT gateway, Elastic IP or VPC endpoints; access only through Session Manager |
| [011](docs/decisions/011-node-owns-its-security-group.md) | The compute module owns the node's security group |
| [012](docs/decisions/012-keep-the-terraform-modules.md) | Keep the network and compute modules instead of one flat ephemeral stack |
| [013](docs/decisions/013-ansible-over-session-manager.md) | Ansible reaches the AWS node with SSH inside Session Manager |
| [014](docs/decisions/014-postgres-in-the-cluster.md) | PolicyPal's Postgres runs in the cluster, not on RDS |
| [015](docs/decisions/015-only-the-ui-is-exposed.md) | Only PolicyPal's UI is exposed; the API stays inside the cluster |
| [016](docs/decisions/016-parameter-store-and-external-secrets.md) | PolicyPal's secrets live in SSM Parameter Store, written by Terraform without entering its state, and External Secrets Operator copies them into the cluster |
| [017](docs/decisions/017-imds-hop-limit-and-host-network.md) | The node keeps IMDSv2 hop limit 1; only External Secrets Operator's controller runs on the host network, and the policypal namespace forbids host networking |
| [018](docs/decisions/018-one-fake-cli-module-for-script-tests.md) | One fake-CLI module for the script tests; the cost check keeps its own |
| [019](docs/decisions/019-shared-tool-versions.md) | Tool versions that several scripts use are pinned once, in ci/versions.env |
| [020](docs/decisions/020-one-oidc-role-per-ci-job.md) | GitHub Actions reaches AWS through OIDC with one role per job, and IAM changes only from my laptop |
| [021](docs/decisions/021-deploy-through-ssm-run-command.md) | GitHub Actions deploys PolicyPal through SSM Run Command, with the YAML CI checked |
| [022](docs/decisions/022-one-addon-script-for-every-chart.md) | One add-on script installs and renders every Helm chart on the cluster |
| [023](docs/decisions/023-one-catalog-of-secrets.md) | One catalog lists every secret and who reads it |
| [024](docs/decisions/024-m7i-flex-node-for-monitoring.md) | The node is an m7i-flex.large once the monitoring runs on it |
| [025](docs/decisions/025-restic-backups-from-the-node.md) | PolicyPal is backed up to S3 by restic on the node, and only restic deletes backups |

## Local stand-ins vs production

| Component | In this project | In production |
|-----------|-----------------|---------------|
| Compute | Docker containers with sshd, no systemd | VMs (Proxmox, cloud) or bare metal |
| Kubernetes | k3d (k3s in Docker) on one machine | k3s/Kubernetes on real nodes |
| Registry | GHCR | GHCR or a private registry |
| Backup target | restic repository on a local PVC | Object storage (S3, B2, ...) |
| Ingress | Plain HTTP through Traefik on `localhost:8080` | TLS with cert-manager and a real domain |

The roles, manifests and monitoring config are written so that moving to real machines means changing the inventory and Helm values, not rewriting them. That move has not been tested.

## Day-to-day commands

```bash
make test / make lint      # pytest; ruff + mypy (app/.venv is created on first use)
make run-app               # the app locally on http://localhost:8000 (API docs at /docs)
make build                 # build the image, hadolint, Trivy gate (both run as pinned Docker images)
make deploy                # build, import into k3d, apply the dev overlay, roll out
make provision             # re-run Ansible against the nodes
make backup                # run the backup CronJob now and print its logs
make test-infra            # repo-level tests in tests/ (tests/.venv is created on first use)
bash ci/shellcheck.sh      # ShellCheck on every shell script (pinned Docker image)
bash ci/gitleaks.sh        # scan the full git history for secrets, values redacted (pinned Docker image); --staged: only staged changes
pre-commit install         # once per clone: gitleaks scans what you stage before every commit (pipx install pre-commit==4.7.0)
make tf-check              # offline Terraform checks: fmt, validate, terraform test, tflint, trivy
make cost-check            # AWS: read-only scan for billable leftovers (needs `aws login --profile devops`)
make owner-ip              # AWS: store this machine's public IP (/32) for the node's security group
make cloud-up              # AWS: store your IP, then create the ephemeral stack after Terraform shows the plan and you type yes (billed hourly)
make cloud-down            # AWS: destroy it (also asks first), then run cost-check
make cloud-config          # AWS: configure the node with Ansible over Session Manager (common, node_exporter, k3s)
make cloud-tunnel          # AWS: forward the node's Kubernetes API to localhost:16443 (leave it open; Ctrl-C closes it)
make cloud-rebuild         # AWS: cloud-up, then cloud-config
make ansible-check         # ansible-lint and syntax checks on both playbooks (offline)
make k8s-check             # render both PolicyPal overlays (pinned kustomize) and every add-on chart per environment (pinned Helm); kubeconform -strict
make policypal-local       # PolicyPal on k3d: random Secrets (once), deploy, smoke test -> http://policypal.localhost:8080
make cloud-addons          # AWS: External Secrets Operator, then the monitoring (once per node; needs make cloud-tunnel running)
make cloud-deploy          # AWS: deploy PolicyPal (ESO makes its Secrets) and smoke-test it; rolls back if the smoke test fails
make cloud-smoke           # AWS: only the smoke test
make check-state-secrets   # AWS, read-only: count each live secret value in the Terraform state (must be 0)
make cloud-restore-test-before   # AWS: sentinels, a backup, the run recorded outside the repo (then cloud-down, rebuild, addons, deploy)
make cloud-restore-test-after    # AWS, on the new node: restore that run and check the sentinels came back
make skip-nightly-destroy  # GitHub setting: hold off the 02:17 MYT nightly destroy for 8 hours (UNTIL=... for longer, up to 7 days)
gh workflow run terraform.yml -f action=apply -f instance_type=t3.micro -f use_spot=true   # waits for my approval
gh workflow run deploy.yml -f policypal_tag=<PolicyPal commit>   # GHCR check, render, deploy on the node
```

The node is an m7i-flex.large on Spot by default (with the monitoring, a 4 GiB c7i-flex.large peaked at 76.9% of its memory: [ADR 024](docs/decisions/024-m7i-flex-node-for-monitoring.md)); `make cloud-up CLOUD_ARGS='-var use_spot=false'` starts it on-demand. With the tunnel open: `KUBECONFIG=~/.kube/devops-aws.yaml kubectl get nodes`. A shell on the node: `aws ssm start-session --target "$(cd terraform/envs/aws && terraform output -raw instance_id)"`.

Grafana and Prometheus are reached with a port-forward:

```bash
kubectl -n monitoring port-forward svc/kube-prometheus-stack-grafana 3000:80        # http://localhost:3000/d/devops-app-metrics
kubectl -n monitoring get secret grafana-admin -o jsonpath='{.data.admin-password}' | base64 -d; echo   # user: admin
kubectl -n monitoring port-forward svc/kube-prometheus-stack-prometheus 9090:9090   # http://localhost:9090
```

On AWS (with `KUBECONFIG=~/.kube/devops-aws.yaml` and the tunnel open) the dashboard is `http://localhost:3000/d/policypal`, and the password is in the Secret `kube-prometheus-stack-grafana`, made by the chart on the node: see [Monitoring on AWS](docs/runbook.md#monitoring-on-aws). Backups on the node: [Backups on AWS](docs/runbook.md#backups-on-aws-restic-to-s3).

## Requirements

- WSL2 Ubuntu 24.04 with Docker Desktop (WSL integration on). Developed and tested on a machine with 8 CPUs and 11 GB RAM.
- `git`, `make`, `curl`, `jq`, `openssl`, `ansible`, `python3` with the venv module, `docker`, `kubectl`, `k3d`, `helm`, and an SSH key at `~/.ssh/id_ed25519`. `make check` lists what's missing; `make install` apt-installs the packages it can (with sudo), creates the SSH key, and links to installers for the rest.
- hadolint, Trivy, promtool, yq, ShellCheck, gitleaks, tflint, kustomize and kubeconform don't need installing: they run as pinned Docker images.
- For the AWS work: AWS CLI v2.32 or newer (for `aws login`), the Session Manager plugin, and Terraform 1.11 or newer (write-only arguments). `make ansible-check` needs ansible-lint (`pipx install ansible-lint`). The pre-commit hook needs pre-commit (`pipx install pre-commit==4.7.0`). The GitHub CLI (`gh`) runs the workflows and `make skip-nightly-destroy`.
- Host port 8080 free (the k3d load balancer).

## Project structure

- `terraform/` — AWS infrastructure: `persistent/` (state bucket, budgets, IAM, the node's role, the owner-IP parameter, PolicyPal's secrets, and GitHub's OIDC provider with the four CI roles), `envs/aws/` (the ephemeral stack) and `modules/` (`network`, `compute`, and `names`: the names both stacks share), with tests in each directory's `tests/`
- `ansible/` — inventories (`docker-nodes.yml`; `aws.yml` is generated per session), playbooks (`site.yml` for the homelab, `aws-node.yml` for EC2), roles `common`, `node_exporter`, `k3s` and `restic_backup` (the AWS node's backups), and `requirements.yml` for the one collection used
- `infra/nodes/` — node image, startup script, Docker Compose file
- `app/` — FastAPI service, tests, Dockerfile
- `k8s/` — the v1 app's kustomize base and `dev` / `prod` overlays; `k8s/policypal/` — PolicyPal's base (namespace, Postgres, API, UI, ingress) and `local` / `aws` overlays (`aws` adds the ExternalSecrets, the alert and the dashboard); `k8s/platform/` — the platform add-ons, each a pinned Helm chart (`chart.env`) with one values file per environment it runs in: External Secrets Operator (AWS, plus its ClusterSecretStore) and kube-prometheus-stack (k3d, and trimmed values for AWS)
- `monitoring/` — what is monitored: ServiceMonitor, alert rules and their tests, dashboard
- `backup/` — the homelab's backup CronJob, restore Job and restore test (the AWS node's backup is the Ansible role `restic_backup`)
- `ci/` — scripts shared by the Makefile and GitHub Actions (lint, test, rule tests, build, scan, ShellCheck, gitleaks, Terraform, Kubernetes and Ansible checks), and those of the AWS workflows: `ephemeral-stack.sh` (every Terraform run they make), `plan-summary.sh`, `nightly-skip.sh`, `deploy-render.sh`, `deploy-over-ssm.sh`, `kubeconform.sh`
- `scripts/` — toolchain check (`bootstrap.sh`), the read-only AWS cost check (`cost-check.sh`), `owner-ip.sh`, `ephemeral-tf.sh` (the Terraform run behind `make cloud-up` / `cloud-down`), `node-id.sh` and `aws-inventory.sh` (behind `make cloud-config`), `cloud-tunnel.sh`, and for PolicyPal `render-k8s.sh`, `policypal-secrets.sh` (k3d only), `deploy-policypal.sh`, `smoke-test.sh` and `node-ip.sh`; `addon.sh` (installs or renders a platform add-on: `make cloud-addons`, `make monitoring-up`, the checks), for secrets `check-state-secrets.sh`; `restore-test.sh` (the restore test across a rebuild), `ssm-run.sh` (one command on the node through SSM Run Command, for the restore test and `deploy.yml`); for CI on AWS `node-deploy.sh` (runs on the node) and `skip-nightly-destroy.sh`
- `tests/` — repo-level tests: the cost check (against its own fake `aws` CLI), the AWS session Make targets and the PolicyPal, ESO and state-check scripts and the AWS workflows' scripts (against the fake CLIs in `tests/fakecli.py`: `kubectl`, `curl`, `docker`, `k3d`, `helm`, `terraform`, `aws`, `ansible-playbook`, `gh`, `restic`, `systemctl`), the workflows' roles checked against the Terraform trust policies, the rules for the AWS node's Ansible code, the backup role and script (fake `restic` and `systemctl`), restic's retention with restic itself (pinned image), the rules for the rendered PolicyPal manifests and the rendered ESO chart, Terraform's secret handling, the gitleaks rule and hook, the workflows, the scripts' copies of shared Terraform names, the shared tool versions (`ci/versions.env`), and the `.gitignore` rules
- `.gitleaks.toml`, `.gitleaksignore`, `.pre-commit-config.yaml` — secret scanning: the extra rule, the two known findings, the pre-commit hook
- `.github/workflows/` — CI on every push, the secret scan, manual CD, and for AWS `terraform.yml`, `deploy.yml` and `nightly-destroy.yml`
- `docs/` — architecture, decisions, runbook, the AWS test log, the demo script, fresh-clone test log

## Limitations

- **`make deploy` always restarts the app.** The dev image tag is always `latest`, so the deployment is restarted on every deploy to pick up the new image, even when nothing changed. The restart drops no requests, but it does take about 25 seconds.
- **The homelab's backups live on one node's disk.** Its restic repository is a local PVC with no off-site copy, so losing that disk loses the backups. (The AWS node's go to S3.)
- **One snapshot per day.** Retention keeps the newest snapshot of each day, in both environments, so a manual backup (or `make restore-test`) replaces that day's scheduled one. On AWS this is why a new node's backups wait for a restore or `policypal-backup enable`.
- **SQLite means one replica.** The dev overlay runs a single pod; scaling out needs a different database.
- **`make restore-test` changes the dev database.** It wipes and restores `/data/app.db`, and leaves its test items behind. Run it only against a dev cluster.
- **The AWS node is one instance, and its data lasts one session.** There is no high availability; a Spot interruption takes it away until `make cloud-rebuild` and `make cloud-deploy` (about four minutes). PolicyPal's Postgres and uploads live on the node's disk ([ADR 014](docs/decisions/014-postgres-in-the-cluster.md)); the hourly backups to S3 bring them back on a new node, so up to an hour of changes can be lost, and everything since the node started if its backups were never turned on.
- **Backups and restores need a person on every new node.** Hourly backups start only after `policypal-backup restore` or `policypal-backup enable`, run in a shell on the node. A failed restore leaves the API stopped on purpose, until someone looks.
- **The AWS alert notifies no one.** There is no Alertmanager on the node (to save memory), so the alert is visible in Prometheus and on the Grafana dashboard only. Prometheus keeps one day of data on the node's disk, which goes with the node at `make cloud-down`.
- **Everything on the node's host network shares one AWS role.** k3s, the SSM agent and External Secrets Operator's controller all get the node role's credentials; on EKS each pod could have its own. The role is small (the SSM agent's actions and reading `/policypal/aws/*`), and PolicyPal's namespace refuses host-network pods ([ADR 017](docs/decisions/017-imds-hop-limit-and-host-network.md)).
- **A rotated secret needs a restart.** ESO updates the Secret within the hour (or at once with a forced sync), but the API reads it only when it starts. Rotating the database password also needs `ALTER ROLE` in Postgres first ([runbook](docs/runbook.md)).
- **A PolicyPal deploy stops the API first.** It is one process with one uploads volume (`Recreate`), so the API is briefly down on every change, and stays down until the rollback if the new version fails. The UI keeps running.
- **The nightly destroy depends on GitHub's scheduler.** Scheduled runs can start hours late (5 h 30 min and 9 h 40 min so far) or be skipped, and GitHub disables schedules after 60 days without repo activity. The budget alerts and `make cost-check` stay as the backup. A late run can also destroy a stack I'm using in the morning: that's accepted (a rebuild takes about four minutes), and `make skip-nightly-destroy` protects a planned session (8 hours, or `UNTIL=` for longer).
- **Admins can bypass the approval on `aws-apply`.** The environments keep GitHub's default `can_admins_bypass`; I'm the only admin, so it changes nothing here, but a team would turn it off.
- **The chat widget doesn't work on AWS.** It calls the API from the browser, and only the UI is published ([ADR 015](docs/decisions/015-only-the-ui-is-exposed.md)).
- **`--check` can't preview a fresh AWS node.** Check mode stops at the first task that needs an earlier change (it only pretends to create directories). On a configured node it works and shows drift.
- **The prod overlay is a template.** It renders and points at an image CI really pushes, but it isn't deployed anywhere, and neither is anything outside this one machine.

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `make check` says the Docker daemon isn't reachable | Start Docker Desktop and enable WSL integration for this distro |
| `curl localhost:8080` returns `404 page not found` | The ingress matches on host: add `-H "Host: devops-app.localhost"` |
| Ansible can't connect to the nodes | Run `make infra-up`; the nodes need `~/.ssh/id_ed25519.pub`, and `make check` verifies the key exists |
| `make cluster-up` fails with "port is already allocated" | Something else uses host port 8080 |

More in [docs/runbook.md](docs/runbook.md).

## What I learned

- Proving a check fails before trusting it matters. Several gates and tests in this repo used to pass no matter what: a linter with `--exit-zero`, a scan with exit code 0, a restore test that compared counts. Each was only fixed after making it fail on purpose.
- Idempotency has to be designed in: downloading to `/tmp` and deleting the file afterwards meant every run reported changes until the role kept a versioned copy.
- In Prometheus, the shape of a query matters as much as the metric. A ratio without `sum()` divides each series by itself, and an alert on `up == 0` stays silent when the target disappears entirely.
- A backup only counts once a restore has been shown to bring the data back, and shown to fail when it doesn't.
- "Zero-downtime rolling updates" needs measuring, not assuming. Kubernetes sends SIGTERM at the same moment it starts removing the pod from the Service, and the ingress takes a moment to notice; a short `preStop` sleep closes that gap.
- A test path needs testing too. My first PolicyPal smoke test called the API through the Kubernetes API server with `kubectl create --raw`, which looked neat and failed on the first real run: kubectl sends no `Content-Type`, so FastAPI rejected every question. `kubectl -v=9` showed it in a minute; running on k3d first meant it cost nothing.
- A fake in a test only knows what I tell it. My deploy waited for two Secrets with one `kubectl wait --for=create` call, and the fake kubectl was happy with that. On k3d the real kubectl gave up in 0.4 seconds: with more than one resource it doesn't wait at all. The fake now behaves like the real tool, and the script waits for each Secret on its own.
- Some defaults only show up on a real host. External Secrets Operator's controller runs on the node's network; on the node it held a second port nobody had written down, and the first Helm upgrade hung because a rolling update needs two copies of a pod holding the same host ports. Both came from looking at the node itself (`ss -ltnp`, the scheduler's events), not from the chart's docs.

- An offline check is only as good as the facts I give it. My trust policies matched the repo by name, the tests agreed with my assumption, and the IAM simulator checks permissions, not trust: only a real token showed this repo uses GitHub's immutable subject, and CloudTrail showed it word for word. Likewise the simulator passed my image rule because I fed it the owner I expected; `decode-authorization-message` on the real refusal showed AWS lists Canonical's images as `amazon`.
- The rollback worked, and still cost 15 minutes without the API. Checking that the image exists before touching the node turns that into a 0.7-second refusal; the rollback stays for images that exist but don't work.
- Measure before choosing, and decide the line first. My plan put the monitoring at 0.6–0.9 GB; the homelab's values used 1.3 GB on k3d, and the 4 GiB AWS node sat at 75.9% of its memory idle and peaked at 76.9% when the API restarted. Because I had set the limit (75%) before measuring, the instance type followed from the numbers, not from a feeling.
- A passing restore test can still teach something. The first one on AWS passed, and the timing around it showed that the new node's own hourly backup would have replaced the snapshot I was restoring within the hour. `restic forget --dry-run` explained why, and a test with restic itself now reproduces it.
- Check what a command returns before scripting it. `systemctl is-active` reports a running one-shot service with the same exit code as a stopped one; only the printed state tells them apart, so the obvious `--quiet` check would never have waited.
- A cache can undo a security step. The image runs `apt-get upgrade` to pick up Debian's fixes, but the CI cache kept serving that layer from before a `libzstd1` fix existed, so the gate failed on an unchanged commit, and master's published image had the same package. The upgrade stage is now never cached, and a test checks both builds.
- A provider's code can matter more than its docs. Changing only the description of a parameter with a write-only value would have sent an empty value with it; the plan showed the version changing, and reading the provider's update function showed why ([ADR 023](docs/decisions/023-one-catalog-of-secrets.md)).

## Next steps

- Notifications for the AWS alert (Alertmanager, or a small webhook to email or chat).
- A smaller PolicyPal image. It is 655 MB to download and about 2.1 GB unpacked (it includes PyTorch), and on k3d each node pulls it separately: 9 min 18 s and 21 min 16 s on the last run, so a new cluster's first deploy can pass the UI's 15-minute rollout deadline and needs running again once the pull ends. A slimmer image, or loading it into k3d once instead of pulling it on every node, would fix that.
- The homelab's backups to object storage, as the AWS node's already go.
- TLS with a domain and cert-manager, if the AWS environment ever needs a public demo.
- Loki for logs.
- Pull-based CD into a long-lived cluster.

## License

MIT. See [LICENSE](LICENSE).
