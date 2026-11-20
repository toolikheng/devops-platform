# The ephemeral root stack: wires the network and compute modules together, tags everything as
# ephemeral, and finds what the persistent stack made (instance profile, owner IP) by name. The rules for
# each part are tested in its own module.

mock_provider "aws" {
  source = "./tests/mocks"
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
    condition     = data.aws_iam_instance_profile.node.name == "devops-platform-node"
    error_message = "The node must use the persistent stack's instance profile."
  }

  assert {
    condition     = data.aws_ssm_parameter.owner_cidr.name == "/devops-platform/owner-cidr"
    error_message = "owner_cidr must come from the /devops-platform/owner-cidr parameter."
  }
}

run "outputs_for_session_manager_and_ansible" {
  command = apply

  assert {
    condition = (
      output.instance_id == module.compute.instance_id &&
      output.public_ip == module.compute.public_ip &&
      output.public_dns == module.compute.public_dns
    )
    error_message = "The stack must expose instance_id, public_ip and public_dns from the compute module."
  }
}
