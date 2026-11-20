# The owner's public IP (/32) lives in a free Standard SSM parameter. `make owner-ip` writes the real
# value; Terraform only creates the parameter with a placeholder and then leaves the value alone.
#
# The placeholder is deliberately not a CIDR: if `make owner-ip` was never run, the ephemeral stack's
# owner_cidr validation fails the plan, instead of creating a security group rule for a made-up address.

mock_provider "aws" {
  source = "./tests/mocks"
}

variables {
  alert_email = "alerts@example.com"
}

run "owner_cidr_parameter" {
  command = apply

  assert {
    condition     = aws_ssm_parameter.owner_cidr.name == module.names.owner_cidr_parameter
    error_message = "The parameter must use the shared name, which the ephemeral stack and owner-ip.sh read."
  }

  assert {
    condition     = aws_ssm_parameter.owner_cidr.type == "String"
    error_message = "An IP address is not a secret: use String, so reading it needs no KMS permission."
  }

  assert {
    condition     = aws_ssm_parameter.owner_cidr.tier == "Standard"
    error_message = "Only the Standard tier is free."
  }

  assert {
    condition     = !can(cidrhost(aws_ssm_parameter.owner_cidr.value, 0))
    error_message = "The placeholder must not be a CIDR, so a forgotten `make owner-ip` fails the ephemeral plan."
  }
}
