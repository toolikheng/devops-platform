# The ephemeral stack: everything billed by the hour. Created at the start of a work session with
# `make cloud-up` and destroyed at the end with `make cloud-down`.

locals {
  name = "devops-platform"
}

# Names shared with the persistent stack.
module "names" {
  source = "../../modules/names"
}

# Made by the persistent stack and found here by name, not through its remote state: planning this stack
# never needs read access to the persistent state.
data "aws_iam_instance_profile" "node" {
  name = module.names.node
}

# The owner's /32, written by `make owner-ip`. Terraform treats the value as sensitive, so it is redacted
# in plan output (and in public CI logs later). It is checked here, where it enters the stack: the
# persistent stack stores the placeholder "unset" until `make owner-ip` has run.
data "aws_ssm_parameter" "owner_cidr" {
  name = module.names.owner_cidr_parameter

  lifecycle {
    postcondition {
      condition     = try(cidrnetmask(self.value), "") == "255.255.255.255"
      error_message = "${module.names.owner_cidr_parameter} does not hold one IPv4 address as a /32. Run `make owner-ip`."
    }
  }
}

module "network" {
  source = "../../modules/network"

  name = local.name
  az   = var.az
}

module "compute" {
  source = "../../modules/compute"

  name                  = module.names.node
  instance_type         = var.instance_type
  use_spot              = var.use_spot
  subnet_id             = module.network.subnet_id
  vpc_id                = module.network.vpc_id
  owner_cidr            = data.aws_ssm_parameter.owner_cidr.value
  instance_profile_name = data.aws_iam_instance_profile.node.name
  ssh_public_key        = var.ssh_public_key
}
