output "instance_id" {
  description = "ID of the node (target for Session Manager and Run Command)."
  value       = aws_instance.node.id
}

output "public_ip" {
  description = "Public IPv4 of the node. It changes every time the node is created."
  value       = aws_instance.node.public_ip
}

output "public_dns" {
  description = "Public DNS name of the node."
  value       = aws_instance.node.public_dns
}
