# One EC2 instance: an allowlisted type, Ubuntu 24.04 amd64 from Canonical, IMDSv2 only, an encrypted gp3
# root volume that is deleted with the instance. Spot and on-demand are in spot.tftest.hcl.
#
# Every apply below uses an allowlisted type (t3.micro, or c7i-flex.large), so the runs that pass also
# prove valid input is accepted; the expect_failures runs prove invalid input is refused.

mock_provider "aws" {
  source = "./tests/mocks"
}

variables {
  name                  = "test"
  instance_type         = "t3.micro"
  subnet_id             = "subnet-0123456789abcdef0"
  security_group_ids    = ["sg-0123456789abcdef0"]
  instance_profile_name = "devops-platform-node"
  ssh_public_key        = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExampleExampleExampleExampleExampleExample test@example"
}

# --- Instance type allowlist (the project's cost rules) ---------------------------------------------

run "type_outside_the_allowlist_is_rejected" {
  command = plan
  variables {
    instance_type = "t3.medium"
  }
  expect_failures = [var.instance_type]
}

run "previous_generation_type_is_rejected" {
  command = plan
  variables {
    instance_type = "t2.micro"
  }
  expect_failures = [var.instance_type]
}

# t4g is on the allowlist but is ARM; the PolicyPal image is amd64 only. The precondition gives a clear
# error at plan time instead of a node that cannot run the app.
run "arm_type_fails_the_x86_precondition" {
  command = plan
  variables {
    instance_type = "t4g.micro"
  }
  override_data {
    target = data.aws_ec2_instance_type.selected
    values = {
      supported_architectures         = ["arm64"]
      burstable_performance_supported = true
    }
  }
  expect_failures = [aws_instance.node]
}

# --- Root volume --------------------------------------------------------------------------------------

run "root_volume_over_30_gb_is_rejected" {
  command = plan
  variables {
    root_volume_gb = 31
  }
  expect_failures = [var.root_volume_gb]
}

run "root_volume_smaller_than_the_ami_is_rejected" {
  command = plan
  variables {
    root_volume_gb = 7
  }
  expect_failures = [var.root_volume_gb]
}

run "root_volume_is_encrypted_gp3_and_deleted_with_the_instance" {
  command = apply

  assert {
    condition = (
      aws_instance.node.root_block_device[0].volume_type == "gp3" &&
      aws_instance.node.root_block_device[0].volume_size == 20 &&
      aws_instance.node.root_block_device[0].encrypted &&
      aws_instance.node.root_block_device[0].delete_on_termination
    )
    error_message = "Root volume must be 20 GB gp3, encrypted, and deleted on termination."
  }
}

# --- Instance settings --------------------------------------------------------------------------------

run "imdsv2_is_required_with_hop_limit_1" {
  command = apply

  assert {
    condition = (
      aws_instance.node.metadata_options[0].http_endpoint == "enabled" &&
      aws_instance.node.metadata_options[0].http_tokens == "required" &&
      aws_instance.node.metadata_options[0].http_put_response_hop_limit == 1
    )
    error_message = "IMDSv2 must be required, with hop limit 1 so ordinary pods cannot reach the node's credentials."
  }
}

run "instance_wiring_and_cost_settings" {
  command = apply

  assert {
    condition = (
      aws_instance.node.ami == "ami-0123456789abcdef0" &&
      aws_instance.node.subnet_id == "subnet-0123456789abcdef0" &&
      toset(aws_instance.node.vpc_security_group_ids) == toset(["sg-0123456789abcdef0"]) &&
      aws_instance.node.iam_instance_profile == "devops-platform-node" &&
      aws_instance.node.key_name == aws_key_pair.this.key_name
    )
    error_message = "The instance must use the looked-up AMI, the given subnet, security groups, profile and key pair."
  }

  assert {
    condition     = aws_instance.node.associate_public_ip_address
    error_message = "The node needs a public IPv4 for outbound access (there is no NAT gateway)."
  }

  assert {
    condition     = aws_instance.node.monitoring == false
    error_message = "Detailed monitoring is a paid CloudWatch feature."
  }

  # A shutdown from inside the node terminates it, so a stopped instance never sits there paying for its volume.
  assert {
    condition     = aws_instance.node.instance_initiated_shutdown_behavior == "terminate"
    error_message = "Shutting down from inside the node must terminate it."
  }
}

run "ami_is_ubuntu_24_04_amd64_from_canonical" {
  command = plan

  assert {
    condition     = length(data.aws_ami.ubuntu.owners) == 1 && data.aws_ami.ubuntu.owners[0] == "099720109477"
    error_message = "The AMI must come from Canonical's account (no Marketplace or community AMIs)."
  }

  assert {
    condition = anytrue([
      for f in data.aws_ami.ubuntu.filter :
      f.name == "name" && anytrue([for v in f.values : startswith(v, "ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-")])
    ])
    error_message = "The AMI must be Ubuntu 24.04 (noble) amd64 server."
  }
}

# --- CPU credits: never pay for surplus credits on T instances --------------------------------------

run "burstable_type_uses_standard_credits" {
  command = apply

  assert {
    condition     = aws_instance.node.credit_specification[0].cpu_credits == "standard"
    error_message = "T instances must use standard credits (unlimited mode can bill surplus credits)."
  }
}

run "non_burstable_type_has_no_credit_setting" {
  command = apply
  variables {
    instance_type = "c7i-flex.large"
  }
  override_data {
    target = data.aws_ec2_instance_type.selected
    values = {
      supported_architectures         = ["x86_64"]
      burstable_performance_supported = false
    }
  }

  assert {
    condition     = length(aws_instance.node.credit_specification) == 0
    error_message = "Only burstable types take a credit specification."
  }
}

# --- Key pair: public key only ------------------------------------------------------------------------

run "key_pair_uses_the_given_public_key" {
  command = apply

  assert {
    condition     = aws_key_pair.this.public_key == var.ssh_public_key && aws_key_pair.this.key_name == "test"
    error_message = "The key pair must be named after the node and hold the given public key."
  }
}

run "private_key_is_rejected" {
  command = plan
  variables {
    ssh_public_key = "-----BEGIN OPENSSH PRIVATE KEY-----"
  }
  expect_failures = [var.ssh_public_key]
}
