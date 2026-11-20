output "instance_id" {
  description = "ID of the node: aws ssm start-session --target <id>."
  value       = module.compute.instance_id
}

output "public_ip" {
  description = "Public IPv4 of the node (changes every session)."
  value       = module.compute.public_ip
}

output "public_dns" {
  description = "Public DNS name of the node."
  value       = module.compute.public_dns
}

output "default_tags" {
  description = "Tags every resource in this stack gets."
  value       = local.default_tags
}
