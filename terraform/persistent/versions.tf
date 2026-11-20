terraform {
  # 1.11+: S3 native state locking (use_lockfile) and write-only arguments.
  required_version = ">= 1.11, < 2.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.73"
    }
  }
}
