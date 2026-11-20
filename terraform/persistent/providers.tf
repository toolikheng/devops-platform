provider "aws" {
  region = var.region

  default_tags {
    tags = local.default_tags
  }
}

locals {
  default_tags = {
    Project   = "devops-platform"
    Owner     = "toolikheng"
    ManagedBy = "terraform"
    Stack     = "persistent"
  }
}

data "aws_caller_identity" "current" {}
