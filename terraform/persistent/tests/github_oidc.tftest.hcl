# GitHub Actions reaches AWS through OpenID Connect: GitHub signs a short-lived token for one job, and STS
# swaps it for about an hour of credentials of one role. No AWS access key exists anywhere.
#
# One role per kind of job. A role's trust policy names exactly the GitHub runs that may use it, through the
# token's "sub" claim: "repo:<owner>/<repo>:environment:<name>" for a job that uses a GitHub Environment,
# "repo:<owner>/<repo>:pull_request" for a pull request, "repo:<owner>/<repo>:ref:refs/heads/master" for any
# other run on master. Both claims are matched with StringEquals: a wildcard such as "repo:toolikheng/*"
# would trust every repository and branch of the account. The workflows' side is checked by
# tests/test_ci_roles.py (each job's subject is one its role trusts).

mock_provider "aws" {
  source = "./tests/mocks"
}

variables {
  alert_email = "alerts@example.com"
}

run "github_is_the_identity_provider_and_sts_the_only_audience" {
  command = apply

  assert {
    condition     = aws_iam_openid_connect_provider.github.url == "https://token.actions.githubusercontent.com"
    error_message = "The OIDC provider must be GitHub Actions' token issuer."
  }

  assert {
    condition     = toset(aws_iam_openid_connect_provider.github.client_id_list) == toset(["sts.amazonaws.com"])
    error_message = "The only audience must be sts.amazonaws.com, the one configure-aws-credentials asks for."
  }
}

run "there_is_one_role_per_kind_of_job" {
  command = apply

  assert {
    condition     = toset(keys(aws_iam_role.github)) == toset(["gha-tf-plan", "gha-tf-apply", "gha-tf-destroy", "gha-deploy"])
    error_message = "Exactly four GitHub roles: plan, apply, nightly destroy and deploy."
  }

  assert {
    condition     = alltrue([for key, role in aws_iam_role.github : role.name == key])
    error_message = "Each role is named by its key: the workflows build its ARN from that name."
  }
}

run "every_role_trusts_only_github_tokens_meant_for_sts" {
  command = apply

  assert {
    condition = alltrue([
      for role in aws_iam_role.github : length(jsondecode(role.assume_role_policy).Statement) == 1 && alltrue([
        for s in jsondecode(role.assume_role_policy).Statement :
        s.Effect == "Allow" && s.Action == "sts:AssumeRoleWithWebIdentity" &&
        s.Principal == { Federated = "arn:aws:iam::111122223333:oidc-provider/token.actions.githubusercontent.com" }
      ])
    ])
    error_message = "Each role has one statement: GitHub's OIDC provider may assume it with a web identity token, nothing else."
  }

  # Only exact matching: no StringLike, ForAnyValue or other operator that could widen the match.
  assert {
    condition = alltrue([
      for role in aws_iam_role.github : alltrue([
        for s in jsondecode(role.assume_role_policy).Statement :
        toset(keys(s.Condition)) == toset(["StringEquals"]) &&
        toset(keys(s.Condition.StringEquals)) == toset(["token.actions.githubusercontent.com:aud", "token.actions.githubusercontent.com:sub"]) &&
        s.Condition.StringEquals["token.actions.githubusercontent.com:aud"] == "sts.amazonaws.com"
      ])
    ])
    error_message = "Each trust policy must match aud (sts.amazonaws.com) and sub with StringEquals only."
  }

  assert {
    condition = alltrue([
      for role in aws_iam_role.github : alltrue([
        for sub in flatten([for s in jsondecode(role.assume_role_policy).Statement : s.Condition.StringEquals["token.actions.githubusercontent.com:sub"]]) :
        !strcontains(sub, "*") && !strcontains(sub, "?")
      ])
    ])
    error_message = "No subject may contain a wildcard."
  }
}

run "each_role_trusts_exactly_its_jobs" {
  command = apply

  # Read-only plans: pull requests (the plan comment), and the plan that shows a manual apply before approval.
  assert {
    condition = toset(flatten([
      for s in jsondecode(aws_iam_role.github["gha-tf-plan"].assume_role_policy).Statement :
      s.Condition.StringEquals["token.actions.githubusercontent.com:sub"]
    ])) == toset(["repo:toolikheng/devops-platform:pull_request", "repo:toolikheng/devops-platform:ref:refs/heads/master"])
    error_message = "gha-tf-plan must trust pull requests and runs on master, nothing else."
  }

  # The environments' own rules add the rest: aws-apply needs the owner's approval, and all three accept
  # master only, so a pull request can never get these roles.
  assert {
    condition = toset(flatten([
      for s in jsondecode(aws_iam_role.github["gha-tf-apply"].assume_role_policy).Statement :
      s.Condition.StringEquals["token.actions.githubusercontent.com:sub"]
    ])) == toset(["repo:toolikheng/devops-platform:environment:aws-apply"])
    error_message = "gha-tf-apply must trust only jobs in the aws-apply environment."
  }

  assert {
    condition = toset(flatten([
      for s in jsondecode(aws_iam_role.github["gha-tf-destroy"].assume_role_policy).Statement :
      s.Condition.StringEquals["token.actions.githubusercontent.com:sub"]
    ])) == toset(["repo:toolikheng/devops-platform:environment:aws-nightly"])
    error_message = "gha-tf-destroy must trust only jobs in the aws-nightly environment."
  }

  assert {
    condition = toset(flatten([
      for s in jsondecode(aws_iam_role.github["gha-deploy"].assume_role_policy).Statement :
      s.Condition.StringEquals["token.actions.githubusercontent.com:sub"]
    ])) == toset(["repo:toolikheng/devops-platform:environment:aws-deploy"])
    error_message = "gha-deploy must trust only jobs in the aws-deploy environment."
  }
}

run "credentials_last_one_hour_at_most" {
  command = apply

  assert {
    condition     = alltrue([for role in aws_iam_role.github : role.max_session_duration == 3600])
    error_message = "GitHub roles keep the 1-hour maximum session: long enough for any job, no longer."
  }
}
