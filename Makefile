.PHONY: help check install \
        infra-up infra-down provision \
        venv test lint run-app image build \
        cluster-up cluster-down deploy \
        monitoring-up monitoring-down \
        backup restore-test \
        destroy all clean

SHELL := /bin/bash
.SHELLFLAGS := -euo pipefail -c

# Detect the current shell for echo -e compatibility
ECHO := echo -e

# Configuration
REPO_ROOT := $(shell pwd)
ANSIBLE_DIR := $(REPO_ROOT)/ansible
APP_DIR := $(REPO_ROOT)/app
K8S_DIR := $(REPO_ROOT)/k8s
INFRA_DIR := $(REPO_ROOT)/infra
MONITORING_DIR := $(REPO_ROOT)/monitoring
# kube-prometheus-stack chart. Helm doesn't upgrade CRDs, so bump this deliberately.
KPS_CHART_VERSION := 92.16.0
BACKUP_DIR := $(REPO_ROOT)/backup
CI_DIR := $(REPO_ROOT)/ci
# App virtualenv with the dev dependencies (created by `make venv`; gitignored).
VENV := $(APP_DIR)/.venv
VENV_STAMP := $(VENV)/.installed

# Color output (if tty)
ifeq ($(shell [ -t 1 ] && echo 1),1)
	RED := \033[0;31m
	GREEN := \033[0;32m
	YELLOW := \033[1;33m
	BLUE := \033[0;34m
	NC := \033[0m
else
	RED := ""
	GREEN := ""
	YELLOW := ""
	BLUE := ""
	NC := ""
endif

help:
	@$(ECHO) "$(BLUE)=== DevOps Platform Makefile ===$(NC)"
	@$(ECHO) ""
	@$(ECHO) "$(BLUE)Phase 0 - Bootstrap:$(NC)"
	@$(ECHO) "  $(GREEN)make check$(NC)          Check if all tools are installed"
	@$(ECHO) "  $(GREEN)make install$(NC)        Install missing apt packages (sudo) and SSH key"
	@$(ECHO) ""
	@$(ECHO) "$(BLUE)Phase 1 - Infrastructure (Ansible):$(NC)"
	@$(ECHO) "  $(GREEN)make infra-up$(NC)       Start Docker nodes"
	@$(ECHO) "  $(GREEN)make infra-down$(NC)     Stop Docker nodes"
	@$(ECHO) "  $(GREEN)make provision$(NC)     Run Ansible playbook on nodes"
	@$(ECHO) ""
	@$(ECHO) "$(BLUE)Phase 2 - Application:$(NC)"
	@$(ECHO) "  $(GREEN)make venv$(NC)          Create app/.venv with dev dependencies (automatic)"
	@$(ECHO) "  $(GREEN)make test$(NC)          Run pytest suite"
	@$(ECHO) "  $(GREEN)make lint$(NC)          Run linters (ruff, mypy)"
	@$(ECHO) "  $(GREEN)make run-app$(NC)       Run the app locally on :8000"
	@$(ECHO) ""
	@$(ECHO) "$(BLUE)Phase 3 - Containerize:$(NC)"
	@$(ECHO) "  $(GREEN)make image$(NC)         Build the Docker image only"
	@$(ECHO) "  $(GREEN)make build$(NC)         Build + hadolint + Trivy gate"
	@$(ECHO) ""
	@$(ECHO) "$(BLUE)Phase 4 - Kubernetes:$(NC)"
	@$(ECHO) "  $(GREEN)make cluster-up$(NC)    Create k3d cluster"
	@$(ECHO) "  $(GREEN)make cluster-down$(NC)  Destroy k3d cluster"
	@$(ECHO) "  $(GREEN)make deploy$(NC)        Deploy app to cluster"
	@$(ECHO) ""
	@$(ECHO) "$(BLUE)Phase 5 - CI/CD:$(NC)"
	@$(ECHO) "  (Managed via GitHub Actions)"
	@$(ECHO) ""
	@$(ECHO) "$(BLUE)Phase 6 - Monitoring:$(NC)"
	@$(ECHO) "  $(GREEN)make monitoring-up$(NC)   Deploy Prometheus + Grafana"
	@$(ECHO) "  $(GREEN)make monitoring-down$(NC) Remove monitoring stack"
	@$(ECHO) ""
	@$(ECHO) "$(BLUE)Phase 7 - Backup:$(NC)"
	@$(ECHO) "  $(GREEN)make backup$(NC)        Run backup job"
	@$(ECHO) "  $(GREEN)make restore-test$(NC)  Test restore procedure"
	@$(ECHO) ""
	@$(ECHO) "$(BLUE)Utilities:$(NC)"
	@$(ECHO) "  $(GREEN)make all$(NC)           Run all phases (check → infra → deploy)"
	@$(ECHO) "  $(GREEN)make destroy$(NC)       Tear down everything"
	@$(ECHO) "  $(GREEN)make clean$(NC)         Remove build artifacts"
	@$(ECHO) ""

# Phase 0 - Bootstrap
check:
	@bash "$(REPO_ROOT)/scripts/bootstrap.sh" --check

install:
	@bash "$(REPO_ROOT)/scripts/bootstrap.sh" --install

# Phase 1 - Infrastructure
infra-up:
	@$(ECHO) "$(BLUE)Starting Docker nodes...$(NC)"
	@docker-compose -f "$(INFRA_DIR)/nodes/docker-compose.yml" up -d --build
	@$(ECHO) "$(BLUE)Waiting for nodes to be SSH-ready...$(NC)"
	@for node in devops-node1 devops-node2 devops-node3; do \
		echo "Checking $$node..."; \
		for i in {1..60}; do \
			if docker exec $$node ssh-keyscan localhost >/dev/null 2>&1; then \
				echo "✓ $$node is ready"; \
				break; \
			fi; \
			sleep 1; \
		done; \
	done
	@$(ECHO) "$(GREEN)✓ All nodes started and SSH-ready$(NC)"
	@docker ps --filter "label=devops.role=node" --format "table {{.Names}}\t{{.Status}}"

infra-down:
	@$(ECHO) "$(BLUE)Stopping Docker nodes...$(NC)"
	@docker-compose -f "$(INFRA_DIR)/nodes/docker-compose.yml" down
	@$(ECHO) "$(GREEN)✓ Nodes stopped$(NC)"

provision:
	@$(ECHO) "$(BLUE)Running Ansible playbook...$(NC)"
	@cd "$(ANSIBLE_DIR)" && ansible-playbook playbooks/site.yml

# Phase 2 - Application
venv: $(VENV_STAMP)

# Rebuilt only when pyproject.toml changes.
$(VENV_STAMP): $(APP_DIR)/pyproject.toml
	@$(ECHO) "$(BLUE)Creating $(VENV) with the app's dev dependencies...$(NC)"
	@python3 -m venv "$(VENV)"
	@"$(VENV)/bin/python" -m pip install -q --upgrade pip
	@"$(VENV)/bin/python" -m pip install -q -e "$(APP_DIR)[dev]"
	@touch "$@"

# Same scripts as CI, with the venv first on PATH.
test: venv
	@PATH="$(VENV)/bin:$$PATH" bash "$(CI_DIR)/test.sh"

lint: venv
	@PATH="$(VENV)/bin:$$PATH" bash "$(CI_DIR)/lint.sh"

run-app: venv
	@$(ECHO) "$(BLUE)Running FastAPI app on http://localhost:8000 ...$(NC)"
	@cd "$(APP_DIR)" && "$(VENV)/bin/python" -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload --reload-dir src

# Phase 3 - Containerize
# hadolint and trivy run as pinned Docker images (same versions and thresholds as CI); nothing to install.
HADOLINT_IMAGE := hadolint/hadolint:v2.12.0-debian

image:
	@bash "$(CI_DIR)/build.sh" devops-app:latest

build: image
	@$(ECHO) "$(BLUE)Linting Dockerfile (hadolint)...$(NC)"
	@docker run --rm -i $(HADOLINT_IMAGE) hadolint --ignore DL3008 - < "$(APP_DIR)/Dockerfile"
	@bash "$(CI_DIR)/scan.sh" devops-app:latest

# Phase 4 - Kubernetes
CLUSTER := devops

# Create the cluster, or start it if it already exists (a no-op when it's running). Errors are not hidden.
cluster-up:
	@if k3d cluster get $(CLUSTER) >/dev/null 2>&1; then \
		$(ECHO) "$(BLUE)Starting existing k3d cluster '$(CLUSTER)'...$(NC)"; \
		k3d cluster start $(CLUSTER) --wait; \
	else \
		$(ECHO) "$(BLUE)Creating k3d cluster '$(CLUSTER)'...$(NC)"; \
		k3d cluster create $(CLUSTER) -p "8080:80@loadbalancer" --agents 2 --wait; \
	fi
	@kubectl config use-context k3d-$(CLUSTER) >/dev/null
	@kubectl wait --for=condition=Ready nodes --all --timeout=120s >/dev/null
	@$(ECHO) "$(GREEN)✓ Cluster ready$(NC)"

cluster-down:
	@if k3d cluster get $(CLUSTER) >/dev/null 2>&1; then \
		$(ECHO) "$(BLUE)Destroying k3d cluster '$(CLUSTER)'...$(NC)"; \
		k3d cluster delete $(CLUSTER); \
	else \
		echo "No k3d cluster '$(CLUSTER)'"; \
	fi
	@$(ECHO) "$(GREEN)✓ Cluster removed$(NC)"

secrets-init:
	@kubectl create namespace devops --dry-run=client -o yaml | kubectl apply -f -
	@kubectl -n devops get secret backup-credentials >/dev/null 2>&1 || \
		kubectl -n devops create secret generic backup-credentials --from-literal=password="$$(openssl rand -base64 24)"

# Build the image, load it into k3d, apply the dev overlay, and roll out the new image.
deploy: image secrets-init
	@$(ECHO) "$(BLUE)Importing devops-app:latest into k3d...$(NC)"
	@k3d image import devops-app:latest -c $(CLUSTER)
	@$(ECHO) "$(BLUE)Deploying app to Kubernetes...$(NC)"
	@kubectl apply -k "$(K8S_DIR)/overlays/dev"
	@kubectl apply -f "$(BACKUP_DIR)/cronjob.yaml"
	@# The dev tag is always :latest, so the spec doesn't change; restart to pick up the imported image.
	@kubectl -n devops rollout restart deploy/devops-app
	@kubectl -n devops rollout status deploy/devops-app --timeout=180s

# Phase 6 - Monitoring
monitoring-up:
	@$(ECHO) "$(BLUE)Deploying monitoring stack...$(NC)"
	@kubectl create namespace monitoring 2>/dev/null || true
	@kubectl -n monitoring get secret grafana-admin >/dev/null 2>&1 || \
		kubectl -n monitoring create secret generic grafana-admin \
		--from-literal=admin-user=admin --from-literal=admin-password="$$(openssl rand -base64 18)"
	@helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
	@helm repo update
	@helm upgrade --install kube-prometheus-stack \
		prometheus-community/kube-prometheus-stack \
		--version $(KPS_CHART_VERSION) \
		-n monitoring \
		-f "$(MONITORING_DIR)/helm-values.yaml"
	@kubectl apply -f "$(MONITORING_DIR)/servicemonitor.yaml" -f "$(MONITORING_DIR)/rules/alert-rules.yaml"
	@$(ECHO) "$(GREEN)✓ Monitoring stack deployed$(NC)"

monitoring-down:
	@$(ECHO) "$(BLUE)Removing monitoring stack...$(NC)"
	@helm uninstall kube-prometheus-stack -n monitoring || true
	@kubectl delete namespace monitoring || true

# Phase 7 - Backup
backup:
	@$(ECHO) "$(BLUE)Running backup job...$(NC)"
	@name=manual-backup-$$(date +%s); \
		kubectl -n devops create job $$name --from=cronjob/devops-app-backup && \
		kubectl -n devops wait --for=condition=complete job/$$name --timeout=300s && \
		kubectl -n devops logs job/$$name

restore-test:
	@$(ECHO) "$(BLUE)Testing restore procedure...$(NC)"
	@bash "$(BACKUP_DIR)/scripts/restore-test.sh"

# Utilities
all: check infra-up provision test build cluster-up deploy monitoring-up
	@$(ECHO) "$(GREEN)✓ All phases complete!$(NC)"

destroy: cluster-down infra-down
	@$(ECHO) "$(GREEN)✓ Infrastructure destroyed$(NC)"

clean:
	@$(ECHO) "$(BLUE)Cleaning up...$(NC)"
	@cd $(APP_DIR) && rm -rf build dist *.egg-info .pytest_cache .mypy_cache .ruff_cache
	@docker image rm -f devops-app:latest || true
	@$(ECHO) "$(GREEN)✓ Clean complete$(NC)"

.DEFAULT_GOAL := help
