# On-demand or Spot from one switch, use_spot.
#
# The first two runs share one state on purpose: the second switches the instance from the first to Spot.
# On an update the mock leaves values the module does not set as null, so an explicit Spot max price would
# show up there. (On a fresh launch the mock fills unset values with random text instead.)

mock_provider "aws" {
  source = "./tests/mocks"
}

variables {
  name                  = "test"
  instance_type         = "t3.micro"
  subnet_id             = "subnet-0123456789abcdef0"
  vpc_id                = "vpc-0123456789abcdef0"
  owner_cidr            = "203.0.113.10/32"
  instance_profile_name = "devops-platform-node"
  ssh_public_key        = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExampleExampleExampleExampleExampleExample test@example"
}

run "on_demand_by_default" {
  command = apply

  assert {
    condition     = length(aws_instance.node.instance_market_options) == 0
    error_message = "Without use_spot the instance must be on-demand."
  }
}

run "spot_is_one_time_terminate_with_no_max_price" {
  command = apply
  variables {
    use_spot = true
  }

  assert {
    condition = (
      aws_instance.node.instance_market_options[0].market_type == "spot" &&
      aws_instance.node.instance_market_options[0].spot_options[0].spot_instance_type == "one-time" &&
      aws_instance.node.instance_market_options[0].spot_options[0].instance_interruption_behavior == "terminate"
    )
    error_message = "Spot must be one-time and terminate on interruption (nothing left stopped and billing for its volume)."
  }

  # No max price means the cap is the on-demand price, so the node is never priced out mid-session.
  assert {
    condition     = aws_instance.node.instance_market_options[0].spot_options[0].max_price == null || aws_instance.node.instance_market_options[0].spot_options[0].max_price == ""
    error_message = "Do not set a Spot max price; the default cap is the on-demand price."
  }
}

# AWS refuses to change the shutdown behaviour of a Spot instance (UnsupportedOperation, found in the first
# live Spot apply), and a one-time Spot instance already terminates on shutdown (measured: AWS reported
# "terminate" without Terraform setting it). So the module must leave it to AWS.
run "spot_leaves_the_shutdown_behaviour_to_aws" {
  command = apply
  # A fresh launch, as in AWS, so nothing is carried over from the on-demand instance above.
  state_key = "fresh_spot_launch"
  variables {
    use_spot = true
  }

  assert {
    condition     = aws_instance.node.instance_initiated_shutdown_behavior == "set-by-aws"
    error_message = "Do not set instance_initiated_shutdown_behavior on a Spot instance: AWS rejects the change."
  }
}
