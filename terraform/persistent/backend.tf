# State lives in the bucket this stack created. It was bootstrapped with local state, then moved here
# with `terraform init -migrate-state`. use_lockfile writes a .tflock object next to the state, so two
# runs can't write it at once, without a DynamoDB table.
terraform {
  backend "s3" {
    bucket       = "toolikheng-devops-platform-tfstate"
    key          = "persistent/terraform.tfstate"
    region       = "ap-southeast-1"
    encrypt      = true
    use_lockfile = true
  }
}
