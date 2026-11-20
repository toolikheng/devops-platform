# Names that both stacks must agree on: the persistent stack creates these resources, and the ephemeral
# stack finds them by name. Both read the names from here, so a rename happens in one place. Scripts can't
# read Terraform, so tests/test_names.py checks their copies (owner-ip.sh) against this file.

output "node" {
  description = "The node's name: its IAM role and instance profile (persistent stack), and its instance, key pair and security group (ephemeral stack)."
  value       = "devops-platform-node"
}

output "owner_cidr_parameter" {
  description = "SSM parameter that holds the owner's public IPv4 as a /32 (written by make owner-ip)."
  value       = "/devops-platform/owner-cidr"
}
