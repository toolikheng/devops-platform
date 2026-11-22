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

# Letters and digits only (about 190 bits in 32 characters), so the password fits in DATABASE_URL without
# escaping.
ephemeral "random_password" "db_password" {
  length  = 32
  special = false
}

ephemeral "random_password" "admin_token" {
  length  = 32
  special = false
}

resource "aws_ssm_parameter" "db_password" {
  name             = "/policypal/aws/db-password"
  description      = "PolicyPal's Postgres password (POSTGRES_PASSWORD and DATABASE_URL). Rotation: docs/runbook.md."
  type             = "SecureString"
  tier             = "Standard"
  key_id           = "alias/aws/ssm"
  value_wo         = ephemeral.random_password.db_password.result
  value_wo_version = var.db_password_version
}

resource "aws_ssm_parameter" "admin_token" {
  name             = "/policypal/aws/admin-token"
  description      = "PolicyPal's admin token (X-Admin-Token). Rotation: raise admin_token_version, see docs/runbook.md."
  type             = "SecureString"
  tier             = "Standard"
  key_id           = "alias/aws/ssm"
  value_wo         = ephemeral.random_password.admin_token.result
  value_wo_version = var.admin_token_version
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
