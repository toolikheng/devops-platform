# The policy the budget action attaches at the cap, and the role AWS Budgets uses to attach it.
#
# The deny policy must block new spending but never lock out clean-up or the owner. Denied actions are
# matched as IAM matches them: case-insensitive, with * and ? wildcards. So "*", "iam:*" or "s3:Put*"
# would all fail these tests, not only exact names.

mock_provider "aws" {
  source = "./tests/mocks"
}

variables {
  alert_email = "alerts@example.com"
}

run "deny_policy_blocks_new_spending" {
  command = apply

  assert {
    condition = alltrue([
      for s in jsondecode(aws_iam_policy.deny_new_spend.policy).Statement :
      s.Effect == "Deny" && !can(s.NotAction) && !can(s.NotResource)
    ])
    error_message = "Every statement must be a plain Deny (no Allow, NotAction or NotResource)."
  }

  assert {
    condition = alltrue([
      for required in [
        "ec2:RunInstances", "ec2:StartInstances", "ec2:RequestSpotInstances", "ec2:CreateFleet",
        "ec2:CreateVolume", "ec2:AllocateAddress", "ec2:CreateNatGateway", "ec2:CreateVpcEndpoint",
        "elasticloadbalancing:CreateLoadBalancer", "rds:CreateDBInstance", "rds:CreateDBCluster",
        "eks:CreateCluster", "secretsmanager:CreateSecret", "kms:CreateKey", "route53:CreateHostedZone",
        "ecr:CreateRepository",
      ] :
      contains(flatten([for s in jsondecode(aws_iam_policy.deny_new_spend.policy).Statement : s.Action]), required)
    ])
    error_message = "The deny policy must block every action that starts something billable."
  }
}

run "deny_policy_never_denies_any_iam_action" {
  command = apply

  # The owner must always be able to detach the policy (iam:DetachUserPolicy) and manage IAM.
  assert {
    condition = alltrue([
      for a in flatten([for s in jsondecode(aws_iam_policy.deny_new_spend.policy).Statement : s.Action]) :
      !can(regex("^${replace(replace(lower(split(":", a)[0]), "*", ".*"), "?", ".")}$", "iam"))
    ])
    error_message = "The deny policy must not match any iam: action, including through a wildcard."
  }
}

run "deny_policy_never_blocks_state_writes_or_clean_up" {
  command = apply

  # Destroy must still read and write Terraform state, and delete what is running.
  assert {
    condition = alltrue([
      for a in flatten([for s in jsondecode(aws_iam_policy.deny_new_spend.policy).Statement : s.Action]) :
      !anytrue([
        for allowed in [
          "s3:getobject", "s3:putobject", "s3:deleteobject", "s3:listbucket",
          "ec2:terminateinstances", "ec2:stopinstances", "ec2:deletevolume", "ec2:deletesnapshot",
          "ec2:releaseaddress", "ec2:deletenatgateway", "ec2:deletevpcendpoints",
          "elasticloadbalancing:deleteloadbalancer", "rds:deletedbinstance", "rds:deletedbcluster",
          "eks:deletecluster", "secretsmanager:deletesecret",
        ] :
        can(regex("^${replace(replace(lower(a), "*", ".*"), "?", ".")}$", allowed))
      ])
    ])
    error_message = "The deny policy must never block state reads/writes (s3:GetObject/PutObject) or clean-up actions."
  }
}

run "budget_action_role_can_only_attach_the_deny_policy_to_the_admin_user" {
  command = apply

  assert {
    condition = alltrue([
      for s in jsondecode(aws_iam_role.budget_action.assume_role_policy).Statement :
      s.Effect == "Allow" && s.Action == "sts:AssumeRole" &&
      s.Principal == { Service = "budgets.amazonaws.com" } &&
      s.Condition.StringEquals["aws:SourceAccount"] == "111122223333"
    ])
    error_message = "Only AWS Budgets, acting for this account, may assume the budget-action role."
  }

  assert {
    condition = alltrue([
      for s in jsondecode(aws_iam_role_policy.budget_action.policy).Statement :
      s.Effect == "Allow" &&
      length(setsubtract(toset(flatten([s.Action])), toset(["iam:AttachUserPolicy", "iam:DetachUserPolicy"]))) == 0 &&
      s.Resource == "arn:aws:iam::111122223333:user/toolikheng-admin" &&
      s.Condition.ArnEquals["iam:PolicyARN"] == aws_iam_policy.deny_new_spend.arn
    ])
    error_message = "The budget-action role may only attach or detach the deny policy, and only on toolikheng-admin."
  }
}
