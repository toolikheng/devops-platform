# The S3 bucket that holds Terraform state: private, versioned, encrypted, TLS-only, old versions expire.
# command = apply runs against the mock provider (no AWS calls), so computed values such as ARNs are known.

mock_provider "aws" {
  source = "./tests/mocks"
}

variables {
  alert_email = "alerts@example.com"
}

run "state_bucket" {
  command = apply

  assert {
    condition     = aws_s3_bucket.state.bucket == "toolikheng-devops-platform-tfstate"
    error_message = "Unexpected state bucket name."
  }

  assert {
    condition     = aws_s3_bucket_versioning.state.versioning_configuration[0].status == "Enabled"
    error_message = "Versioning must be enabled so a bad state write can be rolled back."
  }

  assert {
    condition     = one(one(aws_s3_bucket_server_side_encryption_configuration.state.rule).apply_server_side_encryption_by_default).sse_algorithm == "AES256"
    error_message = "State must be encrypted with SSE-S3 (no customer-managed KMS key)."
  }

  assert {
    condition = (
      aws_s3_bucket_public_access_block.state.block_public_acls &&
      aws_s3_bucket_public_access_block.state.block_public_policy &&
      aws_s3_bucket_public_access_block.state.ignore_public_acls &&
      aws_s3_bucket_public_access_block.state.restrict_public_buckets
    )
    error_message = "All four public access block settings must be on."
  }

  assert {
    condition = anytrue([
      for s in jsondecode(aws_s3_bucket_policy.state.policy).Statement :
      s.Effect == "Deny" && s.Principal == "*" && s.Action == "s3:*" &&
      try(s.Condition.Bool["aws:SecureTransport"], "") == "false" &&
      contains(s.Resource, aws_s3_bucket.state.arn) &&
      contains(s.Resource, "${aws_s3_bucket.state.arn}/*")
    ])
    error_message = "The bucket policy must deny every request that doesn't use TLS, on the bucket and its objects."
  }

  assert {
    condition = (
      one(aws_s3_bucket_lifecycle_configuration.state.rule).status == "Enabled" &&
      one(aws_s3_bucket_lifecycle_configuration.state.rule).noncurrent_version_expiration[0].noncurrent_days == 90 &&
      tonumber(one(aws_s3_bucket_lifecycle_configuration.state.rule).noncurrent_version_expiration[0].newer_noncurrent_versions) == 10 &&
      one(aws_s3_bucket_lifecycle_configuration.state.rule).abort_incomplete_multipart_upload[0].days_after_initiation == 7
    )
    error_message = "Old state versions must expire after 90 days (keeping the newest 10) and incomplete uploads after 7 days."
  }
}
