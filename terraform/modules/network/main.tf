# One VPC with one public subnet in one AZ. No NAT gateway, Elastic IP or VPC endpoints: the node uses its
# own public IPv4 through the internet gateway for outbound traffic (ADR 010).

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

# The node's firewall. Inline rules on purpose: Terraform then owns the group's whole rule set and removes
# any rule added outside it, and the tests can check every rule at once.
# No SSH (22) or Kubernetes API (6443) from anywhere: shell, Ansible and kubectl go through Session Manager.
resource "aws_security_group" "node" {
  name        = "${var.name}-node"
  description = "devops-platform node: HTTP from the owner only"
  vpc_id      = aws_vpc.this.id

  ingress {
    description = "PolicyPal UI through Traefik, owner only"
    protocol    = "tcp"
    from_port   = 80
    to_port     = 80
    cidr_blocks = [var.owner_cidr]
  }

  # Egress has to reach public endpoints (SSM, GHCR, GitHub, S3, apt mirrors) whose addresses change, and
  # the cost rules exclude the NAT gateway, proxy and VPC endpoints that could narrow it. So it is limited
  # by port instead: 443 and 80 only. Hence the two accepted trivy findings (AWS-0104) below.
  egress {
    description = "HTTPS: SSM, GHCR, GitHub, S3, k3s and Helm downloads"
    protocol    = "tcp"
    from_port   = 443
    to_port     = 443
    #trivy:ignore:AWS-0104
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    description = "HTTP: Ubuntu apt mirrors"
    protocol    = "tcp"
    from_port   = 80
    to_port     = 80
    #trivy:ignore:AWS-0104
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${var.name}-node" }
}
