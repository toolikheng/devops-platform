variable "name" {
  description = "Prefix for the Name tag of every resource in this module."
  type        = string
}

variable "az" {
  description = "Availability Zone for the single public subnet."
  type        = string
}

variable "vpc_cidr" {
  description = "Address range of the VPC."
  type        = string
  default     = "10.20.0.0/16"
}

variable "subnet_cidr" {
  description = "Address range of the public subnet (inside vpc_cidr)."
  type        = string
  default     = "10.20.1.0/24"
}

variable "owner_cidr" {
  description = "The owner's public IPv4 address as a /32. The only source allowed in on port 80."
  type        = string
  sensitive   = true

  validation {
    # Exactly one IPv4 address: only an IPv4 /32 has the netmask 255.255.255.255. cidrnetmask fails on IPv6,
    # a bare address, an invalid one and the "unset" placeholder the persistent stack stores until
    # `make owner-ip` runs; ranges such as 0.0.0.0/0 have a shorter mask.
    condition     = try(cidrnetmask(var.owner_cidr), "") == "255.255.255.255"
    error_message = "owner_cidr must be one IPv4 address as a /32 (for example 203.0.113.10/32). Run `make owner-ip`."
  }
}
