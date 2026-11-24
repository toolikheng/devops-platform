# ADR 016: PolicyPal's secrets live in SSM Parameter Store, written by Terraform without entering its state, and External Secrets Operator copies them into the cluster

## Status

Accepted

## Context

PolicyPal needs two secrets on AWS: the Postgres password (in `POSTGRES_PASSWORD` and inside `DATABASE_URL`) and the admin token (`X-Admin-Token`). Until now a script created them as Kubernetes Secrets from my laptop at deploy time. That stops working once GitHub Actions deploys (C6), and it means the values exist only inside a cluster that is destroyed every session.

Three questions: where the values are kept, how they are created, and how they reach the pods. Rules that apply: no long-lived AWS keys anywhere, the Free-Tier-style cost limits (Secrets Manager and customer-managed KMS keys need written approval), and secrets should stay out of Terraform state where possible.

| Where | Cost here | Notes |
|---|---|---|
| Secrets Manager | USD 0.40 per secret per month | built-in rotation, but forbidden without approval, and rotation of these two values is a one-line change anyway |
| **SSM Parameter Store, SecureString, Standard tier, AWS managed key `aws/ssm`** | free (decrypts are within KMS's 20,000 free requests a month) | versioned; IAM can scope access by path |
| HashiCorp Vault | another service to run and keep unsealed | too much for two values |
| SOPS or Sealed Secrets in Git | free | encrypted values in a public repo, plus a key to manage |

| How the value is created | Value in Terraform state? |
|---|---|
| `aws_ssm_parameter` with `value` and `lifecycle { ignore_changes = [value] }` | yes: the provider reads the value back into state |
| a script with `aws ssm put-parameter` | no, but the parameter is then outside Terraform |
| **`ephemeral "random_password"` + the write-only argument `value_wo` (Terraform 1.11+)** | no: neither the plan nor the state holds it |

## Decision

- **Two SecureString parameters**, `/policypal/aws/db-password` and `/policypal/aws/admin-token`, Standard tier, `key_id = "alias/aws/ssm"`, in the persistent stack (`terraform/persistent/policypal_secrets.tf`).
- **Terraform writes them write-only.** Each value comes from an `ephemeral "random_password"` (32 letters and digits, so it fits in a URL without escaping) through `value_wo`. The state keeps only `value_wo_version`. Raising `db_password_version` or `admin_token_version` in `variables.tf` writes a new random value, so **a rotation is a one-line commit**: Git records when, never what. *(Since Phase C7 the secrets are entries of `secret-catalog.yaml` and their versions one map, `secret_versions`: [ADR 023](023-one-catalog-of-secrets.md).)*
- **The node may read only these.** An inline policy on the node role allows `ssm:GetParameter` on `parameter/policypal/aws/*`, nothing else. No `kms:` permission is needed: the `aws/ssm` key lets anyone who may read the parameter decrypt it through SSM.
- **External Secrets Operator** (chart 2.15.0, pinned in `k8s/platform/external-secrets/chart.env`) copies them into the cluster. A `ClusterSecretStore` reads Parameter Store in ap-southeast-1 with **no credentials of its own**: it uses the node's role (how the controller reaches it is [ADR 017](017-imds-hop-limit-and-host-network.md)). Only the `policypal` namespace may use the store. Two `ExternalSecret`s in the aws overlay make `policypal-db` (`POSTGRES_PASSWORD`) and `policypal-admin` (`ADMIN_TOKEN`), one key each with no template, refreshed every hour.
- **The API builds its database URL** in `base/api.yaml`, from the password: `DATABASE_URL` is `postgresql+psycopg://policypal:$(POSTGRES_PASSWORD)@postgres:5432/policypal`, and Kubernetes fills in the variable defined just above it in the same container. PolicyPal's compose file builds it the same way. The URL is written once, for both clusters, and neither the ExternalSecret nor the k3d script has to keep a copy of it in step.
- **k3d keeps the script.** The local cluster has no AWS identity, and giving it AWS keys would put long-lived credentials in Kubernetes, so `make policypal-local` still creates random local Secrets.
- **The deploy waits for the Secrets** after the apply (up to 2 minutes, one `kubectl wait --for=create` per Secret), because the ExternalSecrets are part of that apply. `make cloud-addons` installs ESO once per new node, before the first `make cloud-deploy`.
- **`make check-state-secrets` proves it**: it reads each live value from SSM and counts the state lines that contain it, printing only the counts.

## Consequences

- Measured on AWS: the state check printed 0 and 0 after the first apply and again after rotating the admin token; a second plan showed no changes. ESO made both Secrets within the same second as the ExternalSecrets, and recreated a deleted Secret in 0.2 s.
- Rotating the admin token (version 1 to 2): ESO had the new value one second after a forced sync, but the running API kept accepting the old token until `kubectl rollout restart`, because environment variables are read when a container starts. After the restart (32 s) the new token got 200 and the old one 403. SSM keeps the old version in the parameter's history.
- **The database password can't be rotated this way alone.** Postgres keeps the password it was initialised with, so a new value in SSM would lock the API out until `ALTER ROLE` sets the same password in Postgres (runbook). Each session starts a new database anyway.
- The API container also has `POSTGRES_PASSWORD` in its environment (PolicyPal ignores settings it doesn't know). Its `DATABASE_URL` already contained the same password, so nothing new is exposed. This replaced ExternalSecrets that templated the URL, after the AWS session above: checked on k3d (the URL expanded with the Secret's password, smoke test 6/6), not yet on AWS.
- Nobody sees the values, me included, unless I read them from SSM. Terraform can't notice if someone changes a parameter in the console: it doesn't know the value.
- ESO is another component to run and update: three pods, about 110 MiB on the node. Its ExternalSecret and ClusterSecretStore are custom resources, so kubeconform needs their schemas (a pinned CRDs catalog) and can't validate the chart's own CustomResourceDefinitions (no schema exists for that kind).
- `kubectl wait --for=create` waits only when given a single resource: with two Secret names it failed at once with NotFound. Found on k3d; the deploy now waits for each Secret separately.
