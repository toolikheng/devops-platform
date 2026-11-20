# At the monthly cap, AWS Budgets attaches a deny policy to the admin user automatically. It stops new
# spending but never blocks clean-up: deleting, Terraform state reads/writes and IAM all stay allowed, so
# the owner can always destroy what runs and detach the policy. Limits: running resources keep billing
# until destroyed, and budget data refreshes only about three times a day.

locals {
  account_id     = data.aws_caller_identity.current.account_id
  admin_user_arn = "arn:aws:iam::${local.account_id}:user/${var.admin_user_name}"

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
# account. Permission: attach or detach the deny policy, on the admin user only.
resource "aws_iam_role" "budget_action" {
  name        = "devops-platform-budget-action"
  description = "Assumed by AWS Budgets to attach the deny policy when the monthly cap is reached."
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "budgets.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = { StringEquals = { "aws:SourceAccount" = local.account_id } }
    }]
  })
}

resource "aws_iam_role_policy" "budget_action" {
  name = "attach-deny-new-spend"
  role = aws_iam_role.budget_action.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "AttachOnlyTheDenyPolicyToTheAdminUser"
      Effect    = "Allow"
      Action    = ["iam:AttachUserPolicy", "iam:DetachUserPolicy"]
      Resource  = local.admin_user_arn
      Condition = { ArnEquals = { "iam:PolicyARN" = aws_iam_policy.deny_new_spend.arn } }
    }]
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
      users      = [var.admin_user_name]
    }
  }

  subscriber {
    address           = var.alert_email
    subscription_type = "EMAIL"
  }

  # The role needs its permission before Budgets can use it.
  depends_on = [aws_iam_role_policy.budget_action]
}
