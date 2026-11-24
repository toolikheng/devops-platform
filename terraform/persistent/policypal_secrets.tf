# PolicyPal's secrets in SSM Parameter Store, and the node's permission to read them.
#
# Terraform makes each value with an ephemeral random_password and writes it through a write-only argument
# (value_wo), so the value is in neither the plan nor the state and nobody ever sees it. The state keeps
# only value_wo_version: raising it (variables.tf) writes a new random value, which is how a secret is
# rotated. On the node, External Secrets Operator reads the parameters with the node's role and makes the
# Kubernetes Secrets (k8s/policypal/overlays/aws). scripts/check-state-secrets.sh checks the state.
#
# SecureString with the AWS managed key aws/ssm: encrypted at rest, no monthly key fee, and anyone allowed
# to read a parameter can decrypt it through SSM, so the node needs no kms: permission. Standard tier: free.

# The secrets, by name under /policypal/aws/: secret-catalog.yaml, which the tests also read.
locals {
  secrets = yamldecode(file("${path.module}/secret-catalog.yaml"))
}

# Letters and digits only (about 190 bits in 32 characters), so the password fits in DATABASE_URL without
# escaping.
ephemeral "random_password" "secret" {
  for_each = local.secrets

  length  = 32
  special = false
}

resource "aws_ssm_parameter" "secret" {
  for_each = local.secrets

  name             = "/policypal/aws/${each.key}"
  description      = each.value.description
  type             = "SecureString"
  tier             = "Standard"
  key_id           = "alias/aws/ssm"
  value_wo         = ephemeral.random_password.secret[each.key].result
  value_wo_version = var.secret_versions[each.key]
}

# The two parameters made before the catalog keep their values: Terraform moves them in the state instead of
# destroying and creating them. (Ephemeral values are never in the state, so there is nothing to move.)
moved {
  from = aws_ssm_parameter.db_password
  to   = aws_ssm_parameter.secret["db-password"]
}

moved {
  from = aws_ssm_parameter.admin_token
  to   = aws_ssm_parameter.secret["admin-token"]
}

# External Secrets Operator reads each parameter by its name (GetParameter, decrypted). Nothing else in
# Parameter Store: not /devops-platform/*, no listing, no writes.
resource "aws_iam_role_policy" "node_policypal_parameters" {
  name = "policypal-parameters"
  role = aws_iam_role.node.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "ReadPolicyPalParameters"
      Effect   = "Allow"
      Action   = ["ssm:GetParameter"]
      Resource = "arn:aws:ssm:${local.region}:${data.aws_caller_identity.current.account_id}:parameter/policypal/aws/*"
    }]
  })
}
