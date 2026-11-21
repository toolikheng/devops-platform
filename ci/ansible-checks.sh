#!/bin/bash
set -euo pipefail

# Offline Ansible checks for both playbooks (homelab site.yml and aws-node.yml): ansible-lint, then
# ansible-playbook --syntax-check with the ansible-core that really runs them. No hosts are contacted.
# ansible-lint installs the collections in ansible/requirements.yml into its own cache first.
# CI pins ansible-lint in the workflow; locally it comes from pipx (version recorded in the docs).

echo "=== Running Ansible checks ==="

cd "$(dirname "$0")/../ansible"

echo "--- ansible-lint $(ansible-lint --version 2>/dev/null | head -1 | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)"
# --project-dir: requirements.yml and the collection cache (.ansible/) live in ansible/, not the git root.
ansible-lint --nocolor --project-dir .

echo "--- syntax-check"
# Resolve collections from the cache ansible-lint just filled (CI's plain ansible-core has none).
export ANSIBLE_COLLECTIONS_PATH="$PWD/.ansible/collections${ANSIBLE_COLLECTIONS_PATH:+:$ANSIBLE_COLLECTIONS_PATH}"
ansible-playbook --syntax-check -i inventory/docker-nodes.yml playbooks/site.yml
# The AWS inventory is generated per session; a one-host stand-in for the aws group is enough to parse
# the playbook.
stand_in="$(mktemp --suffix=.yml)"
trap 'rm -f "$stand_in"' EXIT
printf 'aws:\n  hosts:\n    i-0123456789abcdef0:\n' > "$stand_in"
ansible-playbook --syntax-check -i "$stand_in" playbooks/aws-node.yml

echo "✓ Ansible checks passed"
