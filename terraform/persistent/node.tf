# What the ephemeral EC2 node needs from the persistent stack: an IAM identity and the owner's IP address.
# Both cost nothing at rest. They live here, not in the ephemeral stack, so creating or destroying the
# node never needs IAM write permission, and the CI apply role (C6) can be denied IAM writes entirely.

# Names the ephemeral stack looks these resources up by.
module "names" {
  source = "../modules/names"
}

# The node's role. Only the EC2 service can assume it; the node then gets short-lived credentials from
# the instance metadata service (IMDSv2), so no access keys exist anywhere.
resource "aws_iam_role" "node" {
  name        = module.names.node
  description = "Role of the ephemeral EC2 node: SSM agent only (C5 adds its parameters, C7 its backups)."
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

# Just what the SSM agent needs for Session Manager (shell, SSH and port forwarding) and Run Command.
# Not the managed policy AmazonSSMManagedInstanceCore: it also allows ssm:GetParameter(s) on every
# parameter, which would let anything on the node read every secret once C5 adds them.
resource "aws_iam_role_policy" "node_ssm_agent" {
  name = "ssm-agent"
  role = aws_iam_role.node.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # The agent registers itself and reports its status, for this account's instances only.
        Sid      = "RegisterTheInstance"
        Effect   = "Allow"
        Action   = ["ssm:UpdateInstanceInformation"]
        Resource = "arn:aws:ec2:${local.region}:${data.aws_caller_identity.current.account_id}:instance/*"
      },
      {
        # Session Manager channels, and the Run Command message queue. These actions have no resource
        # type in IAM, so "*" is the only possible resource.
        Sid    = "SessionManagerAndRunCommand"
        Effect = "Allow"
        Action = [
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
        ]
        Resource = "*"
      },
    ]
  })
}

# The wrapper EC2 needs to attach a role to an instance. The ephemeral stack looks it up by this name.
resource "aws_iam_instance_profile" "node" {
  name = module.names.node
  role = aws_iam_role.node.name
}

# The owner's public IP as a /32: the only address the node's security group lets in. `make owner-ip`
# writes the real value; Terraform creates the parameter with a placeholder and then ignores the value.
# The placeholder is deliberately not a CIDR, so the ephemeral plan fails until `make owner-ip` has run.
# String, not SecureString: an IP address is not a secret, and reading it then needs no KMS permission.
# Its value is redacted in plan output anyway, because Terraform treats SSM parameter values as sensitive.
resource "aws_ssm_parameter" "owner_cidr" {
  name        = module.names.owner_cidr_parameter
  description = "Owner's public IPv4 as a /32. Set with `make owner-ip`."
  type        = "String"
  tier        = "Standard"
  value       = "unset"

  lifecycle {
    ignore_changes = [value]
  }
}
