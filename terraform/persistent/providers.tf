provider "aws" {
  # Fixed, not a variable: backend.tf (which can't use variables) stores state in this region.
  region = "ap-southeast-1"

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
