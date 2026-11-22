# ADR 020: GitHub Actions reaches AWS through OIDC with one role per job, and IAM changes only from my laptop

## Status

Accepted

## Context

From C6, GitHub Actions plans and applies the ephemeral stack, destroys it every night and deploys PolicyPal. It needs AWS credentials for that. The project's rule is no long-lived AWS keys anywhere, and the repository is public, so its Actions logs are public too.

| Option | Problem |
|---|---|
| An IAM user's access keys in GitHub secrets | a long-lived key that works from anywhere until someone rotates it |
| **GitHub OIDC**: GitHub signs a token for each job, STS swaps it for short-lived credentials of a role whose trust policy names the job | none of the above; the trust policy has to be exact |

With OIDC the questions are how many roles, what each may do, and who changes IAM.

## Decision

- **One OIDC provider** (`token.actions.githubusercontent.com`, audience `sts.amazonaws.com`) and **four roles**, in the persistent stack (`terraform/persistent/github_oidc.tf`):

  | Role | Trusts (token `sub`) | May |
  |---|---|---|
  | `gha-tf-plan` | `repo:toolikheng/devops-platform:pull_request` and `…:ref:refs/heads/master` | read: EC2 descriptions, the node's instance profile, the owner-cidr parameter, the ephemeral state only |
  | `gha-tf-apply` | `…:environment:aws-apply` | build and remove the ephemeral stack, nothing in IAM |
  | `gha-tf-destroy` | `…:environment:aws-nightly` | read and delete, never create; run the cost check |
  | `gha-deploy` | `…:environment:aws-deploy` | send `AWS-RunShellScript` to the tagged node and read the result |

  Both claims are matched with `StringEquals`. A wildcard such as `repo:toolikheng/*` would trust every repository and branch.
- **The environments do the rest.** A job in a GitHub Environment gets `environment:<name>` as its subject whatever its branch, so `aws-apply`, `aws-deploy` and `aws-nightly` accept `master` only, and `aws-apply` also waits for my approval. Fork pull requests get no OIDC token at all.
- **Permissions come from real runs.** The EC2 actions are the calls the Terraform AWS provider made in a Spot apply and destroy (C5) and an on-demand apply (C2), read from CloudTrail event history. The apply role may launch only in ap-southeast-1, and Deny statements on `ec2:RunInstances` repeat the cost rules in IAM: instance types on the allowlist, volumes encrypted and 30 GB or less, Canonical images only. It passes only the node role, only to EC2, and may tag only while creating. Apply and destroy delete only resources tagged `Project=devops-platform` and `Stack=ephemeral` (the Spot request, created untagged by AWS, is the exception: cancelling it costs nothing). The state access is `envs/aws/terraform.tfstate` and its lockfile only.
- **IAM changes only from my laptop.** The persistent stack (IAM, budgets, the state bucket) is planned and applied with my `aws login` session. No role can change IAM, so a compromised workflow cannot give itself more access.
- **Nothing is applied that I didn't see.** The plan file can't be handed from the plan job to the apply job: it holds my IP in plain text, and a public repo's artifacts can be downloaded. So the plan job prints only addresses and actions (`ci/plan-summary.sh`) with their SHA-256; after my approval the apply job plans again and applies its saved plan only if the hash matches. No `-auto-approve` anywhere.
- **The account ID stays out of the logs.** The workflows build each role ARN from one repository secret, `AWS_ACCOUNT_ID`. GitHub prints a step's inputs in the log and doesn't mask variables, but it masks secrets everywhere.
- **The budget action also covers CI.** At the USD 15 cap it attaches the deny-new-spend policy to `gha-tf-apply` as well as to my user. The other roles can't start anything that bills.

## Consequences

- `gh secret list` shows `AWS_ACCOUNT_ID` only: no AWS key exists in GitHub.
- The offline tests check both sides: the trust and permission lists exactly (`terraform/persistent/tests/github_*.tftest.hcl`), and every workflow job's subject against its role's trust (`tests/test_ci_roles.py`). Mutation testing: 57 of 57 deliberate mistakes caught (for example a wildcard subject, a job without its environment, a missing Deny, an apply without the hash check).
- AWS relies on GitHub's environment rules for "master only" and "approved". Anyone with admin rights on the repo could change them; here that is only me.
- All three environments keep GitHub's default `can_admins_bypass: true`: a repository admin can bypass their protection rules, so for `aws-apply` an admin could apply without the approval step. In a repo where I am the only admin that changes nothing. In a team I would set it to `false` (`"can_admins_bypass": false` in the environment's settings), so the reviewer rule holds for admins too.
- Any branch of this repo can open a pull request and run a read-only plan. It can read the ephemeral state, which holds my IP and the node's IDs, but not change anything.
- `ec2:Describe*` is a wildcard on purpose: read-only, and the provider's describe calls change between versions. Write actions are listed one by one, so a new provider version may need a new permission. The apply then fails with `UnauthorizedOperation`, and the runbook shows how to decode the message.
- A change to IAM always needs a laptop session. That is slower than letting CI do it, and it is the point.
- Not yet measured on AWS: the first PR plan comment, an approved apply, the nightly destroy by schedule and by hand (C6 live sessions).
