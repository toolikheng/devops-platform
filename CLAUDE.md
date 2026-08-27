# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**Self-Hosted Homelab DevOps Platform** — a portfolio project demonstrating end-to-end infrastructure, CI/CD, and operations skills. Designed to showcase:

- Infrastructure-as-Code (Ansible) provisioning Docker systemd "nodes" as a Multipass substitute on WSL2
- A containerized Python (FastAPI) microservice with health checks and Prometheus metrics
- Kubernetes deployment (k3d) with self-healing, rolling updates, and resource management
- Monitoring via Prometheus + Grafana with custom dashboards and alert rules
- Automated backup/restore (restic) with round-trip verification
- GitHub Actions CI/CD pipeline that builds, scans, and deploys

**Target audience:** DevOps/Infrastructure job applications. The portfolio value comes from Phases 0–8 being genuinely runnable and tested, not theoretical.

## Environment & Constraints

**Running on:** WSL2 Ubuntu 24.04.1 LTS (kernel 6.18), Windows 10 Home
- **No Proxmox/Hyper-V:** Windows 10 Home lacks Hyper-V; WSL2 has no nested virt (requires Win11+)
- **VM substitute:** Docker containers running systemd + sshd, provisioned by Ansible over SSH
- **Resources:** 8 CPU, 11 GB RAM, 954 GB free — ample for k3d + kube-prometheus-stack + 3 nodes
- **Docker:** Desktop edition with WSL2 integration enabled (Phase 0 requirement)

**Key assumption:** Ansible inventory is designed to be swappable — roles unchanged whether targeting Docker nodes or Proxmox/cloud VMs later. Inventory file (`ansible/inventory/docker-nodes.yml`) can be replaced with `proxmox.yml` or `cloud.yml` without touching playbooks.

## Folder Structure & Phase Mapping

```
DEVOPSPROJECT/
├── CLAUDE.md              ← You are here; session memory & architectural guidance
├── README.md              ← For recruiters/GitHub visitors (clean quickstart + why)
├── Makefile               ← Single entry point for all commands (see "Common Commands")
├── scripts/
│   └── bootstrap.sh       ← Phase 0: toolchain check/install
│
├── ansible/               ← Phase 1: Infrastructure provisioning
│   ├── ansible.cfg
│   ├── inventory/         ← docker-nodes.yml, proxmox.yml.example (swappable)
│   ├── playbooks/         ← site.yml (main), provision.yml (if split needed)
│   └── roles/             ← common/, node_exporter/ (idempotent, reusable)
│
├── infra/nodes/           ← Phase 1: Docker-based "VMs"
│   ├── Dockerfile.node    ← systemd + sshd + your SSH key
│   └── docker-compose.yml ← 3 nodes on a named network
│
├── app/                   ← Phase 2–3: FastAPI microservice + containerization
│   ├── pyproject.toml     ← Dependencies, pytest config, tool settings
│   ├── Dockerfile         ← Multi-stage, non-root user, HEALTHCHECK
│   ├── .dockerignore
│   ├── src/app/
│   │   ├── main.py        ← FastAPI app: /, /healthz, /readyz, /metrics, /items CRUD
│   │   ├── config.py      ← Pydantic-settings for 12-factor config
│   │   └── metrics.py     ← prometheus_client exports request counter/latency
│   └── tests/             ← pytest suite (written before impl; Phase 2)
│
├── k8s/                   ← Phase 4: Kubernetes manifests (kustomize-based)
│   ├── base/              ← namespace, deployment, service, ingress, PVC, configmap
│   └── overlays/
│       ├── dev/           ← 1 replica, localhost ingress
│       └── prod/          ← N replicas, DNS ingress (template for real use)
│
├── monitoring/            ← Phase 6: Prometheus + Grafana (Helm-based)
│   ├── helm-values.yaml   ← kube-prometheus-stack config + additionalScrapeConfigs
│   ├── dashboards/        ← JSON dashboard definitions (provisioned via ConfigMap)
│   ├── rules/             ← Prometheus alert rules (AppDown, HighErrorRate, etc.)
│   └── scrape-configs/    ← (if needed) Manual scrape configs alongside Helm
│
├── backup/                ← Phase 7: Automated backup & restore
│   └── scripts/
│       ├── backup.sh      ← restic job (retention policy, prune)
│       └── restore-test.sh ← Full round-trip: wipe data → restore → verify
│
├── ci/                    ← Phase 5: Shared CI logic (called by both Makefile & Actions)
│   ├── lint.sh
│   ├── test.sh
│   ├── build.sh
│   └── scan.sh
│
├── .github/workflows/     ← Phase 5: GitHub Actions (thin wrappers around ci/)
│   ├── ci.yml             ← lint → test → build → scan → push to GHCR
│   └── cd.yml             ← (manual) deploy job
│
└── docs/                  ← Phase 8: Documentation & decisions
    ├── architecture.md    ← High-level diagram (Mermaid + SVG)
    ├── decisions/         ← ADRs: why k3d, why Docker nodes, why pull-based CD
    ├── diagrams/          ← Architecture & flow diagrams
    └── runbook.md         ← Restore procedure, troubleshooting
```

