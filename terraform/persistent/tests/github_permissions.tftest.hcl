# What each GitHub role may do. Every list is exact, so a new permission is a deliberate change to a test.
#
# The EC2 actions are the calls the Terraform AWS provider made in real runs, taken from CloudTrail event
# history (2026-11-22, C5 Spot apply and destroy; 2026-11-20, C2 on-demand apply, which adds
# ModifyInstanceAttribute). Reads use ec2:Describe* (read-only, and the provider's describe calls vary
# between versions). RevokeSecurityGroupIngress is for the one in-place change the stack has: a new owner IP.
#
# - gha-tf-plan reads only, and only the ephemeral stack's state.
# - gha-tf-apply builds and removes the ephemeral stack: in ap-southeast-1 only, instance types on the
#   allowlist only, encrypted volumes of 30 GB or less, Canonical images only, and it may pass only the node
#   role, only to EC2. It deletes only what carries the stack's tags, and it can change nothing in IAM.
# - gha-tf-destroy can only read and delete (nightly), and run the cost check after.
# - gha-deploy can only send a shell command to the tagged node and read the result.

mock_provider "aws" {
  source = "./tests/mocks"
}

variables {
  alert_email = "alerts@example.com"
}

run "every_policy_is_attached_to_its_own_role" {
  command = apply

  assert {
    condition = (
      toset(keys(aws_iam_role_policy.github)) == toset(keys(aws_iam_role.github)) &&
      alltrue([for key, policy in aws_iam_role_policy.github : policy.role == aws_iam_role.github[key].id])
    )
    error_message = "Each GitHub role gets exactly one inline policy, its own."
  }
}

run "no_role_can_change_iam_or_use_wildcard_actions" {
  command = apply

  assert {
    condition = alltrue([
      for policy in aws_iam_role_policy.github : alltrue([
        for s in jsondecode(policy.policy).Statement :
        !can(s.NotAction) && !can(s.NotResource) &&
        alltrue([for a in flatten([s.Action]) : !strcontains(a, "*") || a == "ec2:Describe*"])
      ])
    ])
    error_message = "No NotAction, NotResource or wildcard action (ec2:Describe*, read-only, is the one exception)."
  }

  assert {
    condition = alltrue([
      for policy in aws_iam_role_policy.github : alltrue([
        for a in flatten([for s in jsondecode(policy.policy).Statement : s.Action]) :
        !startswith(lower(a), "iam:") || contains(["iam:GetInstanceProfile", "iam:PassRole"], a)
      ])
    ])
    error_message = "No GitHub role may change IAM: a compromised workflow could otherwise grant itself more."
  }

  # Deny statements only narrow RunInstances (allowlist, volume, image), never anything else.
  assert {
    condition = alltrue([
      for policy in aws_iam_role_policy.github : alltrue([
        for s in jsondecode(policy.policy).Statement :
        s.Effect == "Allow" || (s.Effect == "Deny" && flatten([s.Action]) == ["ec2:RunInstances"] && can(s.Condition))
      ])
    ])
    error_message = "Every statement is an Allow, except conditional Denys on ec2:RunInstances."
  }
}

run "only_the_apply_role_passes_a_role_and_only_the_node_role_to_ec2" {
  command = apply

  assert {
    condition = alltrue([
      for key, policy in aws_iam_role_policy.github :
      key == "gha-tf-apply" || !contains(flatten([for s in jsondecode(policy.policy).Statement : s.Action]), "iam:PassRole")
    ])
    error_message = "Only gha-tf-apply may pass a role."
  }

  assert {
    condition = alltrue([
      for s in jsondecode(aws_iam_role_policy.github["gha-tf-apply"].policy).Statement :
      !contains(flatten([s.Action]), "iam:PassRole") || (
        flatten([s.Action]) == ["iam:PassRole"] &&
        flatten([s.Resource]) == ["arn:aws:iam::111122223333:role/devops-platform-node"] &&
        s.Condition.StringEquals["iam:PassedToService"] == "ec2.amazonaws.com"
      )
    ])
    error_message = "iam:PassRole only for devops-platform-node, and only to EC2."
  }
}

