# Shared mock data for the compute module's tests (no AWS calls are made).

# An x86_64, burstable (T-family) type unless a run says otherwise.
override_data {
  target = data.aws_ec2_instance_type.selected
  values = {
    supported_architectures         = ["x86_64"]
    burstable_performance_supported = true
  }
}

override_data {
  target = data.aws_ami.ubuntu
  values = {
    id           = "ami-0123456789abcdef0"
    architecture = "x86_64"
  }
}

# What the mock fills in when the module leaves an attribute for AWS to decide.
mock_resource "aws_instance" {
  defaults = {
    instance_initiated_shutdown_behavior = "set-by-aws"
  }
}