## Phases & Status

| Phase | Goal | Status | Key Command |
|-------|------|--------|-------------|
| **0** | Toolchain bootstrap, repo init, SSH keys | ✅ Complete | `make check` → all green |
| **1** | Ansible + Docker nodes, idempotency proof | ⏳ Queued | `make infra-up && make provision` |
| **2** | FastAPI app + pytest (test-driven) | ⏳ Queued | `make test` |
| **3** | Containerize, image scan, security | ⏳ Queued | `make build` |
| **4** | k3d cluster, deployment, rolling updates | ⏳ Queued | `make cluster-up && make deploy` |
| **5** | GitHub Actions CI/CD pipeline | ⏳ Queued | Push to GitHub; see `.github/workflows/` |
| **6** | Prometheus + Grafana, alert rules | ⏳ Queued | `make monitoring-up` |
| **7** | restic backup, restore round-trip test | ⏳ Queued | `make restore-test` |
| **8** | Documentation, architecture diagram, polish | ⏳ Queued | `make destroy` + fresh clone test |
| **9** | Optional: Terraform, Proxmox, self-hosted runner | 🔴 Out of scope for MVP | — |

**Phase 0 completion criteria:**
- `make check` reports all tools PASS
- SSH key pair generated for Ansible + GitHub
- Git repo initialized in DEVOPSPROJECT/, first commit pushed to GitHub
- `CLAUDE.md` v1 ✓ + `README.md` v1 + `Makefile` v1 all committed

## Common Commands

**All commands are in the Makefile.** Run `make` or `make help` to see the full list.

### Frequently used:
```bash
# Bootstrap (Phase 0)
make check                # Verify toolchain is installed
make install              # Install missing tools (requires sudo)

# Infrastructure (Phase 1)
make infra-up            # Start 3 Docker nodes (systemd + sshd)
make infra-down          # Stop nodes
make provision           # Run Ansible playbook (idempotent)

# App & container (Phase 2–3)
make test                # pytest suite
make lint                # ruff + mypy
make run-app             # Run FastAPI locally (http://localhost:8000)
make build               # Build image + scan with trivy & hadolint

# Kubernetes (Phase 4)
make cluster-up          # Create k3d cluster (1 server + 2 agents)
make cluster-down        # Destroy cluster
make deploy              # Deploy app from k8s/overlays/dev

# Monitoring (Phase 6)
make monitoring-up       # Deploy kube-prometheus-stack via Helm
make monitoring-down     # Remove stack

# Backup (Phase 7)
make backup              # Run restic backup job
make restore-test        # Full restore round-trip test

# Utilities
make all                 # Full build: check → infra → provision → test → build → cluster → deploy
make destroy             # Tear down infra + cluster
make clean               # Remove build artifacts
```

## Key Decisions & Why

**1. Docker systemd nodes instead of Multipass**
- WSL2 on Win10 Home lacks nested virt + Hyper-V
- Docker nodes are free, run today, and let Ansible prove idempotency on real SSH
- Inventory swap strategy lets us move to Proxmox/cloud VMs later without rewriting roles

**2. k3d over kind**
- k3d runs real k3s (not just bare containerd), ships Traefik + service LB
- Manifests transfer to real k3s clusters with no modification
- Kind is more minimal but doesn't prepare you for production k3s

**3. GitHub Actions + GHCR (not self-hosted Gitea)**
- Public repo with visible green checkmarks is stronger portfolio signal
- GHCR auth is free via `GITHUB_TOKEN`, no secrets to manage
- Self-hosted runner is a Phase 9 stretch once basic pipeline works

