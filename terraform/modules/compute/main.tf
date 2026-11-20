# The single k3s node: Ubuntu 24.04 amd64, IMDSv2 only, encrypted gp3 root volume, on-demand or Spot.

# Newest Ubuntu 24.04 LTS amd64 server image, from Canonical's own AWS account (no Marketplace AMIs).
data "aws_ami" "ubuntu" {
  most_recent = true
  owners      = ["099720109477"]

  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*"]
  }

  filter {
    name   = "architecture"
    values = ["x86_64"]
  }
}

# Facts about the chosen type: its CPU architecture, and whether it is burstable (T family).
data "aws_ec2_instance_type" "selected" {
  instance_type = var.instance_type
}

# Lets Ansible SSH in through a Session Manager tunnel. Only the public half is ever sent to AWS.
resource "aws_key_pair" "this" {
  key_name   = var.name
  public_key = trimspace(var.ssh_public_key)
}

resource "aws_instance" "node" {
  ami                    = data.aws_ami.ubuntu.id
  instance_type          = var.instance_type
  subnet_id              = var.subnet_id
  vpc_security_group_ids = var.security_group_ids
  iam_instance_profile   = var.instance_profile_name
  key_name               = aws_key_pair.this.key_name

  # The node's own public IPv4 (billed only while it runs) is its only way out: there is no NAT gateway.
  associate_public_ip_address = true
  # Detailed (1-minute) CloudWatch monitoring is a paid feature.
  monitoring = false
  # A shutdown from inside terminates the node, so a stopped instance never sits there paying for its disk.
  instance_initiated_shutdown_behavior = "terminate"

  # IMDSv2 only: credentials need a session token, which blocks the classic SSRF credential theft. Hop
  # limit 1: the token's reply cannot cross the extra network hop into a container, so ordinary pods
  # cannot get the node's credentials.
  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  root_block_device {
    volume_type           = "gp3"
    volume_size           = var.root_volume_gb
    encrypted             = true # AWS-managed key aws/ebs: no key fee.
    delete_on_termination = true
  }

  # T instances: "standard" credits, so a busy node slows down instead of billing surplus credits.
  # Other types have no credits, and AWS rejects the setting for them.
  dynamic "credit_specification" {
    for_each = data.aws_ec2_instance_type.selected.burstable_performance_supported ? [1] : []
    content {
      cpu_credits = "standard"
    }
  }

  # Spot: spare capacity at a discount that AWS can reclaim with two minutes' notice. One-time and
  # terminate, so nothing is left stopped or waiting to relaunch. No max price: the cap is the on-demand
  # price, so the node is never priced out mid-session.
  dynamic "instance_market_options" {
    for_each = var.use_spot ? [1] : []
    content {
      market_type = "spot"
      spot_options {
        spot_instance_type             = "one-time"
        instance_interruption_behavior = "terminate"
      }
    }
  }

  tags = { Name = var.name }

  lifecycle {
    # A newer Ubuntu AMI must never replace a running node mid-session; it is picked up on the next rebuild.
    ignore_changes = [ami]

    precondition {
      # The PolicyPal image is amd64 only. t4g (ARM) is on the allowlist, but could not run the app.
      condition     = contains(data.aws_ec2_instance_type.selected.supported_architectures, "x86_64")
      error_message = "instance_type must support x86_64: the PolicyPal image is amd64 only (t4g is ARM)."
    }
  }
}
