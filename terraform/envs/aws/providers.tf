provider "aws" {
  # Fixed, not a variable: backend.tf (which can't use variables) stores state in this region.
  region = "ap-southeast-1"

  default_tags {
    tags = local.default_tags
  }
}

locals {
  # Stack=ephemeral marks what may be destroyed at the end of a session: the GitHub roles may delete only
  # resources with this tag and Project, and the deploy finds the node by both (tests/test_ci_roles.py keeps
  # the copies equal). (cost-check does not read tags: it lists by resource type.)
  default_tags = {
    Project   = "devops-platform"
    Owner     = "toolikheng"
    ManagedBy = "terraform"
    Stack     = "ephemeral"
  }
}
