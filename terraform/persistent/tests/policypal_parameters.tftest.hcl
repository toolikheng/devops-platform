# PolicyPal's two secrets live in SSM Parameter Store as SecureStrings, encrypted with the AWS managed key
# aws/ssm (no monthly key fee). Terraform writes a random value through a write-only argument (value_wo),
# so the value is in neither the plan nor the state: only a version number is stored, and raising it
# writes a new value. The node may read these parameters and nothing else in Parameter Store.
#
# The mock fills an attribute the configuration doesn't set with a marker (tests/mocks/aws.tfmock.hcl). A
# value from the configuration would replace the marker, so finding the marker after apply proves the
# configuration never sets value or insecure_value.

mock_provider "aws" {
  source = "./tests/mocks"
}

variables {
  alert_email = "alerts@example.com"
}

run "parameters_are_securestrings_under_the_policypal_path" {
  command = apply

  assert {
    condition     = aws_ssm_parameter.db_password.name == "/policypal/aws/db-password"
    error_message = "The database password must be /policypal/aws/db-password (the node may read /policypal/aws/* only)."
  }

  assert {
    condition     = aws_ssm_parameter.admin_token.name == "/policypal/aws/admin-token"
    error_message = "The admin token must be /policypal/aws/admin-token (the node may read /policypal/aws/* only)."
  }

  assert {
    condition = alltrue([
      for p in [aws_ssm_parameter.db_password, aws_ssm_parameter.admin_token] :
      p.type == "SecureString" && p.tier == "Standard" && p.key_id == "alias/aws/ssm"
    ])
    error_message = "Secrets must be Standard-tier SecureStrings (free) encrypted with the AWS managed key aws/ssm (no key fee)."
  }
}

run "secret_values_never_reach_terraform_state" {
  command = apply

  assert {
    condition = alltrue([
      for p in [aws_ssm_parameter.db_password, aws_ssm_parameter.admin_token] :
      p.value == "NOT-SET-IN-CONFIG" && p.insecure_value == "NOT-SET-IN-CONFIG" && p.value_wo == null
    ])
    error_message = "Set secrets with value_wo only: value and insecure_value are stored in state."
  }

  assert {
    condition = (
      aws_ssm_parameter.db_password.value_wo_version == var.db_password_version &&
      aws_ssm_parameter.admin_token.value_wo_version == var.admin_token_version
    )
    error_message = "Each secret's value_wo_version must come from its variable; it is the only thing state keeps."
  }
}

run "raising_a_version_rotates_only_that_secret" {
  command = plan

  variables {
    admin_token_version = 3
  }

  assert {
    condition     = aws_ssm_parameter.admin_token.value_wo_version == 3
    error_message = "admin_token_version must set the admin token's value_wo_version."
  }

  assert {
    condition     = aws_ssm_parameter.db_password.value_wo_version == var.db_password_version
    error_message = "Rotating the admin token must not touch the database password."
  }
}

run "versions_are_whole_numbers_from_1" {
  command = plan

  variables {
    db_password_version = 1.5
    admin_token_version = 0
  }

  expect_failures = [var.db_password_version, var.admin_token_version]
}

run "node_reads_only_the_policypal_parameters" {
  command = apply

  assert {
    condition     = aws_iam_role_policy.node_policypal_parameters.role == aws_iam_role.node.id
    error_message = "The parameter policy must be attached to the node role."
  }

  # One statement: read one parameter at a time, only under /policypal/aws/, in this account and region.
  # The aws/ssm key lets anyone who may read the parameter decrypt it, so no kms: action is needed.
  assert {
    condition = (
      length(jsondecode(aws_iam_role_policy.node_policypal_parameters.policy).Statement) == 1 &&
      alltrue([
        for s in jsondecode(aws_iam_role_policy.node_policypal_parameters.policy).Statement :
        s.Effect == "Allow" && !can(s.NotAction) && !can(s.NotResource) &&
        s.Action == ["ssm:GetParameter"] &&
        s.Resource == "arn:aws:ssm:ap-southeast-1:111122223333:parameter/policypal/aws/*"
      ])
    )
    error_message = "The node may only ssm:GetParameter on arn:aws:ssm:ap-southeast-1:<account>:parameter/policypal/aws/*."
  }
}
