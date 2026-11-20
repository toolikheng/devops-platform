output "vpc_id" {
  description = "ID of the VPC (the node's security group lives in it)."
  value       = aws_vpc.this.id
}

output "subnet_id" {
  description = "ID of the public subnet the node launches in."
  value       = aws_subnet.public.id
}
