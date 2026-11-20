# The EC2 node's identity: a role only EC2 can assume, with just enough permission for the SSM agent
# (Session Manager shell and Run Command), wrapped in an instance profile the ephemeral stack looks up by
# name.
#
# The permission set is checked as an exact list on purpose. The usual shortcut, the managed policy
# AmazonSSMManagedInstanceCore, also allows ssm:GetParameter on every parameter, so any process on the
# node could read every secret. Parameter access is added in C5, scoped to /policypal/aws/* only.

mock_provider "aws" {
  source = "./tests/mocks"
}

variables {
  alert_email = "alerts@example.com"
}

run "only_ec2_can_assume_the_node_role" {
  command = apply

  assert {
    condition     = aws_iam_role.node.name == module.names.node
    error_message = "The node role must use the shared node name from modules/names."
  }

  assert {
    condition = alltrue([
      for s in jsondecode(aws_iam_role.node.assume_role_policy).Statement :
      s.Effect == "Allow" && s.Action == "sts:AssumeRole" && s.Principal == { Service = "ec2.amazonaws.com" }
    ])
    error_message = "Only the EC2 service may assume the node role."
  }
}

run "node_role_has_only_the_ssm_agent_permissions" {
  command = apply

  assert {
    condition = alltrue([
      for s in jsondecode(aws_iam_role_policy.node_ssm_agent.policy).Statement :
      s.Effect == "Allow" && !can(s.NotAction) && !can(s.NotResource)
    ])
    error_message = "Every node statement must be a plain Allow (no NotAction or NotResource)."
  }

  assert {
    condition = (
      toset(flatten([for s in jsondecode(aws_iam_role_policy.node_ssm_agent.policy).Statement : s.Action])) ==
      toset([
        "ssm:UpdateInstanceInformation",
        "ssmmessages:CreateControlChannel",
        "ssmmessages:CreateDataChannel",
        "ssmmessages:OpenControlChannel",
        "ssmmessages:OpenDataChannel",
        "ec2messages:AcknowledgeMessage",
        "ec2messages:DeleteMessage",
        "ec2messages:FailMessage",
        "ec2messages:GetEndpoint",
        "ec2messages:GetMessages",
        "ec2messages:SendReply",
      ])
    )
    error_message = "The node role must allow exactly the SSM agent actions: no wildcards, no ssm:GetParameter*, nothing else."
  }

  # The one action that takes a resource is limited to this account's instances in the project's region.
  assert {
    condition = anytrue([
      for s in jsondecode(aws_iam_role_policy.node_ssm_agent.policy).Statement :
      s.Action == ["ssm:UpdateInstanceInformation"] &&
      s.Resource == "arn:aws:ec2:ap-southeast-1:111122223333:instance/*"
    ])
    error_message = "ssm:UpdateInstanceInformation must be limited to this account's instances in ap-southeast-1."
  }
}

run "instance_profile_wraps_the_node_role" {
  command = apply

  assert {
    condition     = aws_iam_instance_profile.node.name == module.names.node
    error_message = "The instance profile must use the shared node name, which the ephemeral stack looks up."
  }

  assert {
    condition     = aws_iam_instance_profile.node.role == aws_iam_role.node.name
    error_message = "The instance profile must carry the node role."
  }
}
