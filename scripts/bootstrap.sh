#!/bin/bash
# bootstrap.sh - Check toolchain completeness
# Usage: ./scripts/bootstrap.sh [--check]

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

# ANSI color codes
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# Phase 0 core tools (required)
CORE_TOOLS="git ssh curl docker kubectl k3d helm make jq ansible"

# Later-phase tools (optional for Phase 0)
OPTIONAL_TOOLS="ansible-lint kubeconform hadolint trivy restic gh"

check_tool() {
  local tool=$1
  if command -v "$tool" &>/dev/null; then
    local version=$(echo "$($tool --version 2>&1 || echo 'found')" | head -1 | cut -d' ' -f1-2 | sed 's/version //')
    echo -e "${GREEN}✓${NC} $tool ($version)"
    return 0
  else
    echo -e "${RED}✗${NC} $tool"
    return 1
  fi
}

main() {
  echo ""
  echo -e "${BLUE}=== DevOps Platform Bootstrap ===${NC}"
  echo ""

  local missing_core=0
  local missing_optional=0

  echo -e "${BLUE}Phase 0 - Core tools (required):${NC}"
  for tool in $CORE_TOOLS; do
    if ! check_tool "$tool"; then
      ((missing_core++))
    fi
  done

  echo ""
  echo -e "${BLUE}Later phases - Optional tools:${NC}"
  for tool in $OPTIONAL_TOOLS; do
    if ! check_tool "$tool"; then
      ((missing_optional++))
    fi
  done

  echo ""
  if [ $missing_core -eq 0 ]; then
    echo -e "${GREEN}✓ Phase 0 complete! All core tools ready.${NC}"
    if [ $missing_optional -gt 0 ]; then
      echo -e "${YELLOW}Note: $missing_optional optional tool(s) missing (needed for phases 3+).${NC}"
    fi
    echo ""
    return 0
  else
    echo -e "${RED}✗ Missing $missing_core core tool(s).${NC}"
    echo ""
    echo "Install missing tools in WSL:"
    echo "  sudo apt-get update && sudo apt-get install -y git curl ssh make jq ansible"
    echo ""
    return 1
  fi
}

main "$@"
