variable "instance_type" {
  description = "EC2 instance type (must be on the allowlist enforced by modules/compute). t3.small: k3s needs 2 GiB."
  type        = string
  default     = "t3.small"
}

variable "use_spot" {
  description = "Run the node as a Spot instance (ADR 009). On-demand: -var use_spot=false."
  type        = bool
  default     = true
}

variable "az" {
  description = "Availability Zone for the subnet and the node."
  type        = string
  default     = "ap-southeast-1a"
}

variable "ssh_public_key" {
  description = "OpenSSH ed25519 public key (make cloud-up passes ~/.ssh/id_ed25519.pub)."
  type        = string
}
