.PHONY: help check install \
        infra-up infra-down provision \
        venv test lint run-app image build \
        cluster-up cluster-down deploy \
        monitoring-up monitoring-down \
        backup restore-test \
        test-infra cost-check tf-check ansible-check owner-ip cloud-up cloud-down \
        cloud-config cloud-tunnel cloud-rebuild \
        k8s-check policypal-secrets-local policypal-local cloud-deploy cloud-smoke \
        cloud-addons check-state-secrets \
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
# Repo-level tests in tests/ have their own venv (gitignored).
TESTS_VENV := $(REPO_ROOT)/tests/.venv
TESTS_VENV_STAMP := $(TESTS_VENV)/.installed
# AWS profile for the AWS targets (`aws login --profile devops` first). The region is fixed in each stack
# and script (ap-southeast-1), not taken from the shell.
AWS_PROFILE ?= devops
# Extra Terraform arguments for cloud-up/cloud-down, e.g. CLOUD_ARGS='-var use_spot=false' for on-demand.
CLOUD_ARGS ?=
# Where make cloud-config writes the generated AWS inventory (gitignored; tests point it elsewhere).
AWS_INVENTORY ?= $(ANSIBLE_DIR)/inventory/aws.yml
# Extra ansible-playbook arguments for cloud-config, e.g. ANSIBLE_ARGS='--check --diff'.
ANSIBLE_ARGS ?=
# The AWS cluster's kubeconfig, written by make cloud-config (kubeconfig_path in ansible/inventory/group_vars/aws.yml).
AWS_KUBECONFIG ?= $(HOME)/.kube/devops-aws.yaml
# PolicyPal's UI on the local k3d cluster (k3d maps localhost:8080 to Traefik).
POLICYPAL_LOCAL_URL := http://policypal.localhost:8080

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
	@$(ECHO) "  $(GREEN)make test-infra$(NC)    Run the repo-level tests in tests/ (scripts with fake CLIs, manifests, repo rules)"
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
	@$(ECHO) "  $(GREEN)make policypal-local$(NC) Deploy PolicyPal to k3d and smoke-test it (http://policypal.localhost:8080)"
	@$(ECHO) "  $(GREEN)make policypal-secrets-local$(NC) Create PolicyPal's random Secrets in k3d (once; never printed)"
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
	@$(ECHO) "$(BLUE)AWS (v2):$(NC)"
	@$(ECHO) "  $(GREEN)make cost-check$(NC)    Read-only scan for billable AWS leftovers (all regions)"
	@$(ECHO) "  $(GREEN)make tf-check$(NC)      Offline Terraform checks: fmt, validate, test, tflint, trivy"
	@$(ECHO) "  $(GREEN)make ansible-check$(NC) Offline Ansible checks: ansible-lint, syntax-check (both playbooks)"
	@$(ECHO) "  $(GREEN)make k8s-check$(NC)     Offline Kubernetes checks: render PolicyPal (both overlays) and External Secrets Operator, kubeconform -strict"
	@$(ECHO) "  $(GREEN)make owner-ip$(NC)      Store your public IP (/32) for the node's firewall (AWS write)"
	@$(ECHO) "  $(GREEN)make cloud-up$(NC)      Store your IP, then create the ephemeral stack (asks before applying; billed hourly)"
	@$(ECHO) "  $(GREEN)make cloud-down$(NC)    Destroy the ephemeral stack (asks first), then run cost-check"
	@$(ECHO) "  $(GREEN)make cloud-config$(NC)  Configure the running node with Ansible over Session Manager (common, node_exporter, k3s)"
	@$(ECHO) "  $(GREEN)make cloud-tunnel$(NC)  Forward the node's Kubernetes API to localhost:16443 (foreground; Ctrl-C stops it)"
	@$(ECHO) "  $(GREEN)make cloud-rebuild$(NC) cloud-up, then cloud-config: from nothing to a Ready k3s node"
	@$(ECHO) "  $(GREEN)make cloud-addons$(NC)  Install External Secrets Operator on the node (needs make cloud-tunnel running)"
	@$(ECHO) "  $(GREEN)make cloud-deploy$(NC)  Deploy PolicyPal to the node and smoke-test it (needs make cloud-tunnel running)"
	@$(ECHO) "  $(GREEN)make cloud-smoke$(NC)   Smoke-test PolicyPal on the node only (needs make cloud-tunnel running)"
	@$(ECHO) "  $(GREEN)make check-state-secrets$(NC) Read-only: show that no secret value is in the persistent Terraform state"
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

# Same pattern for the repo-level tests in tests/, which have their own small venv.
$(TESTS_VENV_STAMP): $(REPO_ROOT)/tests/requirements.txt
	@$(ECHO) "$(BLUE)Creating $(TESTS_VENV)...$(NC)"
	@python3 -m venv "$(TESTS_VENV)"
	@"$(TESTS_VENV)/bin/python" -m pip install -q --upgrade pip
	@"$(TESTS_VENV)/bin/python" -m pip install -q -r "$(REPO_ROOT)/tests/requirements.txt"
	@touch "$@"

