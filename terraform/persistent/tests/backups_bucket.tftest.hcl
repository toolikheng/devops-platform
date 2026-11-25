# The S3 bucket for restic's backups of PolicyPal (the Postgres dump and the uploads), written by the node
# itself. restic decides what to delete (forget --prune); S3 only cleans up after it. Expiring current objects
# would delete data files that later snapshots still use and corrupt the repository, so no rule may do that.
# command = apply runs against the mock provider (no AWS calls).

mock_provider "aws" {
  source = "./tests/mocks"
}

variables {
  alert_email = "alerts@example.com"
}

run "backups_bucket_is_private_encrypted_versioned_and_tls_only" {
  command = apply

  assert {
    condition     = aws_s3_bucket.backups.bucket == "toolikheng-devops-platform-backups"
    error_message = "The backups bucket is named in terraform/modules/names (the node's backup job uses the same name)."
  }

  assert {
    condition     = aws_s3_bucket.backups.force_destroy == false
    error_message = "Terraform must never delete the bucket with backups in it (no force_destroy)."
  }

  assert {
    condition     = aws_s3_bucket_versioning.backups.versioning_configuration[0].status == "Enabled"
    error_message = "Versioning keeps a deleted or overwritten backup file recoverable for a while."
  }

  assert {
    condition     = one(one(aws_s3_bucket_server_side_encryption_configuration.backups.rule).apply_server_side_encryption_by_default).sse_algorithm == "AES256"
    error_message = "SSE-S3 (AES256): encrypted at rest with no key to pay for (no customer-managed KMS key)."
  }

  assert {
    condition = (
      aws_s3_bucket_public_access_block.backups.block_public_acls &&
      aws_s3_bucket_public_access_block.backups.block_public_policy &&
      aws_s3_bucket_public_access_block.backups.ignore_public_acls &&
      aws_s3_bucket_public_access_block.backups.restrict_public_buckets
    )
    error_message = "All four public access block settings must be on."
  }

  assert {
    condition = jsondecode(aws_s3_bucket_policy.backups.policy).Statement == [{
      Sid       = "DenyRequestsWithoutTLS"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource  = ["arn:aws:s3:::toolikheng-devops-platform-backups", "arn:aws:s3:::toolikheng-devops-platform-backups/*"]
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
    error_message = "The bucket policy must only deny requests without TLS."
  }
}

run "only_restic_deletes_backups" {
  command = apply

  assert {
    condition     = length(aws_s3_bucket_lifecycle_configuration.backups.rule) == 1
    error_message = "One lifecycle rule."
  }

  assert {
    condition = alltrue([
      for r in aws_s3_bucket_lifecycle_configuration.backups.rule : r.status == "Enabled" && length(r.expiration) == 0
    ])
    error_message = "No expiration of current objects: restic's retention decides what is deleted."
  }

  assert {
    condition = alltrue([
      for r in aws_s3_bucket_lifecycle_configuration.backups.rule :
      r.noncurrent_version_expiration[0].noncurrent_days == 7 &&
      r.abort_incomplete_multipart_upload[0].days_after_initiation == 1
    ])
    error_message = "Old versions (files restic already deleted or replaced) go after 7 days; unfinished uploads after 1 day."
  }
}

run "node_reads_and_writes_only_the_restic_prefix" {
  command = apply

  assert {
    condition     = aws_iam_role_policy.node_backups.role == aws_iam_role.node.id
    error_message = "The backups policy must be attached to the node role."
  }

  assert {
    condition = jsondecode(aws_iam_role_policy.node_backups.policy).Statement == [
      {
        Sid       = "ListTheResticPrefix"
        Effect    = "Allow"
        Action    = ["s3:ListBucket"]
        Resource  = "arn:aws:s3:::toolikheng-devops-platform-backups"
        Condition = { StringLike = { "s3:prefix" = ["restic", "restic/*"] } }
      },
      {
        Sid      = "ReadWriteResticObjects"
        Effect   = "Allow"
        Action   = ["s3:DeleteObject", "s3:GetObject", "s3:PutObject"]
        Resource = "arn:aws:s3:::toolikheng-devops-platform-backups/restic/*"
      },
    ]
    error_message = "The node may list and read, write and delete objects under restic/ in the backups bucket, nothing else (not the state bucket)."
  }
}
