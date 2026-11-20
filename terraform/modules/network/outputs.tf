output "vpc_id" {
  description = "ID of the VPC."
  value       = aws_vpc.this.id
}

output "subnet_id" {
  description = "ID of the public subnet the node launches in."
  value       = aws_subnet.public.id
}

output "security_group_id" {
  description = "ID of the node's security group."
  value       = aws_security_group.node.id
}
