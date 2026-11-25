# Backups of PolicyPal (the Postgres dump and the uploads), written by restic on the node with the node's
# role. restic encrypts and deduplicates, and decides what to delete (restic forget --prune), so S3 only
# cleans up after it: noncurrent versions (files restic deleted or replaced) go after 7 days, unfinished
# uploads after 1 day. Nothing expires current objects: that would delete data files later snapshots still
# use and corrupt the repository. Versioning keeps anything deleted recoverable for those 7 days.
# The bucket costs nothing when empty; a few MB of backups cost well under USD 0.01 a month.

locals {
  # S3 ARNs carry no account or region, so they are built from the name (also what the tests compare).
  backups_bucket_arn = "arn:aws:s3:::${module.names.backups_bucket}"
}

# Access logging would need a second bucket and log storage for a bucket only this project touches.
#trivy:ignore:AWS-0089
resource "aws_s3_bucket" "backups" {
  bucket = module.names.backups_bucket
  # Never delete the bucket with backups in it: emptying it is a deliberate step.
  force_destroy = false
}

resource "aws_s3_bucket_versioning" "backups" {
  bucket = aws_s3_bucket.backups.id

  versioning_configuration {
    status = "Enabled"
  }
}

# SSE-S3 (AES256): encryption at rest with no key to manage. restic encrypts the data as well, with the
# repository password in SSM Parameter Store (secret-catalog.yaml). A customer-managed KMS key costs USD 1/month.
#trivy:ignore:AWS-0132
resource "aws_s3_bucket_server_side_encryption_configuration" "backups" {
  bucket = aws_s3_bucket.backups.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "backups" {
  bucket = aws_s3_bucket.backups.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_policy" "backups" {
  bucket = aws_s3_bucket.backups.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyRequestsWithoutTLS"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource  = [local.backups_bucket_arn, "${local.backups_bucket_arn}/*"]
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })

  # Applying a bucket policy before the public access block is in place can be rejected.
  depends_on = [aws_s3_bucket_public_access_block.backups]
}

resource "aws_s3_bucket_lifecycle_configuration" "backups" {
  bucket = aws_s3_bucket.backups.id

  rule {
    id     = "clean-up-after-restic"
    status = "Enabled"

    filter {}

    noncurrent_version_expiration {
      noncurrent_days = 7
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }

  depends_on = [aws_s3_bucket_versioning.backups]
}

# The node's backup job: list the restic prefix, and read, write and delete objects under it. Nothing else in
# the bucket, and nothing in any other bucket (the Terraform state bucket included).
resource "aws_iam_role_policy" "node_backups" {
  name = "backups"
  role = aws_iam_role.node.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "ListTheResticPrefix"
        Effect    = "Allow"
        Action    = ["s3:ListBucket"]
        Resource  = local.backups_bucket_arn
        Condition = { StringLike = { "s3:prefix" = ["restic", "restic/*"] } }
      },
      {
        Sid      = "ReadWriteResticObjects"
        Effect   = "Allow"
        Action   = ["s3:DeleteObject", "s3:GetObject", "s3:PutObject"]
        Resource = "${local.backups_bucket_arn}/restic/*"
      },
    ]
  })
}
