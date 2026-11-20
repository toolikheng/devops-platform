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
| Provisioning is idempotent | `make provision` run twice against freshly created nodes: the first run changed 12 tasks per node, the second reported `changed=0`. node_exporter comes back on its own after `docker restart`. |
| CI gates every push | `ci.yml` runs ruff + mypy, 13 pytest tests, promtool alert-rule tests and hadolint. The image is built, scanned by Trivy, and pushed to GHCR only if there are no fixable HIGH/CRITICAL vulnerabilities. The lint, test, rule-test and Trivy gates were each shown to fail on a real problem before being trusted. |
| CD smoke test | `cd.yml` (manual) builds the image, creates a throwaway k3d cluster on a GitHub-hosted runner, deploys the dev overlay, and checks `/healthz` and `/readyz`. Last run passed. |
| Alert rules load and fire | All four rules are loaded by Prometheus. AppDown fired when the app was scaled to 0 pods (pending, then firing within about two minutes) and cleared when it came back. NodeExporterDown fired while node_exporter was missing and cleared once the nodes were provisioned. |
| Dashboard is provisioned | `make monitoring-up` loads the app dashboard into Grafana with no manual import. Its panels were checked through Grafana's query API under real traffic, and the success / 4xx / 5xx shares add up to 100%. |
| Backups are consistent | `make backup` was run while 656 writes hit the app; the SQLite copy passed `PRAGMA integrity_check` and was saved as a restic snapshot. |
| Restore test catches a broken restore | `make restore-test` passes only if the app serves exactly the backed-up items afterwards and an item written after the backup is gone. A deliberately broken restore (data never copied back) makes it fail. |
| Self-healing | Deleting the app pod: a replacement was Ready 14 seconds later. |
| AWS cost check is read-only and fails safe | `make cost-check` lists anything billable left in the account: the full list in ap-southeast-1 (instances, volumes, snapshots, Elastic IPs, public IPv4, NAT gateways, VPC endpoints, load balancers, EKS, RDS, Secrets Manager, open or active Spot requests, Route 53 hosted zones, ECR repositories, Lightsail instances and databases, SageMaker notebooks and endpoints, customer-managed KMS keys) plus instances, volumes and Elastic IPs in every other enabled region. 38 tests run it against a fake `aws` CLI: each kind of leftover is reported, only describe/list/get calls are made, the Cost Explorer API is never called, and a check that can't run is an error, never a clean result. Run against the real account after every session: clean, and it ignored the two cancelled one-time Spot requests and the free AWS-managed `aws/ebs` key. |
| Terraform state is private, TLS-only and locked | 16 `terraform test` runs check the persistent stack offline with a mock provider, and each one was shown to fail on a deliberately broken copy. Live: the apply created the state bucket and budgets; a second plan through the S3 backend shows no changes; a plain-HTTP request to the bucket gets AccessDenied even as the admin user; with two plans started at once, the second fails with "Error acquiring the state lock". |
| The AWS node is reachable only the way it should be | Live, on-demand and Spot: a shell through SSM Session Manager worked with a role allowed only the 11 SSM-agent actions. From my IP, port 80 was allowed while 22 and 6443 were dropped even though sshd was listening. IMDSv1 got 401 and IMDSv2 worked. Outbound HTTPS and HTTP worked; other ports were blocked. Details in [docs/aws-test-log.md](docs/aws-test-log.md). |
| The node's rules hold before anything is created | 30 offline `terraform test` runs on the network and compute modules and the ephemeral stack: allowlisted instance types only, x86 only, encrypted gp3 root volume of at most 30 GB deleted with the instance, IMDSv2 with hop limit 1, standard CPU credits, no inbound 22 or 6443, owner IP must be one /32. A mutation check broke 37 rules one at a time across the Terraform code; every one was caught. |
| Spot and on-demand both work, and both leave nothing behind | Live: each run was created, checked, destroyed and followed by a clean `make cost-check`; a second plan after each apply showed no changes. The first Spot run failed on a real AWS restriction (see [ADR 009](docs/decisions/009-spot-and-on-demand.md)); it was fixed test-first and the retry passed. The Spot request was one-time with the on-demand price as its cap. |
| The budget action blocks new spend, never clean-up | IAM policy simulator with the deny policy added to the admin user: starting instances, NAT gateways or RDS is explicitly denied. Terminating, deleting, detaching the policy and reading or writing the state file stay allowed. The action's role can only attach that one policy to that one user. |
| Rolling restarts drop no requests | Continuous requests through the ingress during a rolling restart: 1,549 and 1,520 requests in two runs, 0 failures. Before the `preStop` delay was added, the same test failed 24 of 1,533 requests (502s and a timeout). |