# Same scripts as CI, with the venv first on PATH.
test: venv
	@PATH="$(VENV)/bin:$$PATH" bash "$(CI_DIR)/test.sh"

lint: venv
	@PATH="$(VENV)/bin:$$PATH" bash "$(CI_DIR)/lint.sh"

test-infra: $(TESTS_VENV_STAMP)
	@"$(TESTS_VENV)/bin/python" -m pytest -q "$(REPO_ROOT)/tests"

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
	@kubectl apply -k "$(MONITORING_DIR)/dashboards"   # Grafana sidecar picks it up (label grafana_dashboard=1)
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
		kubectl -n devops logs job/$$name --all-containers --prefix

restore-test:
	@$(ECHO) "$(BLUE)Testing restore procedure...$(NC)"
	@bash "$(BACKUP_DIR)/scripts/restore-test.sh"

# AWS (v2)
# Offline: no AWS credentials needed (backends skipped, tests use mock providers).
tf-check:
	@bash "$(CI_DIR)/terraform-checks.sh"

ansible-check:
	@bash "$(CI_DIR)/ansible-checks.sh"

k8s-check:
	@bash "$(CI_DIR)/k8s-checks.sh"

# PolicyPal on the local k3d cluster: cluster-up makes k3d-devops kubectl's current context first.
policypal-secrets-local: cluster-up
	@bash "$(REPO_ROOT)/scripts/policypal-secrets.sh"

policypal-local: policypal-secrets-local
	@bash "$(REPO_ROOT)/scripts/deploy-policypal.sh" local "$(POLICYPAL_LOCAL_URL)"

# Read-only: describe/list/get calls only, never the Cost Explorer API. Exit 1 if anything billable is left.
cost-check:
	@AWS_PROFILE="$(AWS_PROFILE)" bash "$(REPO_ROOT)/scripts/cost-check.sh"

# Read-only: terraform state pull and ssm get-parameter. Prints names and counts, never a value.
check-state-secrets:
	@AWS_PROFILE="$(AWS_PROFILE)" bash "$(REPO_ROOT)/scripts/check-state-secrets.sh"

# AWS writes. owner-ip overwrites one free SSM parameter with this machine's IP straight away. cloud-up
# runs it first, then shows Terraform's plan and waits for "yes", as cloud-down does; never add -auto-approve.
owner-ip:
	@AWS_PROFILE="$(AWS_PROFILE)" bash "$(REPO_ROOT)/scripts/owner-ip.sh"

cloud-up: owner-ip
	@AWS_PROFILE="$(AWS_PROFILE)" bash "$(REPO_ROOT)/scripts/ephemeral-tf.sh" apply $(CLOUD_ARGS)

# cost-check runs only after a successful destroy, and fails the target if anything billable is left.
cloud-down:
	@AWS_PROFILE="$(AWS_PROFILE)" bash "$(REPO_ROOT)/scripts/ephemeral-tf.sh" destroy $(CLOUD_ARGS)
	@$(MAKE) --no-print-directory cost-check

# No AWS changes: these read Terraform output and talk to the node through Session Manager (free).
# The SSH ProxyCommand in ansible/inventory/group_vars/aws.yml calls aws ssm with AWS_PROFILE.
cloud-config:
	@AWS_PROFILE="$(AWS_PROFILE)" bash "$(REPO_ROOT)/scripts/aws-inventory.sh" "$(AWS_INVENTORY)"
	@cd "$(ANSIBLE_DIR)" && AWS_PROFILE="$(AWS_PROFILE)" ansible-playbook -i "$(AWS_INVENTORY)" playbooks/aws-node.yml $(ANSIBLE_ARGS)

cloud-tunnel:
	@AWS_PROFILE="$(AWS_PROFILE)" bash "$(REPO_ROOT)/scripts/cloud-tunnel.sh"

# One command from nothing to a Ready node. cloud-up still shows the plan and waits for "yes".
cloud-rebuild:
	@$(MAKE) --no-print-directory cloud-up
	@$(MAKE) --no-print-directory cloud-config

# On the AWS node. No AWS changes: kubectl and helm go through make cloud-tunnel (run it in another
# terminal), and the UI is checked at the node's public IP, which only your IP can reach. cloud-addons
# installs External Secrets Operator, which makes PolicyPal's Secrets from SSM Parameter Store, so it runs
# once per new node, before cloud-deploy.
cloud-addons:
	@KUBECONFIG="$(AWS_KUBECONFIG)" bash "$(REPO_ROOT)/scripts/install-eso.sh"

cloud-deploy:
	@ip="$$(AWS_PROFILE="$(AWS_PROFILE)" bash "$(REPO_ROOT)/scripts/node-ip.sh")"; \
		KUBECONFIG="$(AWS_KUBECONFIG)" bash "$(REPO_ROOT)/scripts/deploy-policypal.sh" aws "http://$$ip"

cloud-smoke:
	@ip="$$(AWS_PROFILE="$(AWS_PROFILE)" bash "$(REPO_ROOT)/scripts/node-ip.sh")"; \
		KUBECONFIG="$(AWS_KUBECONFIG)" bash "$(REPO_ROOT)/scripts/smoke-test.sh" "http://$$ip"

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
