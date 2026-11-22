# Self-Hosted Homelab DevOps Platform

[![CI](https://github.com/toolikheng/devops-platform/actions/workflows/ci.yml/badge.svg?branch=master)](https://github.com/toolikheng/devops-platform/actions/workflows/ci.yml)

A portfolio project that runs a small platform end to end on one WSL2 machine: Ansible provisions three Docker "nodes", a FastAPI service is built, scanned and deployed to a k3d (k3s) cluster, Prometheus and Grafana monitor it, restic backs it up, and GitHub Actions gates every push.

Every claim in the [What's verified](#whats-verified) table was checked by running it. Gaps are listed under [Limitations](#limitations).

## Quick start

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
| AWS cost check is read-only and fails safe | `make cost-check` lists anything billable left in the account: the full list in ap-southeast-1 (instances, volumes, snapshots, Elastic IPs, public IPv4, NAT gateways, VPC endpoints, load balancers, EKS, RDS, Secrets Manager, open or active Spot requests, Route 53 hosted zones, ECR repositories, Lightsail instances and databases, SageMaker notebooks and endpoints, customer-managed KMS keys) plus instances, volumes and Elastic IPs in every other enabled region. 38 tests run it against a fake `aws` CLI: each kind of leftover is reported, only describe/list/get calls are made, the Cost Explorer API is never called, and a check that can't run is an error, never a clean result. Run against the real account after every session: clean, and it ignored the two cancelled one-time Spot requests and the free AWS-managed `aws/ebs` key. |
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
| A failed deploy rolls back | On k3d: a deploy with an image tag that doesn't exist timed out, both Deployments went back to their previous revision, and the smoke test passed again. 59 tests run the deploy, smoke-test, Secrets, ESO and state-check scripts against fake `kubectl`, `curl`, `docker`, `helm`, `terraform` and `aws`: every failing check fails the smoke test, the smoke test stops if its own port-forward doesn't start (so it can never test some other cluster), only Deployments this deploy changed are rolled back, every namespaced `kubectl` call targets `policypal`, the Secrets created hold exactly the keys the manifests reference, and Secret values never appear in arguments or output. |
| The PolicyPal manifests follow the project rules | Both overlays are rendered with a pinned kustomize and validated with kubeconform `-strict` against Kubernetes 1.36 (ExternalSecrets against their CRD schemas at a pinned commit). 60 pytest rules on the rendered YAML: requests and a memory limit everywhere, probes (API health for readiness only), a rollout deadline no shorter than the deploy script's timeout, pinned and quoted image tags, no Secret objects, one API replica with `Recreate`, extractive mode, persistent uploads, Secrets referenced key by key, ExternalSecrets on AWS that make exactly those keys from the parameters Terraform creates, ClusterIP Services only, an ingress that reaches only the UI, non-root pods with no capabilities and no service account token, and Pod Security "baseline" on the namespace. Mutation checks broke 91 rules (C4) and 71 more (C5: Terraform, ESO, ExternalSecrets, the deploy's wait, the state check, secret scanning, Pod Security) one at a time; every one was caught. |
| Secrets never reach Terraform state | Terraform writes PolicyPal's two SSM parameters with write-only arguments from ephemeral random values. Offline: a test proves the configuration never sets a stored value, and a text check over every `.tf` file allows random values only as ephemeral resources. Live: `make check-state-secrets` counted 0 copies of each live value in the state, after the first apply and again after a rotation; the second plan showed no changes ([ADR 016](docs/decisions/016-parameter-store-and-external-secrets.md)). |
| External Secrets Operator makes PolicyPal's Secrets with the node's own role | Live: the store validated with no credentials of its own; `make cloud-deploy` applied the ExternalSecrets, both Secrets appeared in the same second, and the smoke test passed 6/6. A deleted Secret was back in 0.2 s. The node role got AccessDenied reading a parameter outside `/policypal/aws/`; the IAM policy simulator allows only `GetParameter` there. On k3d: the store refuses ExternalSecrets from other namespaces. |
| Pods can't use the node's AWS credentials | Live: an IMDSv2 token request from inside the PolicyPal API pod timed out while ESO (the only host-network controller) kept syncing; the same image on the host network got a token. So the `policypal` namespace enforces Pod Security "baseline": on k3d a host-network pod was refused there and admitted elsewhere, and PolicyPal still deployed and passed its smoke test ([ADR 017](docs/decisions/017-imds-hop-limit-and-host-network.md)). |
| Rotating a secret is a one-line commit | Live: `admin_token_version` 1 → 2, plan (one in-place change), apply. After a forced sync the running API still took the old token until a restart; then the new token got 200 and the old one 403. The commit records the rotation; the value appears nowhere. |
| Secret scanning catches what the defaults missed | gitleaks' default rules miss the two restic passwords committed long ago in `7ca581f`; tests prove the added rule finds exactly those two, flags or skips 16 lines written at run time as expected, and that the full history is clean with only those two fingerprints ignored. The pre-commit hook's first real run blocked one of my own commits (a comment reading like a password assignment), which led to a narrower rule. |
| The budget action blocks new spend, never clean-up | IAM policy simulator with the deny policy added to the admin user: starting instances, NAT gateways or RDS is explicitly denied. Terminating, deleting, detaching the policy and reading or writing the state file stay allowed. The action's role can only attach that one policy to that one user. |
| Rolling restarts drop no requests | Continuous requests through the ingress during a rolling restart: 1,549 and 1,520 requests in two runs, 0 failures. Before the `preStop` delay was added, the same test failed 24 of 1,533 requests (502s and a timeout). |

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
- Helm chart pinned to 92.16.0, scraping the app (ServiceMonitor) and the three nodes' node_exporter.
- Alert rules: AppDown (scrape failing or no pods at all), HighErrorRate (5xx share of requests above 5%), HighLatency (p95 above 0.5 s), NodeExporterDown. Each has promtool unit tests except NodeExporterDown.
- Grafana dashboard loaded automatically: request rate by route and status, p95/p99 latency, and success / client error / server error shares.

### Backup and recovery (restic)
- Daily CronJob. An init container takes a SQLite online backup (safe while the app is writing) and checks it; restic 0.19.1 then backs up that copy, runs `restic check`, and applies 7 daily / 4 weekly / 12 monthly retention.
- `make restore-test` proves the round trip end to end (see above).

### AWS (Terraform, in progress)
The homelab is being extended to AWS (ap-southeast-1) with two Terraform stacks. The **persistent** stack (`terraform/persistent`) holds only resources that cost about $0 at rest. The **ephemeral** stack (`terraform/envs/aws`, below) holds everything billed by the hour and is destroyed after every session.
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
  - At USD 15 an automatic budget action attaches a deny policy to the admin user that blocks starting anything billable. It never blocks deleting, IAM or state writes, so a clean-up still works.
  - Budgets only stop *new* spend, and their data lags by hours, so destroying the hourly stack after every session stays the main guard.
- **What the node needs.** The persistent stack also holds the node's IAM role and instance profile, the SSM parameter `/devops-platform/owner-cidr` with my IP, and PolicyPal's two secrets in SSM (below). All of it is free, and keeping IAM here means creating or destroying the node never needs IAM permissions.
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
| c7i-flex.large + public IPv4 + 20 GB gp3 (default) | USD 0.105/h | USD 0.061/h |

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
- **Sizing**: with PolicyPal running, the node uses about 1.75 GiB, more than a t3.small has, so the default node is a c7i-flex.large (4 GiB).

#### Secrets: Parameter Store and External Secrets Operator
- **Where they live:** `/policypal/aws/db-password` and `/policypal/aws/admin-token`, SSM SecureStrings (Standard tier, AWS managed key `aws/ssm`: free), in the persistent stack.
- **How they're made:** Terraform generates each value with an ephemeral `random_password` and writes it through a write-only argument, so it's in neither the plan nor the state. Only a version number is stored; raising it in a commit rotates the secret. `make check-state-secrets` reads each live value and counts its copies in the state, printing only the counts.
- **How they reach the pods:** External Secrets Operator (`make cloud-addons`, pinned chart) reads them with the node's instance role through a `ClusterSecretStore` that has no credentials of its own and serves only the `policypal` namespace. Two `ExternalSecret`s make the Secrets and refresh them hourly. The node role may only `GetParameter` under `/policypal/aws/`. See [ADR 016](docs/decisions/016-parameter-store-and-external-secrets.md).
- **Who can reach the node's credentials:** IMDSv2 with hop limit 1 answers only processes on the host network, so only ESO's controller runs there. PolicyPal's namespace enforces Pod Security "baseline", which refuses host-network pods ([ADR 017](docs/decisions/017-imds-hop-limit-and-host-network.md)).
- **Secret scanning:** gitleaks with its default rules plus one for passwords assigned as plain text, run on the whole history in CI and weekly, and on staged changes by a pre-commit hook.

### CI/CD (GitHub Actions)
- On every push and pull request: lint, tests, alert-rule tests, hadolint, repo-level tests, ShellCheck, the Kubernetes checks (kustomize + kubeconform), the Ansible checks and the offline Terraform checks, then build and Trivy scan.
- Secret scan (`secret-scan.yml`): the full history with the repo's gitleaks rules on every pull request, every Monday and on demand; read-only, no secrets.
- On master: the image is pushed to GHCR as `master` and `sha-<commit>`. Actions are pinned to current major versions; trivy-action is pinned to a commit SHA.
- Manual CD with an ephemeral k3d cluster and a smoke test.

## Architecture

```
 git push ──► GitHub Actions CI ──(gates pass)──► GHCR  (master, sha-<commit>)
                 lint · tests · rule tests · hadolint · build · Trivy

 Manual CD ──► throwaway k3d on a GitHub-hosted runner ──► deploy dev overlay ──► smoke test

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
make k8s-check             # render both PolicyPal overlays (pinned kustomize) and the ESO chart (pinned Helm); kubeconform -strict
make policypal-local       # PolicyPal on k3d: random Secrets (once), deploy, smoke test -> http://policypal.localhost:8080
make cloud-addons          # AWS: install External Secrets Operator on the node (once per node; needs make cloud-tunnel running)
make cloud-deploy          # AWS: deploy PolicyPal (ESO makes its Secrets) and smoke-test it; rolls back if the smoke test fails
make cloud-smoke           # AWS: only the smoke test
make check-state-secrets   # AWS, read-only: count each live secret value in the Terraform state (must be 0)
```

The node is a c7i-flex.large on Spot by default (k3s plus PolicyPal need more than a t3.small's 2 GiB); `make cloud-up CLOUD_ARGS='-var use_spot=false'` starts it on-demand. With the tunnel open: `KUBECONFIG=~/.kube/devops-aws.yaml kubectl get nodes`. A shell on the node: `aws ssm start-session --target "$(cd terraform/envs/aws && terraform output -raw instance_id)"`.

Grafana and Prometheus are reached with a port-forward:

```bash
kubectl -n monitoring port-forward svc/kube-prometheus-stack-grafana 3000:80        # http://localhost:3000/d/devops-app-metrics
kubectl -n monitoring get secret grafana-admin -o jsonpath='{.data.admin-password}' | base64 -d; echo   # user: admin
kubectl -n monitoring port-forward svc/kube-prometheus-stack-prometheus 9090:9090   # http://localhost:9090
```

## Requirements

- WSL2 Ubuntu 24.04 with Docker Desktop (WSL integration on). Developed and tested on a machine with 8 CPUs and 11 GB RAM.
- `git`, `make`, `curl`, `jq`, `openssl`, `ansible`, `python3` with the venv module, `docker`, `kubectl`, `k3d`, `helm`, and an SSH key at `~/.ssh/id_ed25519`. `make check` lists what's missing; `make install` apt-installs the packages it can (with sudo), creates the SSH key, and links to installers for the rest.
- hadolint, Trivy, promtool, yq, ShellCheck, gitleaks, tflint, kustomize and kubeconform don't need installing: they run as pinned Docker images.
- For the AWS work (in progress): AWS CLI v2.32 or newer (for `aws login`), the Session Manager plugin, and Terraform 1.11 or newer (write-only arguments). `make ansible-check` needs ansible-lint (`pipx install ansible-lint`). The pre-commit hook needs pre-commit (`pipx install pre-commit==4.7.0`).
- Host port 8080 free (the k3d load balancer).

## Project structure

- `terraform/` — AWS infrastructure: `persistent/` (state bucket, budgets, IAM, the node's role, the owner-IP parameter and PolicyPal's secrets), `envs/aws/` (the ephemeral stack) and `modules/` (`network`, `compute`, and `names`: the names both stacks share), with tests in each directory's `tests/`
- `ansible/` — inventories (`docker-nodes.yml`; `aws.yml` is generated per session), playbooks (`site.yml` for the homelab, `aws-node.yml` for EC2), roles `common`, `node_exporter` and `k3s`, and `requirements.yml` for the one collection used
- `infra/nodes/` — node image, startup script, Docker Compose file
- `app/` — FastAPI service, tests, Dockerfile
- `k8s/` — the v1 app's kustomize base and `dev` / `prod` overlays; `k8s/policypal/` — PolicyPal's base (namespace, Postgres, API, UI, ingress) and `local` / `aws` overlays (`aws` adds the ExternalSecrets); `k8s/platform/external-secrets/` — External Secrets Operator's pinned chart (`chart.env`), values and ClusterSecretStore
- `monitoring/` — Helm values, ServiceMonitor, alert rules and their tests, dashboard
- `backup/` — backup CronJob, restore Job, restore test
- `ci/` — scripts shared by the Makefile and GitHub Actions (lint, test, rule tests, build, scan, ShellCheck, gitleaks, Terraform, Kubernetes and Ansible checks)
- `scripts/` — toolchain check (`bootstrap.sh`), the read-only AWS cost check (`cost-check.sh`), `owner-ip.sh`, `ephemeral-tf.sh` (the Terraform run behind `make cloud-up` / `cloud-down`), `node-id.sh` and `aws-inventory.sh` (behind `make cloud-config`), `cloud-tunnel.sh`, and for PolicyPal `render-k8s.sh`, `policypal-secrets.sh` (k3d only), `deploy-policypal.sh`, `smoke-test.sh` and `node-ip.sh`; for secrets `render-eso.sh`, `install-eso.sh` and `check-state-secrets.sh`
- `tests/` — repo-level tests: the cost check (against a fake `aws` CLI), the AWS session Make targets (against fake `terraform`, `aws`, `curl` and `ansible-playbook`), the rules for the AWS node's Ansible code, the rules for the rendered PolicyPal manifests and the rendered ESO chart, the PolicyPal and secrets scripts and Make targets (against fake `kubectl`, `curl`, `docker`, `k3d`, `helm`, `terraform` and `aws`), Terraform's secret handling, the gitleaks rule and hook, the workflows, the scripts' copies of shared Terraform names, and the `.gitignore` rules
- `.gitleaks.toml`, `.gitleaksignore`, `.pre-commit-config.yaml` — secret scanning: the extra rule, the two known findings, the pre-commit hook
- `.github/workflows/` — CI on every push, the secret scan, manual CD
- `docs/` — architecture, decisions, runbook, fresh-clone test log

## Limitations

- **`make deploy` always restarts the app.** The dev image tag is always `latest`, so the deployment is restarted on every deploy to pick up the new image, even when nothing changed. The restart drops no requests, but it does take about 25 seconds.
- **Backups live on one node's disk.** The restic repository is a local PVC with no off-site copy, so losing that disk loses the backups.
- **One snapshot per day.** Retention keeps the newest snapshot of each day, so a manual backup (or `make restore-test`) replaces that day's scheduled one.
- **SQLite means one replica.** The dev overlay runs a single pod; scaling out needs a different database.
- **`make restore-test` changes the dev database.** It wipes and restores `/data/app.db`, and leaves its test items behind. Run it only against a dev cluster.
- **The AWS node is one instance, and its data lasts one session.** There is no high availability; a Spot interruption takes it away until `make cloud-rebuild` and `make cloud-deploy` (about four minutes). PolicyPal's Postgres and uploads live on the node's disk, so anything uploaded is lost at `make cloud-down` until backups arrive ([ADR 014](docs/decisions/014-postgres-in-the-cluster.md)).
- **Everything on the node's host network shares one AWS role.** k3s, the SSM agent and External Secrets Operator's controller all get the node role's credentials; on EKS each pod could have its own. The role is small (the SSM agent's actions and reading `/policypal/aws/*`), and PolicyPal's namespace refuses host-network pods ([ADR 017](docs/decisions/017-imds-hop-limit-and-host-network.md)).
- **A rotated secret needs a restart.** ESO updates the Secret within the hour (or at once with a forced sync), but the API reads it only when it starts. Rotating the database password also needs `ALTER ROLE` in Postgres first ([runbook](docs/runbook.md)).
- **A PolicyPal deploy stops the API first.** It is one process with one uploads volume (`Recreate`), so the API is briefly down on every change, and stays down until the rollback if the new version fails. The UI keeps running.
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

## Next steps

- Off-site backups: copy the restic repository to object storage.
- Finish the AWS environment: CI/CD that authenticates to AWS with OIDC, and monitoring and backups on the node.
- Loki for logs; cert-manager and TLS.
- Pull-based CD into a long-lived cluster.

## License

MIT. See [LICENSE](LICENSE).
