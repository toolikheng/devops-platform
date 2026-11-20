# One VPC with one public subnet, an internet gateway, and a security group that lets only the owner in,
# only on port 80. No NAT gateway, Elastic IP or VPC endpoints (they cost money; see ADR 010).
#
# The security group's rules are inline blocks, so the group's whole rule set is one attribute. That lets
# these tests check every rule, and Terraform removes any rule added outside it (for example in the
# console) on the next apply.
#
# Unset list attributes of a rule are null under the mock provider and [] in AWS; both mean "none", so
# the rule checks below read them with try(..., 0) / try(..., false).

mock_provider "aws" {}

variables {
  name       = "test"
  az         = "ap-southeast-1a"
  owner_cidr = "203.0.113.10/32"
}

# --- owner_cidr: a single IPv4 address, never the whole internet -----------------------------------

run "owner_cidr_accepts_a_single_ipv4_address" {
  command = plan
}

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

# --- Network layout ---------------------------------------------------------------------------------

run "vpc_with_dns_and_one_public_subnet" {
  command = apply

  assert {
    condition     = aws_vpc.this.cidr_block == "10.20.0.0/16"
    error_message = "Unexpected VPC range."
  }

  assert {
    condition     = aws_vpc.this.enable_dns_support && aws_vpc.this.enable_dns_hostnames
    error_message = "The VPC resolver must be on: the node resolves SSM, GHCR and apt mirror names."
  }

  assert {
    condition = (
      aws_subnet.public.vpc_id == aws_vpc.this.id &&
      aws_subnet.public.cidr_block == "10.20.1.0/24" &&
      aws_subnet.public.availability_zone == "ap-southeast-1a"
    )
    error_message = "The subnet must be 10.20.1.0/24 in the chosen AZ, inside the VPC."
  }

  assert {
    condition     = aws_subnet.public.map_public_ip_on_launch == false
    error_message = "The subnet must not hand out public IPs by default; the instance asks for one explicitly."
  }
}

run "subnet_routes_to_the_internet_through_the_gateway" {
  command = apply

  assert {
    condition     = aws_internet_gateway.this.vpc_id == aws_vpc.this.id
    error_message = "The internet gateway must be attached to the VPC."
  }

  assert {
    condition = (
      aws_route.internet.route_table_id == aws_route_table.public.id &&
      aws_route.internet.destination_cidr_block == "0.0.0.0/0" &&
      aws_route.internet.gateway_id == aws_internet_gateway.this.id
    )
    error_message = "The default route must go to the internet gateway (no NAT gateway)."
  }

  assert {
    condition = (
      aws_route_table_association.public.subnet_id == aws_subnet.public.id &&
      aws_route_table_association.public.route_table_id == aws_route_table.public.id
    )
    error_message = "The public subnet must use the public route table."
  }
}

# --- Security group ---------------------------------------------------------------------------------

run "only_the_owner_gets_in_and_only_on_port_80" {
  command = apply

  assert {
    condition     = aws_security_group.node.vpc_id == aws_vpc.this.id
    error_message = "The security group must belong to the VPC."
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
