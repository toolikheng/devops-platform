# What each GitHub role may do (who may use each role is in github_oidc.tf).
#
# The EC2 actions are the calls the Terraform AWS provider made in real runs of the ephemeral stack, taken
# from CloudTrail event history: a Spot apply and destroy (C5) and an on-demand apply (C2), which also calls
# ModifyInstanceAttribute. Reads use ec2:Describe*: read-only, and the provider's describe calls change
# between versions. RevokeSecurityGroupIngress is for the stack's one in-place change, a new owner IP.
#
# - gha-tf-plan: reads only; of the state, only the ephemeral stack's.
# - gha-tf-apply: builds and removes the ephemeral stack. Only in ap-southeast-1, only allowlisted instance
#   types, encrypted volumes of 30 GB or less and Canonical images; it passes only the node role, only to
#   EC2; it deletes only what carries the stack's tags; it can change nothing in IAM. The budget action
#   attaches the deny-new-spend policy to it at the monthly cap, as to the admin user.
# - gha-tf-destroy: reads and deletes only (nightly), then the cost check.
# - gha-deploy: sends one shell command to the tagged node and reads its result.
# tests/github_permissions.tftest.hcl checks every list exactly.

locals {
  account_id = data.aws_caller_identity.current.account_id
  # This account's EC2 resources in the project's region. Images have no account in their ARN.
  ec2_here        = "arn:aws:ec2:${local.region}:${local.account_id}"
  images_here     = "arn:aws:ec2:${local.region}::image/*"
  state_bucket    = "arn:aws:s3:::${aws_s3_bucket.state.bucket}"
  ephemeral_state = "${local.state_bucket}/envs/aws/terraform.tfstate"
  ephemeral_lock  = "${local.ephemeral_state}.tflock"

  # The cost rules' instance types: the same list as the validation in modules/compute (tests/test_ci_roles.py
  # keeps the two and terraform.yml's choices equal). IAM repeats the check for anything that skips Terraform's.
  instance_type_allowlist = ["t3.micro", "t3.small", "t4g.micro", "t4g.small", "c7i-flex.large", "m7i-flex.large"]

  # Everything scripts/cost-check.sh reads, in every region (tests/test_ci_roles.py checks it against the script).
  cost_check_reads = [
    "ec2:Describe*",
    "elasticloadbalancing:DescribeLoadBalancers",
    "eks:ListClusters",
    "rds:DescribeDBInstances",
    "rds:DescribeDBClusters",
    "secretsmanager:ListSecrets",
    "route53:ListHostedZones",
    "ecr:DescribeRepositories",
    "lightsail:GetInstances",
    "lightsail:GetRelationalDatabases",
    "sagemaker:ListNotebookInstances",
    "sagemaker:ListEndpoints",
    "kms:ListKeys",
    "kms:DescribeKey",
  ]

  # What any plan of the ephemeral stack reads: AWS's current view, the two persistent resources it looks up
  # by name, and its own state (never the persistent stack's).
  read_ephemeral_stack = [
    {
      # Describe calls take no resource ARN.
      Sid      = "DescribeEc2"
      Effect   = "Allow"
      Action   = ["ec2:Describe*"]
      Resource = "*"
    },
    {
      # Called by the provider when it reads the instance.
      Sid      = "ReadInstanceUefiData"
      Effect   = "Allow"
      Action   = ["ec2:GetInstanceUefiData"]
      Resource = "${local.ec2_here}:instance/*"
    },
    {
      Sid      = "ReadTheNodesInstanceProfile"
      Effect   = "Allow"
      Action   = ["iam:GetInstanceProfile"]
      Resource = "arn:aws:iam::${local.account_id}:instance-profile/${module.names.node}"
    },
    {
      Sid      = "ReadTheOwnerCidr"
      Effect   = "Allow"
      Action   = ["ssm:GetParameter"]
      Resource = "arn:aws:ssm:${local.region}:${local.account_id}:parameter${module.names.owner_cidr_parameter}"
    },
    {
      Sid      = "ListTheStateBucket"
      Effect   = "Allow"
      Action   = ["s3:ListBucket"]
      Resource = local.state_bucket
    },
    {
      Sid      = "ReadTheEphemeralState"
      Effect   = "Allow"
      Action   = ["s3:GetObject"]
      Resource = local.ephemeral_state
    },
  ]

  # Writing the state and taking the lock (a lockfile next to the state: use_lockfile).
  write_ephemeral_state = [
    {
      Sid      = "WriteTheEphemeralStateAndLock"
      Effect   = "Allow"
      Action   = ["s3:GetObject", "s3:PutObject"]
      Resource = [local.ephemeral_state, local.ephemeral_lock]
    },
    {
      Sid      = "ReleaseTheLock"
      Effect   = "Allow"
      Action   = ["s3:DeleteObject"]
      Resource = local.ephemeral_lock
    },
  ]

  # Deleting only what the ephemeral stack created: its default tags are on every resource it makes.
  delete_ephemeral_stack = [
    {
      Sid    = "DeleteOnlyTheEphemeralStack"
      Effect = "Allow"
      Action = [
        "ec2:TerminateInstances",
        "ec2:DeleteKeyPair",
        "ec2:DeleteSecurityGroup",
        "ec2:DeleteRoute",
        "ec2:DisassociateRouteTable",
        "ec2:DeleteRouteTable",
        "ec2:DetachInternetGateway",
        "ec2:DeleteInternetGateway",
        "ec2:DeleteSubnet",
        "ec2:DeleteVpc",
      ]
      Resource = "${local.ec2_here}:*"
      Condition = {
        StringEquals = {
          "ec2:ResourceTag/Project" = local.default_tags.Project
          "ec2:ResourceTag/Stack"   = "ephemeral"
        }
      }
    },
    {
      # AWS creates the Spot request itself, untagged; cancelling one never costs anything.
      Sid      = "CancelTheSpotRequest"
      Effect   = "Allow"
      Action   = ["ec2:CancelSpotInstanceRequests"]
      Resource = "${local.ec2_here}:spot-instances-request/*"
    },
  ]

  github_role_statements = {
    "gha-tf-plan" = local.read_ephemeral_stack

    "gha-tf-apply" = concat(local.read_ephemeral_stack, local.write_ephemeral_state, [
      {
        Sid    = "BuildTheEphemeralStack"
        Effect = "Allow"
        Action = [
          "ec2:CreateVpc",
          "ec2:ModifyVpcAttribute",
          "ec2:CreateSubnet",
          "ec2:CreateInternetGateway",
          "ec2:AttachInternetGateway",
          "ec2:CreateRouteTable",
          "ec2:CreateRoute",
          "ec2:AssociateRouteTable",
          "ec2:CreateSecurityGroup",
          "ec2:AuthorizeSecurityGroupIngress",
          "ec2:AuthorizeSecurityGroupEgress",
          "ec2:RevokeSecurityGroupIngress",
          "ec2:RevokeSecurityGroupEgress",
          "ec2:ImportKeyPair",
          "ec2:ModifyInstanceAttribute",
        ]
        Resource = "${local.ec2_here}:*"
      },
      {
        # Launch here; the Deny statements below narrow what may be launched.
        Sid      = "LaunchTheNode"
        Effect   = "Allow"
        Action   = ["ec2:RunInstances"]
        Resource = ["${local.ec2_here}:*", local.images_here]
      },
      {
        Sid       = "NoInstanceTypeOffTheAllowlist"
        Effect    = "Deny"
        Action    = ["ec2:RunInstances"]
        Resource  = "${local.ec2_here}:instance/*"
        Condition = { StringNotEquals = { "ec2:InstanceType" = local.instance_type_allowlist } }
      },
      {
        Sid       = "NoVolumeOver30Gb"
        Effect    = "Deny"
        Action    = ["ec2:RunInstances"]
        Resource  = "${local.ec2_here}:volume/*"
        Condition = { NumericGreaterThan = { "ec2:VolumeSize" = 30 } }
      },
      {
        # Its own statement: conditions in one statement must all match.
        Sid       = "NoUnencryptedVolume"
        Effect    = "Deny"
        Action    = ["ec2:RunInstances"]
        Resource  = "${local.ec2_here}:volume/*"
        Condition = { Bool = { "ec2:Encrypted" = "false" } }
      },
      {
        # Canonical's account: no Marketplace or community images.
        Sid       = "OnlyCanonicalImages"
        Effect    = "Deny"
        Action    = ["ec2:RunInstances"]
        Resource  = local.images_here
        Condition = { StringNotEquals = { "ec2:Owner" = "099720109477" } }
      },
      {
        # Tags only while creating, so an existing resource (the default VPC, say) cannot be tagged into
        # something the delete statement allows.
        Sid      = "TagOnlyWhileCreating"
        Effect   = "Allow"
        Action   = ["ec2:CreateTags"]
        Resource = "${local.ec2_here}:*"
        Condition = {
          StringEquals = {
            "ec2:CreateAction" = [
              "CreateVpc", "CreateSubnet", "CreateInternetGateway", "CreateRouteTable", "CreateSecurityGroup",
              "ImportKeyPair", "RunInstances",
            ]
          }
        }
      },
      {
        Sid       = "PassOnlyTheNodeRoleToEc2"
        Effect    = "Allow"
        Action    = ["iam:PassRole"]
        Resource  = "arn:aws:iam::${local.account_id}:role/${aws_iam_role.node.name}"
        Condition = { StringEquals = { "iam:PassedToService" = "ec2.amazonaws.com" } }
      },
    ], local.delete_ephemeral_stack)

    "gha-tf-destroy" = concat(local.read_ephemeral_stack, local.write_ephemeral_state, local.delete_ephemeral_stack, [
      {
        # The cost check after the destroy scans every enabled region.
        Sid      = "RunTheCostCheck"
        Effect   = "Allow"
        Action   = local.cost_check_reads
        Resource = "*"
      },
    ])

    "gha-deploy" = [
      {
        # Finds the node by its tags. Describe calls take no resource ARN.
        Sid      = "FindTheNode"
        Effect   = "Allow"
        Action   = ["ec2:DescribeInstances"]
        Resource = "*"
      },
      {
        # Two statements, as in AWS's own example: the document has no tags, so a tag condition here
        # would never match.
        Sid      = "OnlyTheShellScriptDocument"
        Effect   = "Allow"
        Action   = ["ssm:SendCommand"]
        Resource = "arn:aws:ssm:${local.region}::document/AWS-RunShellScript"
      },
      {
        Sid      = "OnlyTheEphemeralNode"
        Effect   = "Allow"
        Action   = ["ssm:SendCommand"]
        Resource = "${local.ec2_here}:instance/*"
        Condition = {
          StringEquals = {
            "ssm:resourceTag/Project" = local.default_tags.Project
            "ssm:resourceTag/Stack"   = "ephemeral"
          }
        }
      },
      {
        # Command invocations take no resource ARN.
        Sid      = "ReadTheResult"
        Effect   = "Allow"
        Action   = ["ssm:GetCommandInvocation"]
        Resource = "*"
      },
    ]
  }
}

resource "aws_iam_role_policy" "github" {
  for_each = local.github_role_statements

  name = "${each.key}-permissions"
  role = aws_iam_role.github[each.key].id
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = each.value
  })
}
