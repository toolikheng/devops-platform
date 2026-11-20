# The ephemeral stack: everything billed by the hour. Created at the start of a work session with
# `make cloud-up` and destroyed at the end with `make cloud-down`.

locals {
  name = "devops-platform"
}

# Made by the persistent stack and found here by name, not through its remote state: planning this stack
# never needs read access to the persistent state.
data "aws_iam_instance_profile" "node" {
  name = "devops-platform-node"
}

# The owner's /32, written by `make owner-ip`. Terraform treats the value as sensitive, so it is redacted
# in plan output (and in public CI logs later).
data "aws_ssm_parameter" "owner_cidr" {
  name = "/devops-platform/owner-cidr"
}

module "network" {
  source = "../../modules/network"

  name       = local.name
  az         = var.az
  owner_cidr = data.aws_ssm_parameter.owner_cidr.value
}

module "compute" {
  source = "../../modules/compute"

  name                  = "${local.name}-node"
  instance_type         = var.instance_type
  use_spot              = var.use_spot
  root_volume_gb        = var.root_volume_gb
  subnet_id             = module.network.subnet_id
  security_group_ids    = [module.network.security_group_id]
  instance_profile_name = data.aws_iam_instance_profile.node.name
  ssh_public_key        = var.ssh_public_key
}
