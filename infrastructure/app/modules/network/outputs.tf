output "vpc_id" {
  description = "ID of the voice-plane VPC, for the ALB and ECS service security groups and target group."
  value       = aws_vpc.this.id
}

output "vpc_cidr_block" {
  description = "CIDR block of the VPC, for security-group rules that scope to in-VPC traffic."
  value       = aws_vpc.this.cidr_block
}

output "public_subnet_ids" {
  description = "IDs of the public subnets (one per AZ) hosting the internet-facing ALB and the NAT gateway(s)."
  value       = aws_subnet.public[*].id
}

output "private_subnet_ids" {
  description = "IDs of the private subnets (one per AZ) hosting the ECS Fargate tasks (Req 10.1)."
  value       = aws_subnet.private[*].id
}

output "availability_zones" {
  description = "Names of the Availability Zones the subnets span."
  value       = local.azs
}

output "nat_gateway_ids" {
  description = "IDs of the NAT gateway(s) providing egress for the private subnets."
  value       = aws_nat_gateway.this[*].id
}
