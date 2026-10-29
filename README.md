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
- kustomize overlays: `dev` (deployed locally) and `prod` (3 replicas, pinned GHCR image tag; a template, not deployed anywhere).

### Monitoring (kube-prometheus-stack)
- Helm chart pinned to 92.16.0, scraping the app (ServiceMonitor) and the three nodes' node_exporter.
- Alert rules: AppDown (scrape failing or no pods at all), HighErrorRate (5xx share of requests above 5%), HighLatency (p95 above 0.5 s), NodeExporterDown. Each has promtool unit tests except NodeExporterDown.
- Grafana dashboard loaded automatically: request rate by route and status, p95/p99 latency, and success / client error / server error shares.

### Backup and recovery (restic)
- Daily CronJob. An init container takes a SQLite online backup (safe while the app is writing) and checks it; restic 0.19.1 then backs up that copy, runs `restic check`, and applies 7 daily / 4 weekly / 12 monthly retention.
- `make restore-test` proves the round trip end to end (see above).

### CI/CD (GitHub Actions)
- On every push and pull request: lint, tests, alert-rule tests, hadolint, then build and Trivy scan.
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
```

Grafana and Prometheus are reached with a port-forward:

```bash
kubectl -n monitoring port-forward svc/kube-prometheus-stack-grafana 3000:80        # http://localhost:3000/d/devops-app-metrics
kubectl -n monitoring get secret grafana-admin -o jsonpath='{.data.admin-password}' | base64 -d; echo   # user: admin
kubectl -n monitoring port-forward svc/kube-prometheus-stack-prometheus 9090:9090   # http://localhost:9090
```

## Requirements

- WSL2 Ubuntu 24.04 with Docker Desktop (WSL integration on). Developed and tested on a machine with 8 CPUs and 11 GB RAM.
- `git`, `make`, `curl`, `jq`, `openssl`, `ansible`, `python3` with the venv module, `docker`, `kubectl`, `k3d`, `helm`, and an SSH key at `~/.ssh/id_ed25519`. `make check` lists what's missing; `make install` apt-installs the packages it can (with sudo), creates the SSH key, and links to installers for the rest.
- hadolint, Trivy, promtool and yq don't need installing: they run as pinned Docker images.
- Host port 8080 free (the k3d load balancer).

## Project structure

- `ansible/` — inventory, playbook, `common` and `node_exporter` roles
- `infra/nodes/` — node image, startup script, Docker Compose file
- `app/` — FastAPI service, tests, Dockerfile
- `k8s/` — kustomize base and `dev` / `prod` overlays
- `monitoring/` — Helm values, ServiceMonitor, alert rules and their tests, dashboard
- `backup/` — backup CronJob, restore Job, restore test
- `ci/` — scripts shared by the Makefile and GitHub Actions (lint, test, rule tests, build, scan)
- `.github/workflows/` — CI on every push, manual CD
- `docs/` — architecture, decisions, runbook, fresh-clone test log

## Limitations

- **The systemd branch of the node_exporter role is untested.** The Docker nodes have no systemd, so only the SysV path has run. The systemd unit renders correctly but has never been started on a real VM.
- **`make deploy` always restarts the app.** The dev image tag is always `latest`, so the deployment is restarted on every deploy to pick up the new image, even when nothing changed.
- **Rolling restarts drop some requests.** With one replica, about 1.6% of requests failed (502 or timeout) for a few seconds during a restart, because the old pod stops before Traefik stops routing to it.
- **Backups live on one node's disk.** The restic repository is a local PVC with no off-site copy, so losing that disk loses the backups.
- **One snapshot per day.** Retention keeps the newest snapshot of each day, so a manual backup (or `make restore-test`) replaces that day's scheduled one.
- **SQLite means one replica.** The dev overlay runs a single pod; scaling out needs a different database.
- **`make restore-test` changes the dev database.** It wipes and restores `/data/app.db`, and leaves its test items behind. Run it only against a dev cluster.
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

## Next steps

- Off-site backups: copy the restic repository to object storage.
- A `preStop` delay (or a second replica) so rolling restarts don't drop requests.
- Terraform plus real cloud VMs or Proxmox, using the same Ansible roles.
- Loki for logs; cert-manager and TLS.
- Pull-based CD into a long-lived cluster.

## License

MIT. See [LICENSE](LICENSE).
