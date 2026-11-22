# GitHub Actions reaches AWS through OpenID Connect, so no AWS access key is stored in GitHub. For each job,
# GitHub signs a short-lived token saying which repository it runs for, and which pull request, branch or
# GitHub Environment. STS checks the signature against the provider below and the token's claims against
# the role's trust policy, then returns credentials for one hour at most.
#
# One role per kind of job, each trusting exactly the runs that need it, through the token's "sub" claim.
# A job in a GitHub Environment gets ":environment:<name>" whatever its branch, so the environments' own
# rules keep pull requests away from the roles that change AWS: all three accept master only, and aws-apply
# also waits for the owner's approval. What each role may do is in github_permissions.tf.

locals {
  github_repository = "toolikheng/devops-platform"

  # Role name => the GitHub runs (token subjects, after "repo:<owner>/<repo>:") that may assume it.
  # tests/test_ci_roles.py checks every workflow job against this map.
  github_role_subjects = {
    "gha-tf-plan"    = ["pull_request", "ref:refs/heads/master"]
    "gha-tf-apply"   = ["environment:aws-apply"]
    "gha-tf-destroy" = ["environment:aws-nightly"]
    "gha-deploy"     = ["environment:aws-deploy"]
  }

  github_role_descriptions = {
    "gha-tf-plan"    = "GitHub Actions: read-only plan of the ephemeral stack (pull requests, and before a manual apply)."
    "gha-tf-apply"   = "GitHub Actions: apply or destroy the ephemeral stack after the owner approves (environment aws-apply)."
    "gha-tf-destroy" = "GitHub Actions: nightly destroy of the ephemeral stack, then the cost check (environment aws-nightly)."
    "gha-deploy"     = "GitHub Actions: deploy PolicyPal on the node through SSM Run Command (environment aws-deploy)."
  }
}

# GitHub's token issuer. The audience is what configure-aws-credentials asks for. No thumbprint: AWS checks
# GitHub's certificate against its own trusted certificate authorities.
resource "aws_iam_openid_connect_provider" "github" {
  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
}

resource "aws_iam_role" "github" {
  for_each = local.github_role_subjects

  name        = each.key
  description = local.github_role_descriptions[each.key]
  # One hour: longer than any job (the deploy waits up to 30 minutes), and the AWS default.
  max_session_duration = 3600

  # Exact matches only. A wildcard such as "repo:toolikheng/*" would trust every repository and branch.
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = aws_iam_openid_connect_provider.github.arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
          "token.actions.githubusercontent.com:sub" = [for subject in each.value : "repo:${local.github_repository}:${subject}"]
        }
      }
    }]
  })
}
