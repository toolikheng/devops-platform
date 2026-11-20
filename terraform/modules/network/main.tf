# One VPC with one public subnet in one AZ: the plumbing the node sits in. No NAT gateway, Elastic IP or VPC
# endpoints: the node uses its own public IPv4 through the internet gateway for outbound traffic (ADR 010).
# The node's security group belongs to the compute module (ADR 011).

# No VPC Flow Logs: they need CloudWatch log ingestion (or an S3 log bucket), which the cost rules exclude.
# The only inbound path is one port from one address, and the stack lives for a few hours.
#trivy:ignore:AWS-0178
resource "aws_vpc" "this" {
  cidr_block = var.vpc_cidr
  # The Amazon-provided DNS resolver: the node resolves SSM, GHCR and apt mirror names through it.
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = { Name = "${var.name}-vpc" }
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id
  tags   = { Name = "${var.name}-igw" }
}

resource "aws_subnet" "public" {
  vpc_id            = aws_vpc.this.id
  cidr_block        = var.subnet_cidr
  availability_zone = var.az
  # Off: the instance asks for a public IP itself, so nothing else launched here gets one by accident.
  map_public_ip_on_launch = false

  tags = { Name = "${var.name}-public" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id
  tags   = { Name = "${var.name}-public" }
}

# Everything outside the VPC goes through the internet gateway. This route makes the subnet "public".
resource "aws_route" "internet" {
  route_table_id         = aws_route_table.public.id
  destination_cidr_block = "0.0.0.0/0"
  gateway_id             = aws_internet_gateway.this.id
}

resource "aws_route_table_association" "public" {
  subnet_id      = aws_subnet.public.id
  route_table_id = aws_route_table.public.id
}
