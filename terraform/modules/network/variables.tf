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