run "plan_role_only_reads_the_ephemeral_stack" {
  command = apply

  assert {
    condition = toset(flatten([for s in jsondecode(aws_iam_role_policy.github["gha-tf-plan"].policy).Statement : s.Action])) == toset([
      "ec2:Describe*", "ec2:GetInstanceUefiData", "iam:GetInstanceProfile", "ssm:GetParameter", "s3:ListBucket", "s3:GetObject",
    ])
    error_message = "gha-tf-plan reads only: EC2 descriptions, the node's instance profile, the owner-cidr parameter and the state."
  }

  assert {
    condition = alltrue([
      for s in jsondecode(aws_iam_role_policy.github["gha-tf-plan"].policy).Statement : (
        (!contains(flatten([s.Action]), "s3:GetObject") || flatten([s.Resource]) == ["arn:aws:s3:::toolikheng-devops-platform-tfstate/envs/aws/terraform.tfstate"]) &&
        (!contains(flatten([s.Action]), "s3:ListBucket") || flatten([s.Resource]) == ["arn:aws:s3:::toolikheng-devops-platform-tfstate"]) &&
        (!contains(flatten([s.Action]), "ssm:GetParameter") || flatten([s.Resource]) == ["arn:aws:ssm:ap-southeast-1:111122223333:parameter/devops-platform/owner-cidr"]) &&
        (!contains(flatten([s.Action]), "iam:GetInstanceProfile") || flatten([s.Resource]) == ["arn:aws:iam::111122223333:instance-profile/devops-platform-node"])
      )
    ])
    error_message = "gha-tf-plan reads only the ephemeral state object (never persistent/), the owner-cidr parameter and the node's instance profile."
  }
}

run "apply_role_builds_and_removes_only_the_ephemeral_stack" {
  command = apply

  assert {
    condition = toset(flatten([for s in jsondecode(aws_iam_role_policy.github["gha-tf-apply"].policy).Statement : s.Action if s.Effect == "Allow"])) == toset([
      # reads, as gha-tf-plan
      "ec2:Describe*", "ec2:GetInstanceUefiData", "iam:GetInstanceProfile", "ssm:GetParameter", "s3:ListBucket", "s3:GetObject",
      # state and its lockfile
      "s3:PutObject", "s3:DeleteObject",
      # create and change (CloudTrail)
      "ec2:CreateVpc", "ec2:ModifyVpcAttribute", "ec2:CreateSubnet", "ec2:CreateInternetGateway", "ec2:AttachInternetGateway",
      "ec2:CreateRouteTable", "ec2:CreateRoute", "ec2:AssociateRouteTable", "ec2:CreateSecurityGroup",
      "ec2:AuthorizeSecurityGroupIngress", "ec2:AuthorizeSecurityGroupEgress", "ec2:RevokeSecurityGroupIngress",
      "ec2:RevokeSecurityGroupEgress", "ec2:ImportKeyPair", "ec2:RunInstances", "ec2:ModifyInstanceAttribute", "ec2:CreateTags",
      "iam:PassRole",
      # delete (CloudTrail)
      "ec2:TerminateInstances", "ec2:CancelSpotInstanceRequests", "ec2:DeleteKeyPair", "ec2:DeleteSecurityGroup", "ec2:DeleteRoute",
      "ec2:DisassociateRouteTable", "ec2:DeleteRouteTable", "ec2:DetachInternetGateway", "ec2:DeleteInternetGateway",
      "ec2:DeleteSubnet", "ec2:DeleteVpc",
    ])
    error_message = "gha-tf-apply may do exactly what building and removing the ephemeral stack needs."
  }

  # Every EC2 change happens in ap-southeast-1, in this account (the image is the only resource with no account).
  assert {
    condition = alltrue([
      for s in jsondecode(aws_iam_role_policy.github["gha-tf-apply"].policy).Statement :
      s.Effect == "Deny" || alltrue([for a in flatten([s.Action]) : !startswith(a, "ec2:") || a == "ec2:Describe*"]) ||
      alltrue([for r in flatten([s.Resource]) : startswith(r, "arn:aws:ec2:ap-southeast-1:111122223333:") || r == "arn:aws:ec2:ap-southeast-1::image/*"])
    ])
    error_message = "gha-tf-apply may change EC2 resources only in ap-southeast-1 and this account."
  }

  # Tags can be set only while creating something, so the role cannot tag an existing resource (such as the
  # default VPC) to make it deletable.
  assert {
    condition = alltrue([
      for s in jsondecode(aws_iam_role_policy.github["gha-tf-apply"].policy).Statement :
      !contains(flatten([s.Action]), "ec2:CreateTags") || (
        flatten([s.Action]) == ["ec2:CreateTags"] &&
        toset(flatten([s.Condition.StringEquals["ec2:CreateAction"]])) == toset([
          "CreateVpc", "CreateSubnet", "CreateInternetGateway", "CreateRouteTable", "CreateSecurityGroup", "ImportKeyPair", "RunInstances",
        ])
      )
    ])
    error_message = "ec2:CreateTags only as part of creating a resource (ec2:CreateAction)."
  }
}

