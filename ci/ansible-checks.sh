#!/bin/bash
set -euo pipefail

# Offline Ansible checks: ansible-lint over everything in ansible/. Its syntax-check rule runs
# `ansible-playbook --syntax-check` on both playbooks (site.yml and aws-node.yml) and the roles they use,
# so no separate syntax check is needed. No hosts are contacted. ansible-lint first installs the
# collections in ansible/requirements.yml into ansible/.ansible/. CI pins ansible-lint in the workflow.

echo "=== Running Ansible checks ==="

# --project-dir: requirements.yml and the collection cache live in ansible/, not the git root.
cd "$(dirname "$0")/../ansible"
ansible-lint --nocolor --project-dir .

echo "✓ Ansible checks passed"
