# Shared mock data for the ephemeral stack's tests (no AWS calls are made).

# The owner's IP as `make owner-ip` stores it.
override_data {
  target = data.aws_ssm_parameter.owner_cidr
  values = {
    value = "203.0.113.10/32"
  }
}
