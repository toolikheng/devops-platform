#!/bin/bash
# bootstrap.sh - Check toolchain completeness or install missing tools
# Usage: ./scripts/bootstrap.sh [--check|--install]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

# ANSI color codes
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Tool checklist with versions we tested
declare -A TOOLS=(
  [git]="2.30+"
  [ssh]="7.4+"
  [curl]="7.50+"
  [docker]="20.10+"
  [kubectl]="1.24+"
  [k3d]="5.0+"
  [helm]="3.10+"
  [make]="4.0+"
  [jq]="1.6+"
  [ansible]="2.10+"
  [ansible-lint]="6.0+"
  [kubeconform]="0.4+"
  [hadolint]="2.8+"
  [trivy]="0.20+"
  [restic]="0.12+"
)

check_tool() {
  local tool=$1
  local min_version=${TOOLS[$tool]:-"any"}

  if ! command -v "$tool" &>/dev/null; then
    echo -e "${RED}✗${NC} $tool"
    return 1
  fi

  # For tools that output version on --version, show it; otherwise just confirm found
  local version=""
  if "$tool" --version &>/dev/null 2>&1; then
    version=$(echo "$($tool --version 2>&1 || true)" | head -1 | sed 's/.*version //' | cut -d' ' -f1)
    echo -e "${GREEN}✓${NC} $tool (${version:-unknown})"
  else
    echo -e "${GREEN}✓${NC} $tool (found)"
  fi
  return 0
}

print_status() {
  local mode=${1:-check}

  echo ""
  echo -e "${BLUE}=== DevOps Platform Bootstrap ===${NC}"
  echo -e "${BLUE}Mode: $mode${NC}"
  echo ""

  if [ "$mode" = "check" ]; then
    echo "Checking toolchain..."
  else
    echo "Installing/verifying toolchain..."
  fi
  echo ""
}

check_all() {
  local missing=0

  print_status "check"

  for tool in "${!TOOLS[@]}"; do
    if ! check_tool "$tool"; then
      ((missing++))
    fi
  done

  echo ""
  if [ $missing -eq 0 ]; then
    echo -e "${GREEN}✓ All tools present!${NC}"
    echo ""
    return 0
  else
    echo -e "${RED}✗ Missing $missing tool(s)${NC}"
    echo ""
    echo "To install missing tools, run:"
    echo "  ./scripts/bootstrap.sh --install"
    echo ""
    return 1
  fi
}

install_all() {
  print_status "install"

  # Ensure we're in WSL (Linux)
  if ! grep -qi "microsoft" /proc/version 2>/dev/null; then
    echo -e "${YELLOW}⚠${NC} Not running in WSL2. Some install steps may differ."
  fi

  echo "Updating package lists..."
  sudo apt-get update -qq

  echo ""
  echo "Installing system packages via apt..."

  # Core packages
  local packages=(
    git curl ssh make jq
    build-essential python3-pip python3-venv
  )
  sudo apt-get install -y "${packages[@]}" >/dev/null 2>&1

  echo ""
  echo "Installing CLI tools via apt..."
  sudo apt-get install -y \
    ansible \
    docker.io \
    >/dev/null 2>&1

  # Add user to docker group so we don't need sudo
  if ! groups | grep -q docker; then
    sudo usermod -aG docker "$USER"
    echo -e "${YELLOW}⚠${NC} Added $USER to docker group. You may need to log out/in or 'newgrp docker'."
  fi

  echo ""
  echo "Installing kubectl..."
  if ! command -v kubectl &>/dev/null; then
    curl -sLo /tmp/kubectl https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl
    sudo install -o root -g root -m 0755 /tmp/kubectl /usr/local/bin/kubectl
    rm -f /tmp/kubectl
  fi

  echo "Installing k3d..."
  if ! command -v k3d &>/dev/null; then
    curl -s https://raw.githubusercontent.com/k3d-io/k3d/main/install.sh | bash >/dev/null 2>&1
  fi

  echo "Installing helm..."
  if ! command -v helm &>/dev/null; then
    curl https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash >/dev/null 2>&1
  fi

  echo "Installing kubeconform..."
  if ! command -v kubeconform &>/dev/null; then
    wget https://github.com/yannh/kubeconform/releases/latest/download/kubeconform-linux-amd64.tar.gz -O - | tar xz -C /tmp/
    sudo mv /tmp/kubeconform /usr/local/bin/
  fi

  echo "Installing hadolint..."
  if ! command -v hadolint &>/dev/null; then
    curl -sL https://github.com/hadolint/hadolint/releases/latest/download/hadolint-Linux-x86_64 -o /tmp/hadolint
    sudo install -o root -g root -m 0755 /tmp/hadolint /usr/local/bin/hadolint
    rm -f /tmp/hadolint
  fi

  echo "Installing trivy..."
  if ! command -v trivy &>/dev/null; then
    curl -sfL https://raw.githubusercontent.com/aquasecurity/trivy/main/contrib/install.sh | sh -s -- -b /usr/local/bin >/dev/null 2>&1
  fi

  echo "Installing restic..."
  if ! command -v restic &>/dev/null; then
    curl -sL https://github.com/restic/restic/releases/download/v0.16.0/restic_0.16.0_linux_amd64.bz2 | bzip2 -d > /tmp/restic
    sudo install -o root -g root -m 0755 /tmp/restic /usr/local/bin/restic
    rm -f /tmp/restic
  fi

  echo "Installing ansible-lint via pipx..."
  if ! command -v pipx &>/dev/null; then
    pip3 install --user pipx >/dev/null 2>&1
  fi
  if ! command -v ansible-lint &>/dev/null; then
    pipx install ansible-lint >/dev/null 2>&1
  fi

  echo "Installing gh CLI..."
  if ! command -v gh &>/dev/null; then
    curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | sudo dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg >/dev/null 2>&1
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | sudo tee /etc/apt/sources.list.d/github-cli.sources > /dev/null
    sudo apt update >/dev/null 2>&1
    sudo apt install -y gh >/dev/null 2>&1
  fi

  echo ""
  echo -e "${GREEN}✓ Installation complete!${NC}"
  echo ""
  echo "Running final check..."
  echo ""

  check_all
}

main() {
  local mode="${1:-check}"

  case "$mode" in
    --check|-c|check)
      check_all
      ;;
    --install|-i|install)
      install_all
      ;;
    *)
      echo "Usage: $0 [--check|--install]"
      echo ""
      echo "  --check    Verify all tools are installed (default)"
      echo "  --install  Install missing tools"
      exit 1
      ;;
  esac
}

main "$@"
