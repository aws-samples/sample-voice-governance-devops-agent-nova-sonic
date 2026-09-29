# Network module (Req 10.1, 15.3): the VPC hosting the voice plane. Public
# subnets carry the internet-facing ALB and the NAT gateway(s); private
# subnets carry the ECS Fargate tasks (no public IPs — egress to Bedrock,
# DevOps Agent, DynamoDB, and AppSync rides NAT). Subnets span at least two
# Availability Zones so the ECS service can keep its minimum of 2 tasks
# spread across ≥2 AZs (Req 10.1). AZ names come from a data source and
# every CIDR derives from input variables — nothing account-, region-, or
# environment-specific is hardcoded (Req 15.5).

terraform {
  required_version = ">= 1.9"

  # All app-layer modules pin the same AWS provider series. The floor is
  # v6.9.0 because the AppSync Events resources (aws_appsync_api /
  # aws_appsync_channel_namespace) used by the appsync_events module first
  # shipped in that release.
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 6.9.0, < 7.0.0"
    }
  }
}

data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  azs = slice(data.aws_availability_zones.available.names, 0, var.az_count)

  # Public subnets take the first az_count blocks of the VPC CIDR, private
  # subnets the next az_count, so layouts stay stable when az_count grows.
  public_cidrs  = [for i in range(var.az_count) : cidrsubnet(var.vpc_cidr, var.subnet_newbits, i)]
  private_cidrs = [for i in range(var.az_count) : cidrsubnet(var.vpc_cidr, var.subnet_newbits, var.az_count + i)]

  nat_gateway_count = var.single_nat_gateway ? 1 : var.az_count
}

# DNS support and hostnames are required for interface endpoints and for
# the ECS tasks to resolve AWS service endpoints through the VPC resolver.
resource "aws_vpc" "this" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = {
    Name = "${var.environment}-voice-vpc"
  }
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id

  tags = {
    Name = "${var.environment}-voice-igw"
  }
}

# Public subnets: ALB nodes and NAT gateways only. No instance launches
# happen here, so automatic public IP assignment stays off.
resource "aws_subnet" "public" {
  count = var.az_count

  vpc_id                  = aws_vpc.this.id
  cidr_block              = local.public_cidrs[count.index]
  availability_zone       = local.azs[count.index]
  map_public_ip_on_launch = false

  tags = {
    Name = "${var.environment}-voice-public-${local.azs[count.index]}"
    Tier = "public"
  }
}

# Private subnets: ECS Fargate tasks (Req 10.1 — ≥2 AZs). Tasks get no
# public IPs; outbound traffic leaves through the NAT gateway(s).
resource "aws_subnet" "private" {
  count = var.az_count

  vpc_id            = aws_vpc.this.id
  cidr_block        = local.private_cidrs[count.index]
  availability_zone = local.azs[count.index]

  tags = {
    Name = "${var.environment}-voice-private-${local.azs[count.index]}"
    Tier = "private"
  }
}

resource "aws_eip" "nat" {
  count = local.nat_gateway_count

  domain = "vpc"

  tags = {
    Name = "${var.environment}-voice-nat-${local.azs[count.index]}"
  }

  depends_on = [aws_internet_gateway.this]
}

# One NAT gateway per AZ by default so a single AZ loss never severs the
# Bedrock/DevOps-Agent egress of tasks in the surviving AZs (design: an AZ
# loss never drops a live voice call). single_nat_gateway collapses this to
# one gateway as a cost control for non-production environments.
resource "aws_nat_gateway" "this" {
  count = local.nat_gateway_count

  allocation_id = aws_eip.nat[count.index].id
  subnet_id     = aws_subnet.public[count.index].id

  tags = {
    Name = "${var.environment}-voice-nat-${local.azs[count.index]}"
  }

  depends_on = [aws_internet_gateway.this]
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id

  tags = {
    Name = "${var.environment}-voice-public"
  }
}

resource "aws_route" "public_internet" {
  route_table_id         = aws_route_table.public.id
  destination_cidr_block = "0.0.0.0/0"
  gateway_id             = aws_internet_gateway.this.id
}

resource "aws_route_table_association" "public" {
  count = var.az_count

  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

# One route table per private subnet: with per-AZ NAT each subnet routes to
# its own gateway; with single_nat_gateway all route to gateway 0.
resource "aws_route_table" "private" {
  count = var.az_count

  vpc_id = aws_vpc.this.id

  tags = {
    Name = "${var.environment}-voice-private-${local.azs[count.index]}"
  }
}

resource "aws_route" "private_nat" {
  count = var.az_count

  route_table_id         = aws_route_table.private[count.index].id
  destination_cidr_block = "0.0.0.0/0"
  nat_gateway_id         = aws_nat_gateway.this[var.single_nat_gateway ? 0 : count.index].id
}

resource "aws_route_table_association" "private" {
  count = var.az_count

  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private[count.index].id
}
