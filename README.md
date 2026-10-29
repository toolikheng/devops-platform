# Self-Hosted Homelab DevOps Platform

[![CI](https://github.com/toolikheng/devops-platform/actions/workflows/ci.yml/badge.svg?branch=master)](https://github.com/toolikheng/devops-platform/actions/workflows/ci.yml)

A hands-on portfolio project demonstrating end-to-end DevOps skills: infrastructure provisioning, application deployment, Kubernetes orchestration, monitoring, and automated backups — all running in a self-hosted homelab on WSL2.

## Quick Start

```bash
# 1. Check the toolchain (make install fixes what it can)
make check

# 2. Start Docker infrastructure (3 nodes)
make infra-up

# 3. Provision with Ansible
make provision

# 4. Run tests
make test

# 5. Build container image
make build

# 6. Create Kubernetes cluster
make cluster-up

# 7. Deploy app
make deploy

# 8. View monitoring at http://localhost:3000 (Grafana)
make monitoring-up
```

All commands are in the `Makefile` — run `make help` for the complete list.

## What's Included

### 🏗️ Infrastructure as Code (Ansible)
- **3 Docker nodes** running Ubuntu systemd + sshd (standing in for Proxmox VMs)
- Idempotent roles for common tasks, node_exporter for metrics
- Inventory design is swappable — move to real Proxmox or cloud VMs without rewriting roles

### 📦 Application (FastAPI)
- Microservice with health checks (`/healthz`, `/readyz`), CRUD endpoints, Prometheus metrics
- Test-driven development with pytest
- SQLite persistence with backup/restore support
- Multi-stage Docker image with non-root user and security scanning

### ☸️ Kubernetes (k3d)
- k3s cluster (1 server, 2 agents) running in Docker
- Deployment manifests with resource limits, probes, security context
- kustomize overlays for dev/prod configuration
- Self-healing pods and zero-downtime rolling updates

### 📊 Monitoring (Prometheus + Grafana)
- `kube-prometheus-stack` Helm chart (Prometheus, Grafana, Alertmanager)
- ServiceMonitor for the app and alert rules for downtime, high error rates, and latency
- App dashboard JSON in `monitoring/dashboards/` (import it into Grafana; not loaded automatically yet)
- Docker-node scraping requires `make provision` first

### 💾 Backup & Recovery (restic)
- Daily backup of the app's SQLite database via Kubernetes CronJob, using SQLite's online backup API (consistent while the app is writing)
- Restic repository on a persistent volume, with retention policy and pruning
- **Restore test included** (`make restore-test`) — writes data, backs up, wipes it, restores, and verifies the result

### 🔄 CI/CD (GitHub Actions)
- Lint, test, build, scan pipeline on every push
- Multi-stage build stages (lint → test → hadolint → build → trivy → push)
- Images tagged by commit SHA and pushed to GitHub Container Registry (GHCR)
- Manual deploy (`cd.yml`, `workflow_dispatch`): creates an ephemeral k3d cluster on a GitHub-hosted runner, deploys the dev overlay, and smoke-tests `/healthz` and `/readyz`
- Self-hosted runner variant (for deploying to the local cluster) lives on the `self-hosted-runner` branch

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│            GitHub Actions CI/CD Pipeline                │
│  (lint, test, build, scan, push to GHCR)               │
└──────────────────┬──────────────────────────────────────┘
                   │
                   ▼
          ┌─────────────────┐
          │  GHCR Registry  │
          │  (images)       │
          └────────┬────────┘
                   │
      ┌────────────┴────────────┐
      │                         │
      ▼                         ▼
┌──────────────┐       ┌──────────────────┐
│    k3d       │       │  Docker Nodes    │
│  Cluster     │       │  (3 systemd      │
│  • Traefik   │       │   containers)    │
│  • App       │       │                  │
│  • Ingress   │       │  ← Ansible       │
│              │       │  provisions      │
│              │       │  over SSH        │
└──────────────┘       └──────────────────┘
      │
      ├──────────────────────────┐
      │                          │
      ▼                          ▼
  ┌─────────────────┐   ┌──────────────────┐
  │  Prometheus     │   │  Grafana         │
  │  (pull-based)   │   │  (dashboards)    │
  └─────────────────┘   └──────────────────┘
      │
      ├─────────────────────────┐
      │                         │
      ▼                         ▼
   (targets)           (targets)
   • App metrics       • cluster health
   • node_exporter     • app golden signals
```

## Important: Local vs Production

This project uses substitutes for local development that would change in a production deployment:

| Component | Local (This Project) | Production |
|-----------|----------------------|------------|
| VMs / Compute | Docker systemd containers | Proxmox VE, cloud VMs, or bare metal |
| Kubernetes | k3d (k3s in Docker) | k3s/k8s cluster on real nodes |
| Registry | GitHub Container Registry (GHCR) | GHCR or private registry (same interface) |
| Backup target | Restic repo on a local PVC | S3, B2, or other object storage |
| Ingress / TLS | Traefik + self-signed via localhost | cert-manager + real domain |

**The payoff:** Ansible playbooks, Kubernetes manifests, and monitoring configs are **identical** between local and production. Only the inventory and Helm values change — no rewriting roles or manifests.

## Development Workflow

### Running the app locally
```bash
make run-app
# Serves on http://localhost:8000
# OpenAPI docs at http://localhost:8000/docs
```

### Testing
```bash
make test      # pytest suite (creates app/.venv with dev dependencies on first run)
make lint      # ruff + mypy
```

### Building the image
```bash
make build     # Builds, scans for CVEs, shows report
```

### Deploying to k3d
```bash
make cluster-up   # Create cluster
make deploy       # Deploy app, backup CronJob, and backup credentials
```

### Monitoring
```bash
make monitoring-up
# Grafana: http://localhost:3000
# Prometheus: http://localhost:9090
```

### Backup & Recovery
```bash
make backup         # Run one backup job now and print its log
make restore-test   # Write test data, back up, wipe the dev DB, restore, verify (dev cluster only)
```

### Full teardown
```bash
make destroy        # Removes cluster, stops nodes
```

## Environment Setup

**Requirements:**
- **WSL2 Ubuntu 24.04+** (Windows 10 build 19045+)
- **Docker Desktop** with WSL2 integration enabled
- 8+ GB RAM, 50+ GB free disk space

**First time?** Run:
```bash
make install   # apt-installs missing packages (incl. python3-venv) with sudo, creates ~/.ssh/id_ed25519,
               # and links to installers for Docker Desktop, kubectl, k3d and helm
```

See [`docs/architecture.md`](docs/architecture.md) and [`docs/decisions/`](docs/decisions/) for the architecture and design decisions.

## Project Structure

- **`ansible/`** — Roles and playbooks for infrastructure provisioning
- **`app/`** — FastAPI microservice with tests and Dockerfile
- **`k8s/`** — Kubernetes manifests (kustomize-based)
- **`infra/nodes/`** — Docker Compose for the "VMs"
- **`monitoring/`** — Prometheus and Grafana configuration (Helm values, dashboards, alert rules)
- **`backup/`** — Backup CronJob, restore Job, and restore test script
- **`ci/`** — Shared CI logic (called by both Makefile and GitHub Actions)
- **`.github/workflows/`** — GitHub Actions pipeline (CI on every push, manual CD)

## Phases

This project is built in phases. Each phase is a working, deployable piece:

1. ✓ **Phase 0** — Bootstrap (toolchain, repo init, SSH keys)
2. ✓ **Phase 1** — Ansible + Docker nodes
3. ✓ **Phase 2** — FastAPI app + tests
4. ✓ **Phase 3** — Containerization + scanning
5. ✓ **Phase 4** — Kubernetes deployment
6. ✓ **Phase 5** — GitHub Actions CI/CD
7. ✓ **Phase 6** — Monitoring (Prometheus + Grafana)
8. **Self-hosted runner** — on the `self-hosted-runner` branch

## Proof Points (What You Can Demo)

- **Idempotency:** Run Ansible twice, second run shows `changed=0` ✓
- **Self-healing:** Kill a pod, watch it restart within seconds ✓
- **Rolling updates:** Deploy a new app version with zero downtime ✓
- **Monitoring:** Prometheus scrapes the app's `up` series for the `devops` namespace ✓
- **Backup:** `make restore-test` restores the app's data from a restic snapshot and verifies it ✓
- **CI/CD:** Push to master, watch the pipeline run end-to-end ✓

## Common Issues & Troubleshooting

**Docker daemon not running:**
```bash
# Start Docker Desktop (GUI), or enable WSL integration under Resources → WSL Integration
```

**k3d cluster fails to start:**
```bash
# Check Docker is running
docker ps

# If you see port conflicts, destroy and recreate
make cluster-down
make cluster-up
```

**Ansible can't reach nodes:**
```bash
# Make sure infra-up has started the containers
make infra-up

# Check SSH key permissions
ls -la ~/.ssh/id_ed25519
chmod 600 ~/.ssh/id_ed25519
```

**Grafana dashboards empty:**
```bash
# The app dashboard is not loaded automatically: import monitoring/dashboards/app-metrics.json in Grafana
# Wait 30 seconds for Prometheus to scrape targets, then check http://localhost:9090/targets
```

## What I Learned Building This

- Ansible is powerful for declarative infrastructure; roles + handlers enforce idempotency
- k3d is an honest testbed for k3s — manifests transfer directly to production
- Pull-based monitoring (Prometheus) changes how you think about observability vs push-based systems
- A restore test you've actually run beats any backup policy document
- Separating CI logic (`ci/` scripts) from pipeline config (`.github/workflows/`) makes local and cloud testing identical

## Next Steps

- **Off-site backups** — copy the restic repository to object storage; right now it lives on one node's disk
- **Grafana dashboard provisioning** — load the app dashboard automatically instead of importing it by hand
- **Self-hosted runner** — fix pod-to-API-server connectivity so the in-cluster deploy job can run
- **Terraform + real cloud VMs** — drop in AWS/Hetzner/Oracle Cloud instances and let Terraform create the inventory
- **Proxmox integration** — when hardware is available, swap the inventory and the same roles work
- **Loki + log aggregation** — add centralized logging
- **cert-manager + TLS** — real certificates instead of self-signed

## License

MIT. See [LICENSE](LICENSE).

## Author

Built as a portfolio project to demonstrate DevOps skills.

---

**Have questions?** Open an issue on this repository.
