terraform {
  # Same bucket as the persistent stack, separate key, so each stack has its own state and lock.
  # Region and bucket are literals because a backend block cannot use variables.
  backend "s3" {
    bucket       = "toolikheng-devops-platform-tfstate"
    key          = "envs/aws/terraform.tfstate"
    region       = "ap-southeast-1"
    encrypt      = true
    use_lockfile = true
  }
}
