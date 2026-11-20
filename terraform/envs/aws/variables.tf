variable "instance_type" {
  description = "EC2 instance type (must be on the allowlist enforced by modules/compute)."
  type        = string
  default     = "t3.micro"
}

variable "use_spot" {
  description = "Run the node as a Spot instance."
  type        = bool
  default     = false
}

variable "root_volume_gb" {
  description = "Root volume size in GB."
  type        = number
  default     = 20
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
