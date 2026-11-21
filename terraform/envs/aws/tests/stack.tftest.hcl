# The ephemeral root stack: wires the network and compute modules together, tags everything as
# ephemeral, finds what the persistent stack made (instance profile, owner IP) by name, and checks the
# owner IP where it reads it. The rules for each part are tested in its own module.
#
# Compute is replaced by its outputs here, so these tests cross it only at its interface and survive
# changes inside it. Network stays real: it has no data sources to mock.

mock_provider "aws" {
  source = "./tests/mocks"
}

override_module {
  target = module.compute
  outputs = {
    instance_id = "i-0123456789abcdef0"
    public_ip   = "203.0.113.20"
    public_dns  = "ec2-203-0-113-20.ap-southeast-1.compute.amazonaws.com"
  }
}

variables {
  ssh_public_key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExampleExampleExampleExampleExampleExample test@example"
}

run "every_resource_gets_the_project_tags" {
  command = plan

  assert {
    condition = (
      length(output.default_tags) == 4 &&
      output.default_tags["Project"] == "devops-platform" &&
      output.default_tags["Owner"] == "toolikheng" &&
      output.default_tags["ManagedBy"] == "terraform" &&
      output.default_tags["Stack"] == "ephemeral"
    )
    error_message = "Default tags must be exactly Project, Owner, ManagedBy and Stack=ephemeral (Stack=ephemeral marks what may be destroyed)."
  }
}

# By name, not through the persistent stack's remote state, so a plan never needs to read that state.
run "persistent_resources_are_looked_up_by_name" {
  command = plan

  assert {
    condition     = data.aws_iam_instance_profile.node.name == module.names.node
    error_message = "The node must use the persistent stack's instance profile (shared node name)."
  }

  assert {
    condition     = data.aws_ssm_parameter.owner_cidr.name == module.names.owner_cidr_parameter
    error_message = "owner_cidr must come from the shared owner-cidr parameter."
  }
}

# The persistent stack creates the parameter as "unset" until `make owner-ip` stores a real /32.
run "owner_ip_never_set_fails_the_plan" {
  command = plan
  override_data {
    target = data.aws_ssm_parameter.owner_cidr
    values = {
      value = "unset"
    }
  }
  expect_failures = [data.aws_ssm_parameter.owner_cidr]
}

run "owner_ip_that_is_a_range_fails_the_plan" {
  command = plan
  override_data {
    target = data.aws_ssm_parameter.owner_cidr
    values = {
      value = "0.0.0.0/0"
    }
  }
  expect_failures = [data.aws_ssm_parameter.owner_cidr]
}

# From Phase C3 a session runs k3s, which needs 2 GiB, and runs on Spot unless asked otherwise (ADR 009):
# `make cloud-up CLOUD_ARGS='-var use_spot=false'` for on-demand.
run "default_node_is_a_small_spot_instance" {
  command = plan

  assert {
    condition     = var.instance_type == "t3.small"
    error_message = "The default node must be t3.small (k3s needs 2 GiB)."
  }

  assert {
    condition     = var.use_spot == true
    error_message = "The default node must be Spot; on-demand is the explicit choice (-var use_spot=false)."
  }
}

run "outputs_for_session_manager_and_ansible" {
  command = apply

  assert {
    condition = (
      output.instance_id == "i-0123456789abcdef0" &&
      output.public_ip == "203.0.113.20" &&
      output.public_dns == "ec2-203-0-113-20.ap-southeast-1.compute.amazonaws.com"
    )
    error_message = "The stack must expose the node's instance_id, public_ip and public_dns."
  }
}
