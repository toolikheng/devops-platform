# How the node is reached: its security group, and the one address allowed in. Only TCP 80 from the owner's
# /32 comes in; nothing on 22 (SSH) or 6443 (Kubernetes API), because shell, Ansible and kubectl go through
# Session Manager. Outbound is TCP 443 and 80 only.
#
# The rules are inline blocks, so the group's whole rule set is one attribute. That lets these tests check
# every rule, and Terraform removes any rule added outside it (for example in the console) on the next apply.
# Unset list attributes of a rule are null under the mock provider and [] in AWS; both mean "none", so the
# rule checks below read them with try(..., 0) / try(..., false).

mock_provider "aws" {
  source = "./tests/mocks"
}

variables {
  name                  = "test"
  instance_type         = "t3.micro"
  subnet_id             = "subnet-0123456789abcdef0"
  vpc_id                = "vpc-0123456789abcdef0"
  owner_cidr            = "203.0.113.10/32"
  instance_profile_name = "devops-platform-node"
  ssh_public_key        = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExampleExampleExampleExampleExampleExample test@example"
}

# --- owner_cidr: a single IPv4 address, never the whole internet -----------------------------------
# The apply runs further down use a valid /32, so they also prove a single address is accepted.

run "owner_cidr_rejects_the_whole_internet" {
  command = plan
  variables {
    owner_cidr = "0.0.0.0/0"
  }
  expect_failures = [var.owner_cidr]
}

run "owner_cidr_rejects_a_range" {
  command = plan
  variables {
    owner_cidr = "203.0.113.0/24"
  }
  expect_failures = [var.owner_cidr]
}

run "owner_cidr_rejects_an_address_without_a_prefix" {
  command = plan
  variables {
    owner_cidr = "203.0.113.10"
  }
  expect_failures = [var.owner_cidr]
}

run "owner_cidr_rejects_an_invalid_address" {
  command = plan
  variables {
    owner_cidr = "203.0.113.300/32"
  }
  expect_failures = [var.owner_cidr]
}

run "owner_cidr_rejects_ipv6" {
  command = plan
  variables {
    owner_cidr = "2001:db8::1/128"
  }
  expect_failures = [var.owner_cidr]
}

run "owner_cidr_rejects_the_unset_placeholder" {
  command = plan
  variables {
    owner_cidr = "unset"
  }
  expect_failures = [var.owner_cidr]
}

# --- Security group rules ---------------------------------------------------------------------------

run "only_the_owner_gets_in_and_only_on_port_80" {
  command = apply

  assert {
    condition     = aws_security_group.node.vpc_id == "vpc-0123456789abcdef0"
    error_message = "The security group must belong to the given VPC."
  }

  assert {
    condition = (
      length(aws_security_group.node.ingress) == 1 &&
      alltrue([
        for r in aws_security_group.node.ingress :
        r.protocol == "tcp" && r.from_port == 80 && r.to_port == 80 &&
        length(r.cidr_blocks) == 1 && r.cidr_blocks[0] == "203.0.113.10/32" &&
        try(length(r.ipv6_cidr_blocks), 0) == 0 && try(length(r.prefix_list_ids), 0) == 0 &&
        try(length(r.security_groups), 0) == 0 && r.self != true
      ])
    )
    error_message = "The only inbound rule must be TCP 80 from owner_cidr."
  }

  # Belt and braces: even if the rule above changes, these doors stay shut.
  assert {
    condition = alltrue([
      for r in aws_security_group.node.ingress :
      r.protocol != "-1" &&
      !(r.from_port <= 22 && r.to_port >= 22) &&
      !(r.from_port <= 6443 && r.to_port >= 6443) &&
      !contains(r.cidr_blocks, "0.0.0.0/0") && !try(contains(r.ipv6_cidr_blocks, "::/0"), false)
    ])
    error_message = "No inbound SSH (22), Kubernetes API (6443), all-protocol rule, or rule open to the internet."
  }
}

run "outbound_is_only_http_and_https" {
  command = apply

  assert {
    condition = (
      toset([
        for r in aws_security_group.node.egress :
        "${r.protocol} ${r.from_port}-${r.to_port} to ${join(",", r.cidr_blocks)}"
      ]) ==
      toset(["tcp 80-80 to 0.0.0.0/0", "tcp 443-443 to 0.0.0.0/0"])
    )
    error_message = "Outbound must be exactly TCP 443 (SSM, GHCR, GitHub, S3) and TCP 80 (apt mirrors)."
  }
}
