# The secrets live in SSM Parameter Store as SecureStrings, encrypted with the AWS managed key aws/ssm (no
# monthly key fee), one per entry of secret-catalog.yaml. Terraform writes a random value through a write-only
# argument (value_wo), so the value is in neither the plan nor the state: only a version number is stored,
# and raising it writes a new value. The node may read these parameters and nothing else in Parameter Store.
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

run "one_securestring_per_catalog_entry_under_the_policypal_path" {
  command = apply

  assert {
    condition     = toset(keys(aws_ssm_parameter.secret)) == toset(["admin-token", "db-password", "restic-password"])
    error_message = "One parameter per entry of secret-catalog.yaml: admin-token, db-password, restic-password."
  }

  assert {
    condition     = alltrue([for k, p in aws_ssm_parameter.secret : p.name == "/policypal/aws/${k}"])
    error_message = "Each secret must be /policypal/aws/<its catalog name> (the node may read /policypal/aws/* only)."
  }

  assert {
    condition = alltrue([
      for p in values(aws_ssm_parameter.secret) :
      p.type == "SecureString" && p.tier == "Standard" && p.key_id == "alias/aws/ssm"
    ])
    error_message = "Secrets must be Standard-tier SecureStrings (free) encrypted with the AWS managed key aws/ssm (no key fee)."
  }

  # The descriptions, word for word. A write-only parameter can't change anything but its tags on its own: the
  # AWS provider (6.73.0, resourceParameterUpdate) then writes a new parameter version with the value from the
  # state, and the state holds no value (that is the point of value_wo). So a description changes only together
  # with its secret's version, which sends a new random value with it. The admin token's still names the old
  # variable until its next rotation (ADR 023).
  assert {
    condition = (
      aws_ssm_parameter.secret["db-password"].description == "PolicyPal's Postgres password (POSTGRES_PASSWORD and DATABASE_URL). Rotation: docs/runbook.md." &&
      aws_ssm_parameter.secret["admin-token"].description == "PolicyPal's admin token (X-Admin-Token). Rotation: raise admin_token_version, see docs/runbook.md." &&
      aws_ssm_parameter.secret["restic-password"].description == "The restic repository password for PolicyPal's backups in S3. Read by the node's backup job. Not rotated: see docs/runbook.md."
    )
    error_message = "A description may change only together with its secret's version (a rotation): see the comment above."
  }
}

run "secret_values_never_reach_terraform_state" {
  command = apply

  # value_wo itself can't be checked here: a write-only attribute always reads as null, whatever the
  # configuration sets. tests/test_terraform_secrets.py checks that it comes from an ephemeral resource.
  assert {
    condition = alltrue([
      for p in values(aws_ssm_parameter.secret) :
      p.value == "NOT-SET-IN-CONFIG" && p.insecure_value == "NOT-SET-IN-CONFIG"
    ])
    error_message = "Set secrets with value_wo only: value and insecure_value are stored in state."
  }

  assert {
    condition     = alltrue([for k, p in aws_ssm_parameter.secret : p.value_wo_version == var.secret_versions[k]])
    error_message = "Each secret's value_wo_version must come from secret_versions; it is the only thing state keeps."
  }
}

run "raising_a_version_rotates_only_that_secret" {
  command = plan

  variables {
    secret_versions = { "db-password" = 1, "admin-token" = 3, "restic-password" = 1 }
  }

  assert {
    condition     = aws_ssm_parameter.secret["admin-token"].value_wo_version == 3
    error_message = "secret_versions[\"admin-token\"] must set the admin token's value_wo_version."
  }

  assert {
    condition     = aws_ssm_parameter.secret["db-password"].value_wo_version == 1
    error_message = "Rotating the admin token must not touch the database password."
  }
}

run "versions_are_whole_numbers_from_1" {
  command = plan

  variables {
    secret_versions = { "db-password" = 1.5, "admin-token" = 0, "restic-password" = 1 }
  }

  expect_failures = [var.secret_versions]
}

run "every_secret_has_a_version" {
  command = plan

  variables {
    secret_versions = { "db-password" = 1, "admin-token" = 2 }
  }

  expect_failures = [var.secret_versions]
}

run "no_version_for_a_secret_that_isnt_in_the_catalog" {
  command = plan

  variables {
    secret_versions = { "db-password" = 1, "admin-token" = 2, "restic-password" = 1, "db-pasword" = 1 }
  }

  expect_failures = [var.secret_versions]
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
