#!/usr/bin/env bash
# ephemeral-tf.sh - run one Terraform command in the ephemeral stack (terraform/envs/aws).
#
# Used by `make cloud-up` (apply), `make cloud-down` (destroy) and node-id.sh (`output`, read-only).
# Extra arguments go straight to Terraform, e.g. `apply -var use_spot=false`. Terraform shows its plan and waits for "yes": never pass
# -auto-approve. The SSH public key reaches Terraform as TF_VAR_ssh_public_key; Ansible uses it through
# Session Manager from Phase C3.
set -euo pipefail

key_file="${SSH_PUBLIC_KEY_FILE:-$HOME/.ssh/id_ed25519.pub}"
if [ ! -f "$key_file" ]; then
  echo "Missing $key_file: create it with ssh-keygen -t ed25519, or set SSH_PUBLIC_KEY_FILE." >&2
  exit 1
fi
TF_VAR_ssh_public_key="$(cat "$key_file")"
export TF_VAR_ssh_public_key

cd "$(dirname "$0")/../terraform/envs/aws"
terraform init -input=false >/dev/null
terraform "$@"