run "apply_role_launches_only_what_the_cost_rules_allow" {
  command = apply

  # Deny statements: they hold whatever other resources RunInstances touches, and they repeat in IAM what
  # Terraform's own validation checks, so a change that skipped the validation still could not launch.
  assert {
    condition = anytrue([
      for s in jsondecode(aws_iam_role_policy.github["gha-tf-apply"].policy).Statement :
      s.Effect == "Deny" && flatten([s.Resource]) == ["arn:aws:ec2:ap-southeast-1:111122223333:instance/*"] &&
      toset(flatten([s.Condition.StringNotEquals["ec2:InstanceType"]])) == toset([
        "t3.micro", "t3.small", "t4g.micro", "t4g.small", "c7i-flex.large", "m7i-flex.large",
      ])
    ])
    error_message = "RunInstances must be denied for any instance type outside the allowlist."
  }

  assert {
    condition = anytrue([
      for s in jsondecode(aws_iam_role_policy.github["gha-tf-apply"].policy).Statement :
      s.Effect == "Deny" && flatten([s.Resource]) == ["arn:aws:ec2:ap-southeast-1:111122223333:volume/*"] &&
      try(tonumber(s.Condition.NumericGreaterThan["ec2:VolumeSize"]), null) == 30 # two Denys share volume/*
    ])
    error_message = "RunInstances must be denied for a volume larger than 30 GB."
  }

  # A separate statement: conditions in one statement must all match, so "larger than 30 GB and unencrypted"
  # would let an unencrypted 20 GB volume through.
  assert {
    condition = anytrue([
      for s in jsondecode(aws_iam_role_policy.github["gha-tf-apply"].policy).Statement :
      s.Effect == "Deny" && flatten([s.Resource]) == ["arn:aws:ec2:ap-southeast-1:111122223333:volume/*"] &&
      toset(keys(s.Condition)) == toset(["Bool"]) && tostring(s.Condition.Bool["ec2:Encrypted"]) == "false"
    ])
    error_message = "RunInstances must be denied for an unencrypted volume."
  }

  assert {
    condition = anytrue([
      for s in jsondecode(aws_iam_role_policy.github["gha-tf-apply"].policy).Statement :
      s.Effect == "Deny" && flatten([s.Resource]) == ["arn:aws:ec2:ap-southeast-1::image/*"] &&
      s.Condition.StringNotEquals["ec2:Owner"] == "099720109477"
    ])
    error_message = "RunInstances must be denied for any image not owned by Canonical (no Marketplace AMIs)."
  }
}

run "deletes_need_the_ephemeral_stacks_tags" {
  command = apply

  # Both roles that delete: only resources tagged as this project's ephemeral stack. The Spot request is the
  # exception: AWS creates it untagged, and cancelling one never costs anything.
  assert {
    condition = alltrue([
      for key in ["gha-tf-apply", "gha-tf-destroy"] : alltrue([
        for s in jsondecode(aws_iam_role_policy.github[key].policy).Statement :
        !anytrue([for a in flatten([s.Action]) : can(regex("^ec2:(Delete|Terminate|Detach|Disassociate)", a))]) || (
          s.Condition.StringEquals["ec2:ResourceTag/Project"] == "devops-platform" &&
          s.Condition.StringEquals["ec2:ResourceTag/Stack"] == "ephemeral" &&
          alltrue([for r in flatten([s.Resource]) : startswith(r, "arn:aws:ec2:ap-southeast-1:111122223333:")])
        )
      ])
    ])
    error_message = "Deletes are allowed only on resources tagged Project=devops-platform and Stack=ephemeral, in ap-southeast-1."
  }
}

run "state_access_is_the_ephemeral_state_and_its_lockfile_only" {
  command = apply

  assert {
    condition = alltrue([
      for key in ["gha-tf-apply", "gha-tf-destroy"] : alltrue([
        for s in jsondecode(aws_iam_role_policy.github[key].policy).Statement : (
          (!contains(flatten([s.Action]), "s3:PutObject") || toset(flatten([s.Resource])) == toset([
            "arn:aws:s3:::toolikheng-devops-platform-tfstate/envs/aws/terraform.tfstate",
            "arn:aws:s3:::toolikheng-devops-platform-tfstate/envs/aws/terraform.tfstate.tflock",
          ])) &&
          (!contains(flatten([s.Action]), "s3:DeleteObject") || flatten([s.Resource]) == ["arn:aws:s3:::toolikheng-devops-platform-tfstate/envs/aws/terraform.tfstate.tflock"]) &&
          alltrue([for r in flatten([s.Resource]) : !strcontains(r, "persistent/")])
        )
      ])
    ])
    error_message = "Apply and destroy write only envs/aws/terraform.tfstate and its lockfile, and delete only the lockfile."
  }
}

