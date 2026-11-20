provider "aws" {
  # Fixed, not a variable: backend.tf (which can't use variables) stores state in this region.
  region = "ap-southeast-1"

  default_tags {
    tags = local.default_tags
  }
}

locals {
  # Stack=ephemeral marks what may be destroyed at the end of a session. The CI roles planned for Phase C6
  # are to be limited to resources with this tag. (cost-check does not read tags: it lists by resource type.)
  default_tags = {
    Project   = "devops-platform"
    Owner     = "toolikheng"
    ManagedBy = "terraform"
    Stack     = "ephemeral"
  }
}
