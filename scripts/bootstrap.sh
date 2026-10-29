#!/bin/bash
# bootstrap.sh - check (default) or install what `make all` needs on WSL2 Ubuntu.
# Usage: scripts/bootstrap.sh [--check | --install]
#
# hadolint, trivy, promtool and yq are not needed on the host: the Makefile and ci/ scripts run them as
# pinned Docker images.
set -uo pipefail

GREEN='\033[0;32m'; RED='\033[0;31m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'

# command:apt-package - installed by --install with sudo apt-get.
APT_TOOLS="git:git ssh:openssh-client curl:curl make:make jq:jq openssl:openssl ansible:ansible python3:python3"
# Not in Ubuntu's default repositories; --install prints where to get them.
UPSTREAM_TOOLS="docker kubectl k3d helm"
declare -A UPSTREAM_URL=(
  [docker]="Docker Desktop with WSL integration: https://docs.docker.com/desktop/features/wsl/"
  [kubectl]="https://kubernetes.io/docs/tasks/tools/install-kubectl-linux/"
  [k3d]="https://k3d.io/#installation"
  [helm]="https://helm.sh/docs/intro/install/"
)
SSH_KEY="$HOME/.ssh/id_ed25519" # ansible.cfg and infra/nodes/docker-compose.yml expect this key

have() { command -v "$1" >/dev/null 2>&1; }
venv_ok() { python3 -c 'import ensurepip, venv' >/dev/null 2>&1; }
pass() { echo -e "${GREEN}✓${NC} $1"; }
fail() { echo -e "${RED}✗${NC} $1"; }

# Fills MISSING_APT (apt packages) and MISSING_OTHER (anything else); prints a report.
check() {
  MISSING_APT=()
  MISSING_OTHER=()
  echo -e "${BLUE}=== Toolchain for make all ===${NC}"
  for entry in $APT_TOOLS; do
    local tool=${entry%%:*} pkg=${entry#*:}
    if have "$tool"; then pass "$tool"; else fail "$tool (apt: $pkg)"; MISSING_APT+=("$pkg"); fi
  done
  if have python3 && venv_ok; then pass "python3 venv module"; else fail "python3 venv module (apt: python3-venv)"; MISSING_APT+=("python3-venv"); fi
  for tool in $UPSTREAM_TOOLS; do
    if have "$tool"; then pass "$tool"; else fail "$tool"; MISSING_OTHER+=("$tool"); fi
  done
  if have docker && docker info >/dev/null 2>&1; then pass "Docker daemon reachable"; else fail "Docker daemon reachable (start Docker Desktop)"; MISSING_OTHER+=("docker-daemon"); fi
  if [ -f "$SSH_KEY" ] && [ -f "$SSH_KEY.pub" ]; then pass "SSH key $SSH_KEY"; else fail "SSH key $SSH_KEY"; MISSING_OTHER+=("ssh-key"); fi
  [ ${#MISSING_APT[@]} -eq 0 ] && [ ${#MISSING_OTHER[@]} -eq 0 ]
}

install() {
  check && { echo -e "${GREEN}✓ Nothing to install.${NC}"; return 0; }
  echo
  if [ ${#MISSING_APT[@]} -gt 0 ]; then
    echo -e "${BLUE}Installing apt packages: ${MISSING_APT[*]}${NC}"
    sudo apt-get update && sudo apt-get install -y "${MISSING_APT[@]}" || return 1
  fi
  for item in "${MISSING_OTHER[@]}"; do
    case "$item" in
      ssh-key)
        echo -e "${BLUE}Generating SSH key $SSH_KEY${NC}"
        mkdir -p "$HOME/.ssh" && chmod 700 "$HOME/.ssh"
        ssh-keygen -t ed25519 -N "" -C "devops@homelab" -f "$SSH_KEY" ;;
      docker-daemon) echo -e "${YELLOW}Start Docker Desktop, then re-run.${NC}" ;;
      *) echo -e "${YELLOW}Install $item manually: ${UPSTREAM_URL[$item]}${NC}" ;;
    esac
  done
  echo
  check
}

case "${1:---check}" in
  --check) check && echo -e "${GREEN}✓ All set.${NC}" || { echo -e "${RED}Run 'make install' to fix what it can.${NC}"; exit 1; } ;;
  --install) install || exit 1 ;;
  *) echo "Usage: $0 [--check | --install]" >&2; exit 2 ;;
esac
