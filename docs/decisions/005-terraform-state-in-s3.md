# ADR 005: Terraform state in S3 with native locking, split into persistent and ephemeral stacks

## Status

Accepted

## Context

The AWS environment is billed by the hour, so anything that runs is destroyed after every working session. Some resources must outlive those sessions and cost nothing at rest: the bucket that holds Terraform state, the budgets, IAM roles and SSM parameters.

Terraform state records which real resource each block of code manages. If it is lost, Terraform can no longer manage what it created. It can also contain sensitive attributes. A state file on one laptop can be lost with the laptop and can't be used by CI.

## Decision

- Two root stacks. `terraform/persistent` holds only resources that cost about $0 at rest and is applied from the laptop. The ephemeral stack (Phase C2) holds everything billed by the hour and is destroyed after each session.
- State lives in an S3 bucket created by the persistent stack:
  - versioned, so a bad write can be rolled back
  - SSE-S3 encryption, with no customer-managed KMS key to pay for
  - all four public access blocks on
  - a bucket policy that denies any request without TLS
  - old versions expire after 90 days, keeping the newest 10
  - `prevent_destroy`
- Locking uses the S3 backend's `use_lockfile = true` (Terraform 1.10+). A `.tflock` object is created with a conditional write next to the state, so a second run fails to get the lock instead of overwriting the state. No DynamoDB table is needed.
- Bootstrap, because the bucket that stores state is created by Terraform:
  1. The first apply ran without a backend (local state) and created the bucket.
  2. `backend.tf` was then added and `terraform init -migrate-state` copied the state into the bucket.
  3. The local copies were deleted.

## Consequences

- Verified live:
  - after the migration, a plan through the S3 backend shows no changes
  - a request over plain HTTP gets AccessDenied, even for the admin user, while HTTPS works
  - two plans started at once: the second fails with "Error acquiring the state lock" and the first completes
- The persistent stack is never destroyed with the ephemeral one. `prevent_destroy` makes Terraform refuse to delete the state bucket.
- Rebuilding the persistent stack in a new account needs the same bootstrap (see the runbook): the backend can't point at a bucket that doesn't exist yet.
- State is readable by anyone with S3 access to the bucket. Secrets must stay out of it, which is why later phases use Terraform write-only arguments for SSM parameter values.