## What's included

### Infrastructure as code (Ansible)
- Three Ubuntu 24.04 containers stand in for VMs. They run `sshd` as PID 1 (no systemd), and your `~/.ssh/id_ed25519.pub` is mounted in at startup, so no key is committed.
- `ansible-playbook` runs from WSL over real SSH to `localhost:2201-2203`.
- Roles: `common` (packages, timezone, SSH hardening, motd) and `node_exporter` (checksum-verified download, run as a service). The role uses systemd on hosts that have it and a SysV init script on hosts that don't, such as these containers.

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
- **What the node needs.** The persistent stack also holds the node's IAM role and instance profile, and the SSM parameter `/devops-platform/owner-cidr` with my IP. All of it is free, and keeping IAM here means creating or destroying the node never needs IAM permissions.
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
- **Your IP:** `make owner-ip` stores this machine's public IP as a /32. Until it has run, the plan fails rather than open the port to a made-up address.

Cost while the stack exists (ap-southeast-1, before tax). Nothing is billed once it is destroyed.

| Node | On-demand | Spot (typical) |
|---|---|---|
| t3.micro + public IPv4 + 20 GB gp3 | USD 0.021/h | USD 0.012/h |
| t3.small + public IPv4 + 20 GB gp3 | USD 0.034/h | USD 0.019/h |
| c7i-flex.large + public IPv4 + 20 GB gp3 | USD 0.105/h | USD 0.061/h |

See [ADR 008](docs/decisions/008-k3s-on-ec2-over-eks.md) (k3s on EC2, not EKS), [ADR 009](docs/decisions/009-spot-and-on-demand.md) (Spot and on-demand), [ADR 010](docs/decisions/010-public-subnet-without-nat.md) (network and access) and the live results in [docs/aws-test-log.md](docs/aws-test-log.md).

