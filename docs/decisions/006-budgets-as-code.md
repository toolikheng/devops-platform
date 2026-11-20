# ADR 006: Budgets as code, with an automatic deny action at the monthly cap

## Status

Accepted

## Context

The account is on AWS's Paid plan: every hour of EC2, public IPv4 and EBS is billed. A `zero-spend` budget (alert on the first cent) had already been created in the console. Budget alerts are emails: they don't stop anything, and AWS refreshes budget data only about three times a day.

## Decision

- **Adopt the console budget instead of replacing it.** An `import` block brought `zero-spend` into Terraform state on the first apply; the plan showed "1 to import" and the only changes were tags and excluding credits. Nothing was deleted, so there was no gap in alerting. The import block was then removed: an import is a one-time move, and keeping it would make a fresh account fail to plan.
- **`monthly-cap` budget of USD 15** with email alerts when actual spend passes USD 1, 5 and 10. Both budgets exclude credits, so a credit can never hide real spend.
- **At USD 15, an automatic budget action** attaches `devops-platform-deny-new-spend` to the admin user. The policy denies starting anything billable (instances, Spot requests, volumes, snapshots, Elastic IPs, NAT gateways, VPC endpoints, load balancers, RDS, EKS, Secrets Manager, KMS keys, Route 53 zones, ECR, Lightsail, SageMaker, Bedrock calls).
  - It never denies IAM actions, so the policy can always be detached by hand.
  - It never denies S3 state reads and writes or clean-up actions, so a destroy still works after it fires.
- **AWS Budgets runs the action through `devops-platform-budget-action`.**
  - Its trust policy allows only `budgets.amazonaws.com`, acting for this account.
  - Its only permission is attaching or detaching that one policy, on that one user.

## Consequences

- Budgets are free, including up to two action-enabled budgets.
- The deny policy only stops *new* spending. Anything already running keeps billing until it is destroyed, and the action fires up to about a day after the spend because of the refresh delay. The nightly destroy of the ephemeral stack (Phase C6) remains the main protection.
- The action can't be fired as a test without spending USD 15, so it was verified without firing:
  - **Deny policy:** the IAM policy simulator, with the policy added to the admin user, shows `ec2:RunInstances`, `ec2:StartInstances`, `ec2:CreateNatGateway` and `rds:CreateDBInstance` as explicitly denied. `ec2:TerminateInstances`, `ec2:DeleteVolume`, `iam:DetachUserPolicy` and S3 Get/PutObject on the state object stay allowed.
  - **Role:** the simulator shows it may attach or detach only this policy on only this user. AdministratorAccess, other users and roles are denied.
- Terraform tests check the same rules offline. Each test was shown to fail on a deliberately broken copy, for example a wildcard such as `i?m:*` or a mixed-case `S3:Put*` in the deny list.
- After the action fires, the owner decides when to detach the policy (see the runbook). It is not removed automatically at the start of the next month.
