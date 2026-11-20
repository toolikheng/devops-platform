# Shared mock data for the ephemeral stack's tests (no AWS calls are made).

override_data {
  target = data.aws_ssm_parameter.owner_cidr
  values = {
    value = "203.0.113.10/32"
  }
}

override_data {
  target = module.compute.data.aws_ec2_instance_type.selected
  values = {
    supported_architectures         = ["x86_64"]
    burstable_performance_supported = true
  }
}

override_data {
  target = module.compute.data.aws_ami.ubuntu
  values = {
    id           = "ami-0123456789abcdef0"
    architecture = "x86_64"
  }
}
