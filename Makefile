.PHONY: help check install \
        infra-up infra-down provision \
        test lint build \
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
BACKUP_DIR := $(REPO_ROOT)/backup
CI_DIR := $(REPO_ROOT)/ci

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
	@$(ECHO) "  $(GREEN)make install$(NC)        Install missing tools (requires sudo)"
	@$(ECHO) ""
	@$(ECHO) "$(BLUE)Phase 1 - Infrastructure (Ansible):$(NC)"
	@$(ECHO) "  $(GREEN)make infra-up$(NC)       Start Docker nodes"
	@$(ECHO) "  $(GREEN)make infra-down$(NC)     Stop Docker nodes"
	@$(ECHO) "  $(GREEN)make provision$(NC)     Run Ansible playbook on nodes"
	@$(ECHO) ""
	@$(ECHO) "$(BLUE)Phase 2 - Application:$(NC)"
	@$(ECHO) "  $(GREEN)make test$(NC)          Run pytest suite"
	@$(ECHO) "  $(GREEN)make lint$(NC)          Run linters (ruff, mypy)"
	@$(ECHO) ""
	@$(ECHO) "$(BLUE)Phase 3 - Containerize:$(NC)"
	@$(ECHO) "  $(GREEN)make build$(NC)         Build Docker image"
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
	@$(REPO_ROOT)/scripts/bootstrap.sh --check

install:
	@$(REPO_ROOT)/scripts/bootstrap.sh --install

# Phase 1 - Infrastructure
infra-up:
	@$(ECHO) "$(BLUE)Starting Docker nodes...$(NC)"
	@cd $(INFRA_DIR)/nodes && docker-compose up -d
	@sleep 2
	@$(ECHO) "$(GREEN)✓ Nodes started$(NC)"
	@docker ps --filter "label=devops.role=node" --format "table {{.Names}}\t{{.Status}}"

infra-down:
	@$(ECHO) "$(BLUE)Stopping Docker nodes...$(NC)"
	@cd $(INFRA_DIR)/nodes && docker-compose down
	@$(ECHO) "$(GREEN)✓ Nodes stopped$(NC)"

provision:
	@$(ECHO) "$(BLUE)Running Ansible playbook...$(NC)"
	@cd $(ANSIBLE_DIR) && ansible-playbook playbooks/site.yml -v

# Phase 2 - Application
test:
	@$(ECHO) "$(BLUE)Running tests...$(NC)"
	@cd $(APP_DIR) && python3 -m pytest tests/ -v --tb=short

lint:
	@$(ECHO) "$(BLUE)Running linters...$(NC)"
	@cd $(APP_DIR) && python3 -m ruff check . && echo "✓ ruff"
	@cd $(APP_DIR) && python3 -m mypy src/app/ && echo "✓ mypy"

run-app:
	@$(ECHO) "$(BLUE)Running FastAPI app...$(NC)"
	@cd $(APP_DIR) && python3 -m uvicorn src.app.main:app --host 0.0.0.0 --port 8000 --reload

# Phase 3 - Containerize
build:
	@$(ECHO) "$(BLUE)Building Docker image...$(NC)"
	@docker build -t devops-app:latest -f $(APP_DIR)/Dockerfile $(APP_DIR)
	@hadolint $(APP_DIR)/Dockerfile
	@$(ECHO) "$(BLUE)Scanning image...$(NC)"
	@trivy image --severity HIGH,CRITICAL devops-app:latest

# Phase 4 - Kubernetes
cluster-up:
	@$(ECHO) "$(BLUE)Creating k3d cluster...$(NC)"
	@k3d cluster create devops -p "8080:80@loadbalancer" --agents 2 || true
	@$(ECHO) "$(GREEN)✓ Cluster ready$(NC)"

cluster-down:
	@$(ECHO) "$(BLUE)Destroying k3d cluster...$(NC)"
	@k3d cluster delete devops || true
	@$(ECHO) "$(GREEN)✓ Cluster removed$(NC)"

deploy:
	@$(ECHO) "$(BLUE)Deploying app to Kubernetes...$(NC)"
	@kubectl apply -k $(K8S_DIR)/overlays/dev
	@kubectl rollout status deploy/devops-app -n devops || echo "Rollout in progress..."

# Phase 6 - Monitoring
monitoring-up:
	@$(ECHO) "$(BLUE)Deploying monitoring stack...$(NC)"
	@kubectl create namespace monitoring 2>/dev/null || true
	@helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
	@helm repo update
	@helm upgrade --install kube-prometheus-stack \
		prometheus-community/kube-prometheus-stack \
		-n monitoring \
		-f $(MONITORING_DIR)/helm-values.yaml
	@$(ECHO) "$(GREEN)✓ Monitoring stack deployed$(NC)"

monitoring-down:
	@$(ECHO) "$(BLUE)Removing monitoring stack...$(NC)"
	@helm uninstall kube-prometheus-stack -n monitoring || true
	@kubectl delete namespace monitoring || true

# Phase 7 - Backup
backup:
	@$(ECHO) "$(BLUE)Running backup...$(NC)"
	@bash $(BACKUP_DIR)/scripts/backup.sh

restore-test:
	@$(ECHO) "$(BLUE)Testing restore procedure...$(NC)"
	@bash $(BACKUP_DIR)/scripts/restore-test.sh

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
