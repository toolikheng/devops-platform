# ADR 023: One catalog lists every secret and who reads it

## Status

Accepted

## Context

Phase C5 made two SSM SecureStrings, `/policypal/aws/db-password` and `/policypal/aws/admin-token` ([ADR 016](016-parameter-store-and-external-secrets.md)). Each was written out in full: an `ephemeral "random_password"`, an `aws_ssm_parameter` and a version variable, and the Terraform tests named each one. Who may read a secret was stated in three places: the node role's path (`parameter/policypal/aws/*`), the `ClusterSecretStore`'s namespace list, and a test that required every parameter to be read by PolicyPal's aws overlay.

Phase C7 adds a third secret, the restic repository password. The backup runs on the node itself (a systemd timer with the node's role), not in a pod, so it reads the password without External Secrets Operator. Under the old shape that meant another copy of the Terraform blocks, another variable, new test lines, and a test that would have failed because PolicyPal doesn't read it. In C5 I had chosen to wait for the third secret before changing this.

## Decision

- **`terraform/persistent/secret-catalog.yaml`** lists every secret by its name under `/policypal/aws/`, with its description and, when an ExternalSecret reads it, that ExternalSecret's namespace. An entry with no namespace is read by the node itself.
- **Terraform reads the catalog** (`yamldecode`) and makes one ephemeral random value and one SecureString per entry with `for_each`. Each version is an entry of one map variable, `secret_versions`; a validation refuses a map whose keys differ from the catalog's, so a misspelt name can't add a version that rotates nothing.
- **`moved` blocks** move the two existing parameters to their new addresses, so they keep their values. The apply that records the move has to show 0 to add, 0 to change and 0 to destroy.
- **The tests read the same file:** each catalogued secret with a namespace is read by exactly one ExternalSecret in that namespace, nothing else is read, and the store serves exactly the catalog's namespaces, which must be `policypal` alone.

## Consequences

- Adding a secret is one catalog entry, one version, and (for a namespace) one ExternalSecret. The runbook has the steps.
- A rotation is still a one-line commit: raise that secret's number in `secret_versions`.
- The node role's permission is a path (`parameter/policypal/aws/*`), so it can read a new secret under that path with no IAM change. The other side of that: ESO uses the same role, so any ExternalSecret in `policypal` could read any secret under the path, the restic password included. The store's namespace list and the tests limit which namespaces can ask; the role can't tell one secret from another ([ADR 017](017-imds-hop-limit-and-host-network.md) already notes that host-network workloads share one role).
- The two existing descriptions are kept word for word so that the move changes nothing in AWS. The admin token's still says "raise admin_token_version"; it is corrected in the same apply that adds the restic password, which changes the stack anyway.
