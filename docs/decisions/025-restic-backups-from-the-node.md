# ADR 025: PolicyPal is backed up to S3 by restic on the node, and only restic deletes backups

## Status

Accepted

## Context

Every session builds a new node and the nightly destroy removes it, so PolicyPal's data (the Postgres database and the uploaded files) must live outside the node to survive. The two belong together: PolicyPal deletes database rows whose upload file is missing, so a restore needs a dump and the uploads from the same moment.

A backup job needs three things at once:

1. The uploads volume. It is ReadWriteOnce and lives in the `policypal` namespace, so only a pod there can mount it.
2. AWS credentials to write to S3. With the IMDSv2 hop limit of 1, only processes on the node's host network get the node role's credentials ([ADR 017](017-imds-hop-limit-and-host-network.md)).
3. A namespace that allows that. The `policypal` namespace enforces Pod Security "baseline", which forbids host-network pods.

A CronJob can't have all three without undoing the Pod Security decision or raising the hop limit, which would give every pod the node's credentials.

## Decision

- **The backup runs on the node itself**, as a systemd timer installed by Ansible (role `restic_backup`), like node_exporter. It runs as root every hour: it reaches Postgres and the volume through k3s, and S3 with the node role.
- **restic** (0.19.1, pinned in `ci/versions.env` with the homelab backup's image, downloaded and checked against the release's sha256) writes to `s3://toolikheng-devops-platform-backups/restic`. It encrypts and deduplicates before anything leaves the node.
- **Order:** the uploads directory is looked up from the volume bound to `policypal-uploads` and must exist; then restic runs `pg_dump` in the Postgres pod (`--stdin-from-command`: no snapshot if the dump fails); then the uploads. Both snapshots of a run share a `run-<time>` tag, so a restore takes the pair.
- **The password** is the `restic-password` entry of the secret catalog ([ADR 023](023-one-catalog-of-secrets.md)), read from SSM Parameter Store at run time through `RESTIC_PASSWORD_COMMAND` by a short Python helper (boto3 from Ubuntu's package). It is never written to disk, a log or a command line. The node role could already read it (path permission); its S3 permission covers only the `restic/` prefix of that bucket.
- **Retention is restic's**: 7 daily, 4 weekly and 12 monthly snapshots for each kind, then `prune` and `check`. The bucket's only lifecycle rule removes noncurrent versions after 7 days and unfinished uploads after 1 day, and never current objects. A data file written once is used by every later snapshot that contains the same data, so an age-based expiry would corrupt the repository.
- **A failed run is visible:** the script exits non-zero on any error, so the unit shows failed (`systemctl status policypal-backup`). Retention never runs after a run that saved nothing. A successful run writes its time for node_exporter's textfile collector (an off-by-default option of the node_exporter role, on for AWS only), and the dashboard shows the time since.
- **Restore** is the same script (`policypal-backup restore RUN`): the API stopped, the database recreated and the dump loaded, the uploads directory made exactly the run's, the API started. A failure leaves the API stopped rather than running it on half-restored data. `scripts/restore-test.sh` proves it across a rebuild: sentinels, a backup, a new node, the restore, and the sentinels and the snapshot IDs checked.

## Consequences

- No pod in `policypal` needs the host network, and the hop limit stays at 1. The backup is not a Kubernetes object: `kubectl` doesn't show it; `systemctl` and `journalctl` do.
- The script depends on how k3s's local-path provisioner records a volume's directory (`spec.local.path` in this k3s; it reads `spec.hostPath.path` too).
- Versioning plus the 7-day rule means something restic deleted, or something deleted by mistake, can still be recovered for a week.
- Not rotated: changing the repository password needs `restic key add` and `restic key remove`; a new secret version alone would lock the repository.
- The dump is Postgres's compressed custom format, so restic can't deduplicate it between runs (about 265 KiB each on k3d). Small enough to leave for now.
