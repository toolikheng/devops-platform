variable "name" {
  description = "Name tag of the instance and name of its key pair."
  type        = string
}

variable "instance_type" {
  description = "EC2 instance type."
  type        = string

  validation {
    # The allowlist from the project's cost rules. Anything else needs a written, costed decision first.
    condition     = contains(["t3.micro", "t3.small", "t4g.micro", "t4g.small", "c7i-flex.large", "m7i-flex.large"], var.instance_type)
    error_message = "instance_type must be one of t3.micro, t3.small, t4g.micro, t4g.small, c7i-flex.large, m7i-flex.large."
  }
}

variable "use_spot" {
  description = "Run as a one-time Spot instance (cheaper, can be interrupted) instead of on-demand."
  type        = bool
  default     = false
}

variable "root_volume_gb" {
  description = "Size of the encrypted gp3 root volume in GB."
  type        = number
  default     = 20

  validation {
    # 8 GB is the Ubuntu AMI's own snapshot size; 30 GB is the project's cap.
    condition     = var.root_volume_gb >= 8 && var.root_volume_gb <= 30
    error_message = "root_volume_gb must be between 8 and 30."
  }
}

variable "subnet_id" {
  description = "Subnet to launch the instance in."
  type        = string
}

variable "security_group_ids" {
  description = "Security groups attached to the instance."
  type        = list(string)
}

variable "instance_profile_name" {
  description = "Instance profile that gives the node its IAM role."
  type        = string
}

variable "ssh_public_key" {
  description = "OpenSSH ed25519 public key for SSH tunnelled through Session Manager (used by Ansible)."
  type        = string

  validation {
    # Catches a private key (or anything else) passed by mistake before it could reach AWS or the state.
    condition     = can(regex("^ssh-ed25519 [A-Za-z0-9+/]+={0,2}( .*)?$", trimspace(var.ssh_public_key)))
    error_message = "ssh_public_key must be an OpenSSH ed25519 public key (the contents of ~/.ssh/id_ed25519.pub)."
  }
}
