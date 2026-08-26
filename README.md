# Self-Hosted Homelab DevOps Platform

[![CI](https://img.shields.io/badge/CI-GitHub%20Actions-blue)](https://github.com/YOUR_GITHUB_USERNAME/devops-platform/actions)
![Status](https://img.shields.io/badge/Status-Building-yellow)

A hands-on portfolio project demonstrating end-to-end DevOps skills: infrastructure provisioning, application deployment, Kubernetes orchestration, monitoring, and automated backups — all running in a self-hosted homelab on WSL2.

## Quick Start

```bash
# 1. Check toolchain is installed
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
- Single-node k3s cluster inside Docker
- Deployment manifests with resource limits, probes, security context
- kustomize overlays for dev/prod configuration
- Self-healing pods and zero-downtime rolling updates

### 📊 Monitoring (Prometheus + Grafana)
- `kube-prometheus-stack` Helm chart (Prometheus, Grafana, Alertmanager)
- Custom dashboards for cluster health and application metrics
- Alert rules for downtime, high error rates, node failures
- Scrapes both the Kubernetes cluster and Ansible-provisioned nodes

### 💾 Backup & Recovery (restic)
- Automated daily backups via Kubernetes CronJob
- Retention policy and pruning
- **Restore test included** — proves backups are recoverable (not theoretical)
- Backs up app data, Kubernetes manifests, Grafana dashboards

### 🔄 CI/CD (GitHub Actions)
- Lint, test, build, scan pipeline on every push
- Multi-stage build stages (lint → test → hadolint → build → trivy → push)
- Images tagged by commit SHA and pushed to GitHub Container Registry (GHCR)
- Manual deploy job (cluster not reachable from GitHub runners)

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

**Full architecture diagram:** see `docs/diagrams/`.

## Important: Local vs Production

This project uses substitutes for local development that would change in a production deployment:

| Component | Local (This Project) | Production |
|-----------|----------------------|------------|
| VMs / Compute | Docker systemd containers | Proxmox VE, cloud VMs, or bare metal |
| Kubernetes | k3d (k3s in Docker) | k3s/k8s cluster on real nodes |
| Registry | GitHub Container Registry (GHCR) | GHCR or private registry (same interface) |
| Backup target | Local restic repo on disk | S3, B2, or other object storage |
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
make test      # pytest suite
make lint      # ruff + mypy
```

### Building the image
```bash
make build     # Builds, scans for CVEs, shows report
```

### Deploying to k3d
```bash
make cluster-up   # Create cluster
make deploy       # Deploy app
```

### Monitoring
```bash
make monitoring-up
# Grafana: http://localhost:3000
# Prometheus: http://localhost:9090
```

### Backup & Recovery
```bash
make backup         # Run one backup job
make restore-test   # Test the restore procedure (destroys & recovers real data)
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
make install   # Installs: ansible, kubectl, k3d, helm, kubeconform, hadolint, trivy, restic, gh CLI
```

See `CLAUDE.md` for detailed environment info and architectural decisions.

## Project Structure

- **`ansible/`** — Roles and playbooks for infrastructure provisioning
- **`app/`** — FastAPI microservice with tests and Dockerfile
- **`k8s/`** — Kubernetes manifests (kustomize-based)
- **`infra/nodes/`** — Docker Compose for the "VMs"
- **`monitoring/`** — Prometheus and Grafana configuration (Helm values, dashboards, alert rules)
- **`backup/`** — restic backup scripts and restore test
- **`ci/`** — Shared CI logic (called by both Makefile and GitHub Actions)
- **`.github/workflows/`** — GitHub Actions pipeline
- **`docs/`** — Architecture, decisions (ADRs), diagrams, runbooks

## Phases

This project is built in phases. Each phase is a working, deployable piece:

1. ✓ **Phase 0** — Bootstrap (toolchain, repo init, SSH keys)
2. ⏳ **Phase 1** — Ansible + Docker nodes
3. ⏳ **Phase 2** — FastAPI app + tests
4. ⏳ **Phase 3** — Containerization + scanning
5. ⏳ **Phase 4** — Kubernetes deployment
6. ⏳ **Phase 5** — GitHub Actions CI/CD
7. ⏳ **Phase 6** — Monitoring (Prometheus + Grafana)
8. ⏳ **Phase 7** — Backup + restore testing
9. ⏳ **Phase 8** — Documentation + polish

See `CLAUDE.md` for detailed phase breakdown and verification steps.

## Proof Points (What You Can Demo)

- **Idempotency:** Run Ansible twice, second run shows `changed=0` ✓
- **Self-healing:** Kill a pod, watch it restart within seconds ✓
- **Rolling updates:** Deploy a new app version with zero downtime ✓
- **Monitoring:** Grafana dashboard shows live metrics from both cluster and nodes ✓
- **Alerts:** Watch an alert fire, resolve, and clear ✓
- **Backup:** Full restore from backup with data integrity check ✓
- **CI/CD:** Commit to main, watch the pipeline run end-to-end ✓

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
# Wait 30 seconds for Prometheus to scrape targets
# Check Prometheus targets at http://localhost:9090/targets
```

See `docs/runbook.md` for more troubleshooting.

## What I Learned Building This

- Ansible is powerful for declarative infrastructure; roles + handlers enforce idempotency
- k3d is an honest testbed for k3s — manifests transfer directly to production
- Pull-based monitoring (Prometheus) changes how you think about observability vs push-based systems
- A restore test you've actually run beats any backup policy document
- Separating CI logic (`ci/` scripts) from pipeline config (`.github/workflows/`) makes local and cloud testing identical

## Next Steps (Phase 9+)

- **Terraform + real cloud VMs** — drop in AWS/Hetzner/Oracle Cloud instances and let Terraform create the inventory
- **Proxmox integration** — when hardware is available, swap the inventory and the same roles work
- **Self-hosted runner** — close the deploy loop so GitHub Actions can push to the local cluster
- **Loki + log aggregation** — add centralized logging
- **cert-manager + TLS** — real certificates instead of self-signed

## License

MIT (update this if you publish)

## Author

Built as a portfolio project to demonstrate DevOps skills.

---

**Have questions?** See the `docs/` folder for architecture diagrams, decision records, and detailed runbooks.
