# Terraform state for every stack in this project. State records which real resource each block in the
# code is, and it can contain sensitive attributes, so the bucket is private, encrypted, TLS-only and
# versioned (a bad write can be rolled back). Locking uses a lockfile next to the state (use_lockfile),
# so no DynamoDB table is needed.

# Access logging would need a second bucket and log storage for a bucket only this project touches.
#trivy:ignore:AWS-0089
resource "aws_s3_bucket" "state" {
  # Fixed, not a variable: backend.tf (which can't use variables) points at this bucket.
  bucket = "toolikheng-devops-platform-tfstate"

  # The bucket holds Terraform's own state: Terraform must never delete it.
  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id

  versioning_configuration {
    status = "Enabled"
  }
}

# SSE-S3 (AES256): encryption at rest with no key to manage. A customer-managed KMS key costs
# USD 1/month plus requests and is not allowed by the project's cost rules.
#trivy:ignore:AWS-0132
resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
  bucket = aws_s3_bucket.state.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "state" {
  bucket = aws_s3_bucket.state.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_policy" "state" {
  bucket = aws_s3_bucket.state.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyRequestsWithoutTLS"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource  = [aws_s3_bucket.state.arn, "${aws_s3_bucket.state.arn}/*"]
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })

  # Applying a bucket policy before the public access block is in place can be rejected.
  depends_on = [aws_s3_bucket_public_access_block.state]
}

# Every apply writes a new state version. Keep the newest 10 old versions, delete the rest after 90 days.
resource "aws_s3_bucket_lifecycle_configuration" "state" {
  bucket = aws_s3_bucket.state.id

  rule {
    id     = "expire-old-state-versions"
    status = "Enabled"

    filter {}

    noncurrent_version_expiration {
      noncurrent_days           = 90
      newer_noncurrent_versions = 10
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }

  depends_on = [aws_s3_bucket_versioning.state]
}
