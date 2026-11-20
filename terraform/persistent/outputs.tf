output "state_bucket_name" {
  description = "S3 bucket that holds Terraform state for every stack."
  value       = aws_s3_bucket.state.bucket
}

output "deny_new_spend_policy_arn" {
  description = "Policy the budget action attaches at the monthly cap (used by the IAM policy simulator check)."
  value       = aws_iam_policy.deny_new_spend.arn
}

output "default_tags" {
  description = "Tags every resource in this stack gets."
  value       = local.default_tags
}
