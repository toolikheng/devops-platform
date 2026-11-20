# One VPC with one public subnet, an internet gateway and a default route: the plumbing the node sits in.
# No NAT gateway, Elastic IP or VPC endpoints (they cost money; see ADR 010). The node's security group
# belongs to the compute module (ADR 011).

mock_provider "aws" {}

variables {
  name = "test"
  az   = "ap-southeast-1a"
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

run "outputs_for_the_node" {
  command = apply

  assert {
    condition     = output.vpc_id == aws_vpc.this.id && output.subnet_id == aws_subnet.public.id
    error_message = "The module must hand the node its VPC and subnet."
  }
}