run "destroy_role_can_read_and_delete_but_never_create" {
  command = apply

  assert {
    condition = alltrue([
      for a in flatten([for s in jsondecode(aws_iam_role_policy.github["gha-tf-destroy"].policy).Statement : s.Action]) :
      a == "s3:PutObject" || !can(regex("^[a-z0-9-]+:(Create|Run|Request|Allocate|Import|Start|Authorize|Associate|Attach|Modify|Put|Pass|Update|Enable|Copy|Register|Replace)", a))
    ])
    error_message = "gha-tf-destroy must not be able to create or change anything (s3:PutObject is the state write)."
  }

  assert {
    condition = toset(flatten([for s in jsondecode(aws_iam_role_policy.github["gha-tf-destroy"].policy).Statement : s.Action])) == toset([
      # what the destroy plan reads, and the state
      "ec2:Describe*", "ec2:GetInstanceUefiData", "iam:GetInstanceProfile", "ssm:GetParameter", "s3:ListBucket", "s3:GetObject",
      "s3:PutObject", "s3:DeleteObject",
      # delete (CloudTrail, C5 destroy)
      "ec2:TerminateInstances", "ec2:CancelSpotInstanceRequests", "ec2:DeleteKeyPair", "ec2:DeleteSecurityGroup", "ec2:DeleteRoute",
      "ec2:DisassociateRouteTable", "ec2:DeleteRouteTable", "ec2:DetachInternetGateway", "ec2:DeleteInternetGateway",
      "ec2:DeleteSubnet", "ec2:DeleteVpc",
      # scripts/cost-check.sh after the destroy (tests/test_ci_roles.py checks it against the script)
      "elasticloadbalancing:DescribeLoadBalancers", "eks:ListClusters", "rds:DescribeDBInstances", "rds:DescribeDBClusters",
      "secretsmanager:ListSecrets", "route53:ListHostedZones", "ecr:DescribeRepositories", "lightsail:GetInstances",
      "lightsail:GetRelationalDatabases", "sagemaker:ListNotebookInstances", "sagemaker:ListEndpoints", "kms:ListKeys", "kms:DescribeKey",
    ])
    error_message = "gha-tf-destroy may do exactly what the nightly destroy and the cost check need."
  }
}

run "deploy_role_only_runs_a_shell_command_on_the_tagged_node" {
  command = apply

  assert {
    condition = toset(flatten([for s in jsondecode(aws_iam_role_policy.github["gha-deploy"].policy).Statement : s.Action])) == toset([
      "ec2:DescribeInstances", "ssm:SendCommand", "ssm:GetCommandInvocation",
    ])
    error_message = "gha-deploy may find the node, send it a command and read the result, nothing else."
  }

  # Two statements, as AWS's own example: the tag condition on the document's statement would never match.
  assert {
    condition = anytrue([
      for s in jsondecode(aws_iam_role_policy.github["gha-deploy"].policy).Statement :
      flatten([s.Action]) == ["ssm:SendCommand"] && !can(s.Condition) &&
      flatten([s.Resource]) == ["arn:aws:ssm:ap-southeast-1::document/AWS-RunShellScript"]
    ])
    error_message = "ssm:SendCommand may use the AWS-RunShellScript document only."
  }

  assert {
    condition = anytrue([
      for s in jsondecode(aws_iam_role_policy.github["gha-deploy"].policy).Statement :
      flatten([s.Action]) == ["ssm:SendCommand"] &&
      flatten([s.Resource]) == ["arn:aws:ec2:ap-southeast-1:111122223333:instance/*"] &&
      s.Condition.StringEquals["ssm:resourceTag/Project"] == "devops-platform" &&
      s.Condition.StringEquals["ssm:resourceTag/Stack"] == "ephemeral"
    ])
    error_message = "ssm:SendCommand may target only instances tagged Project=devops-platform and Stack=ephemeral."
  }

  assert {
    condition = alltrue([
      for s in jsondecode(aws_iam_role_policy.github["gha-deploy"].policy).Statement :
      !contains(flatten([s.Action]), "ssm:SendCommand") || alltrue([
        for r in flatten([s.Resource]) : r != "*" && !strcontains(r, "instance/*") || can(s.Condition.StringEquals["ssm:resourceTag/Stack"])
      ])
    ])
    error_message = "No statement may allow ssm:SendCommand on instances without the tag condition."
  }
}
