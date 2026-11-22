# At the monthly cap, AWS Budgets attaches a deny policy to the admin user and to the CI apply role
# (gha-tf-apply, the one GitHub role that can start instances) automatically. It stops new
# spending but never blocks clean-up: deleting, Terraform state reads/writes and IAM all stay allowed, so
# the owner can always destroy what runs and detach the policy. Limits: running resources keep billing
# until destroyed, and budget data refreshes only about three times a day.

data "aws_caller_identity" "current" {}

# Looked up rather than built from its name, so a wrong user name fails the plan instead of failing the
# action when the cap is reached.
data "aws_iam_user" "admin" {
  user_name = var.admin_user_name
}

locals {
  # Actions that start something billed by the hour, by size or per request.
  new_spend_actions = [
    "ec2:RunInstances",
    "ec2:StartInstances",
    "ec2:RequestSpotInstances",
    "ec2:RequestSpotFleet",
    "ec2:CreateFleet",
    "ec2:CreateVolume",
    "ec2:CreateSnapshot",
    "ec2:CreateSnapshots",
    "ec2:AllocateAddress",
    "ec2:CreateNatGateway",
    "ec2:CreateVpcEndpoint",
    "elasticloadbalancing:CreateLoadBalancer",
    "rds:CreateDBInstance",
    "rds:CreateDBCluster",
    "eks:CreateCluster",
    "eks:CreateNodegroup",
    "secretsmanager:CreateSecret",
    "kms:CreateKey",
    "route53:CreateHostedZone",
    "ecr:CreateRepository",
    "lightsail:Create*",
    "sagemaker:Create*",
    "bedrock:InvokeModel*",
  ]
}

resource "aws_iam_policy" "deny_new_spend" {
  name        = "devops-platform-deny-new-spend"
  description = "Attached by the monthly-cap budget action: blocks starting anything new that costs money."
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "DenyNewSpend"
      Effect   = "Deny"
      Action   = local.new_spend_actions
      Resource = "*"
    }]
  })
}

# The role AWS Budgets assumes to run the action. Trust: only the Budgets service, acting for this
# account. Permission: attach or detach the deny policy, on the admin user and gha-tf-apply only.
resource "aws_iam_role" "budget_action" {
  name        = "devops-platform-budget-action"
  description = "Assumed by AWS Budgets to attach the deny policy when the monthly cap is reached."
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "budgets.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = { StringEquals = { "aws:SourceAccount" = data.aws_caller_identity.current.account_id } }
    }]
  })
}

resource "aws_iam_role_policy" "budget_action" {
  name = "attach-deny-new-spend"
  role = aws_iam_role.budget_action.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AttachOnlyTheDenyPolicyToTheAdminUser"
        Effect    = "Allow"
        Action    = ["iam:AttachUserPolicy", "iam:DetachUserPolicy"]
        Resource  = data.aws_iam_user.admin.arn
        Condition = { ArnEquals = { "iam:PolicyARN" = aws_iam_policy.deny_new_spend.arn } }
      },
      {
        # The CI role that can start instances (terraform.yml apply) is stopped at the cap too.
        Sid       = "AttachOnlyTheDenyPolicyToTheCiApplyRole"
        Effect    = "Allow"
        Action    = ["iam:AttachRolePolicy", "iam:DetachRolePolicy"]
        Resource  = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/${aws_iam_role.github["gha-tf-apply"].name}"
        Condition = { ArnEquals = { "iam:PolicyARN" = aws_iam_policy.deny_new_spend.arn } }
      },
    ]
  })
}

resource "aws_budgets_budget_action" "deny_new_spend" {
  budget_name        = aws_budgets_budget.monthly_cap.name
  action_type        = "APPLY_IAM_POLICY"
  approval_model     = "AUTOMATIC"
  notification_type  = "ACTUAL"
  execution_role_arn = aws_iam_role.budget_action.arn

  action_threshold {
    action_threshold_type  = "ABSOLUTE_VALUE"
    action_threshold_value = var.monthly_budget_usd
  }

  definition {
    iam_action_definition {
      policy_arn = aws_iam_policy.deny_new_spend.arn
      users      = [data.aws_iam_user.admin.user_name]
      # The other GitHub roles cannot start anything that bills.
      roles = [aws_iam_role.github["gha-tf-apply"].name]
    }
  }

  subscriber {
    address           = var.alert_email
    subscription_type = "EMAIL"
  }

  # The role needs its permission before Budgets can use it.
  depends_on = [aws_iam_role_policy.budget_action]
}