### CI/CD (GitHub Actions)
- On every push and pull request: lint, tests, alert-rule tests, hadolint, repo-level tests, ShellCheck and the offline Terraform checks, then build and Trivy scan.
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
bash ci/gitleaks.sh        # scan the full git history for secrets, values redacted (pinned Docker image)
make tf-check              # offline Terraform checks: fmt, validate, terraform test, tflint, trivy
make cost-check            # AWS: read-only scan for billable leftovers (needs `aws login --profile devops`)
make owner-ip              # AWS: store this machine's public IP (/32) for the node's security group
make cloud-up              # AWS: create the ephemeral stack, after Terraform shows the plan and you type yes (billed hourly)
make cloud-down            # AWS: destroy it (also asks first), then run cost-check
```

`make cloud-up CLOUD_ARGS='-var use_spot=true'` starts the node on Spot. A shell on the node: `aws ssm start-session --target "$(cd terraform/envs/aws && terraform output -raw instance_id)"`.

Grafana and Prometheus are reached with a port-forward:

```bash
kubectl -n monitoring port-forward svc/kube-prometheus-stack-grafana 3000:80        # http://localhost:3000/d/devops-app-metrics
kubectl -n monitoring get secret grafana-admin -o jsonpath='{.data.admin-password}' | base64 -d; echo   # user: admin
kubectl -n monitoring port-forward svc/kube-prometheus-stack-prometheus 9090:9090   # http://localhost:9090
```

## Requirements

- WSL2 Ubuntu 24.04 with Docker Desktop (WSL integration on). Developed and tested on a machine with 8 CPUs and 11 GB RAM.
- `git`, `make`, `curl`, `jq`, `openssl`, `ansible`, `python3` with the venv module, `docker`, `kubectl`, `k3d`, `helm`, and an SSH key at `~/.ssh/id_ed25519`. `make check` lists what's missing; `make install` apt-installs the packages it can (with sudo), creates the SSH key, and links to installers for the rest.
- hadolint, Trivy, promtool, yq, ShellCheck, gitleaks and tflint don't need installing: they run as pinned Docker images.
- For the AWS work (in progress): AWS CLI v2.32 or newer (for `aws login`), the Session Manager plugin, and Terraform 1.11 or newer.
- Host port 8080 free (the k3d load balancer).

## Project structure

- `terraform/` — AWS infrastructure: `persistent/` (state bucket, budgets, IAM, the node's role and the owner-IP parameter), `envs/aws/` (the ephemeral stack) and `modules/` (`network`, `compute`), with tests in each directory's `tests/`
- `ansible/` — inventory, playbook, `common` and `node_exporter` roles
- `infra/nodes/` — node image, startup script, Docker Compose file
- `app/` — FastAPI service, tests, Dockerfile
- `k8s/` — kustomize base and `dev` / `prod` overlays
- `monitoring/` — Helm values, ServiceMonitor, alert rules and their tests, dashboard
- `backup/` — backup CronJob, restore Job, restore test
- `ci/` — scripts shared by the Makefile and GitHub Actions (lint, test, rule tests, build, scan, ShellCheck, gitleaks)
- `scripts/` — toolchain check (`bootstrap.sh`), the read-only AWS cost check (`cost-check.sh`), `owner-ip.sh`, and `ephemeral-tf.sh` (the Terraform run behind `make cloud-up` / `cloud-down`)
- `tests/` — repo-level tests: the cost check (against a fake `aws` CLI), the AWS session Make targets (against fake `terraform`, `aws` and `curl`) and the `.gitignore` rules
- `.github/workflows/` — CI on every push, manual CD
- `docs/` — architecture, decisions, runbook, fresh-clone test log

## Limitations

- **The systemd branch of the node_exporter role is untested.** The Docker nodes have no systemd, so only the SysV path has run. The systemd unit renders correctly but has never been started on a real VM.
- **`make deploy` always restarts the app.** The dev image tag is always `latest`, so the deployment is restarted on every deploy to pick up the new image, even when nothing changed. The restart drops no requests, but it does take about 25 seconds.
- **Backups live on one node's disk.** The restic repository is a local PVC with no off-site copy, so losing that disk loses the backups.
- **One snapshot per day.** Retention keeps the newest snapshot of each day, so a manual backup (or `make restore-test`) replaces that day's scheduled one.
- **SQLite means one replica.** The dev overlay runs a single pod; scaling out needs a different database.
- **`make restore-test` changes the dev database.** It wipes and restores `/data/app.db`, and leaves its test items behind. Run it only against a dev cluster.
- **The AWS node is a bare server so far.** It runs Ubuntu and the SSM agent; k3s and the app come in later phases. It is a single instance with no high availability, and a Spot interruption takes it away until it is rebuilt.
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

## Next steps

- Off-site backups: copy the restic repository to object storage.
- Finish the AWS environment: k3s on the node with the same Ansible roles, PolicyPal, secrets from SSM Parameter Store, and CI/CD that authenticates to AWS with OIDC.
- Loki for logs; cert-manager and TLS.
- Pull-based CD into a long-lived cluster.

## License

MIT. See [LICENSE](LICENSE).