**4. Ansible + simple roles (not Helm for infra)**
- Helm is overkill for 3 Docker nodes; Ansible proves IaC competency
- Node provisioning (packages, users, services) is simpler in roles
- Monitoring stack *does* use Helm because kube-prometheus-stack is complex

**5. kustomize for K8s (not pure kubectl)**
- Overlays (dev/prod) demonstrate real-world multi-environment practice
- Validation via `kubeconform` catches mistakes before apply

## Teaching Moments (What Each Phase Teaches)

- **Phase 1:** Ansible idempotency, roles, handlers, inventory management
- **Phase 2:** Test-driven development, liveness vs readiness probes, 12-factor config
- **Phase 3:** Docker image layers, multi-stage builds, security scanning, non-root users
- **Phase 4:** K8s resource model (deployment, service, ingress, PVC), self-healing, rolling updates
- **Phase 5:** CI/CD pipeline design, GitHub Actions, image tagging by commit SHA
- **Phase 6:** Pull-based monitoring, ServiceMonitor CRD, PromQL, alert rules
- **Phase 7:** Backup strategy (3-2-1 rule), RPO/RTO, restore testing (the crucial part)
- **Phase 8:** Architecture documentation, ADRs, production readiness checklist

## What You Must Know About This Project

1. **The plan is the source of truth.** Read `/plans/i-m-building-a-portfolio-tender-puddle.md` for the full scope, phase breakdown, and verification steps. CLAUDE.md is a session-local memory aid; the plan file is the architectural spec.

2. **Test before implement.** Every component (Ansible playbook, Dockerfile, K8s manifest, CI job, backup script) has verification steps documented *before* code is written. Run the test, show the failure, then implement, then show it passing. This is non-negotiable.

3. **Teach as you go.** Before the first use of any new tool (Ansible, Dockerfile, K8s manifest, GitHub Actions, Prometheus, restic), a short explanation of what it does and why — before the code. No surprises.

4. **End-to-end verification.** Every phase ends in something the user can actually run and see working. No theoretical components.

5. **Substitutions, stated honestly.** The README contains a table showing what's a WSL2 substitute (Docker nodes, k3d) vs what's real (GHCR, Ansible). This is portfolio strength, not weakness — it shows you know the tradeoffs.

## Resources & Links

- **Approved plan file:** `/plans/i-m-building-a-portfolio-tender-puddle.md` — detailed scope, verification steps, risks
- **GitHub repo:** (to be created in Phase 0)
- **Bootstrap script:** `scripts/bootstrap.sh --check` to verify tools, `--install` to add missing ones
- **Makefile:** `make help` for all available commands

## Phase 0 — Completion Summary (2026-08-28)

**What was built:**
- Folder structure (`ansible/`, `app/`, `k8s/`, `infra/`, `monitoring/`, `backup/`, `ci/`, `docs/`)
- `Makefile` — single entry point for all operations
- `bootstrap.sh` — toolchain verification script
- `CLAUDE.md` v1 (this file)
- `README.md` v1 — portfolio documentation
- `.gitignore` — standard Python/Docker/K8s exclusions
- Git repo initialized, 2 commits, pushed to GitHub

**Core tools installed & verified (all ✓):**
- git, ssh, curl, docker, kubectl, k3d, helm, make, jq, ansible, gh CLI
- SSH key pair (ed25519) generated for Ansible + GitHub

**GitHub repository:**
- Public repo at https://github.com/toolikheng/devops-platform
- Ready for CI/CD pipeline (Phase 5)

**Optional tools (for later phases, currently missing):**
- ansible-lint, kubeconform, hadolint, trivy, restic
- Will install in Phase 3 (scanning) and Phase 7 (backup)

**Phase 0 verification:** `make check` → all core tools green ✓

**Next phase entry point:** `make infra-up` (Phase 1 — Ansible infrastructure)

## Updating This File

Update CLAUDE.md at the end of each phase before the check-in step. Include:
- Phase completed (mark as ✓)
- What was built
- Key commands that now work
- Any new environment constraints discovered
- Next phase's entry point (i.e., which `make` command to run first)

Do not let CLAUDE.md drift from the actual state of the project.
