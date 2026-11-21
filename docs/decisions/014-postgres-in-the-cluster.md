# ADR 014: PolicyPal's Postgres runs in the cluster, not on RDS

## Status

Accepted

## Context

PolicyPal needs PostgreSQL with the pgvector extension. Its first migration runs `CREATE EXTENSION vector`, so the database user must be allowed to create extensions. Phase C4 deploys it on the k3s node, and the whole ephemeral stack is destroyed after every session (ADR 008).

| Option | Extra cost (ap-southeast-1, before tax) | Fits the session workflow | Notes |
|---|---|---|---|
| Postgres StatefulSet on the node | USD 0; measured 39–101 MiB of the node's memory | yes: created and filled in about 2 minutes by `make cloud-deploy` | same image as PolicyPal's own `docker-compose.yml` (`pgvector/pgvector:0.8.7-pg17`) |
| RDS PostgreSQL db.t4g.micro, Single-AZ | USD 0.025/h plus USD 0.138 per GB-month of gp3 (Price List API, 2026-11-21) | poorly: an RDS instance takes several minutes to create and to delete, every session | needs a DB subnet group in two AZs (a second subnet), its own security group and a password; pgvector is supported |

On Spot the node costs about USD 0.053/h, so RDS would add almost half again to every session. The project rules also list RDS as something used only with written approval and a cost estimate.

## Decision

- **Postgres runs in the cluster** as a one-replica StatefulSet (`k8s/policypal/base/postgres.yaml`) with a 5Gi `local-path` volume, a headless Service named `postgres`, a `pg_isready` readiness probe and a plain TCP liveness probe, so a slow recovery isn't killed.
- **The superuser is `policypal`**, as in PolicyPal's compose file, because the migration creates the `vector` extension. The password comes from the `policypal-db` Secret, never from Git.
- **It runs as the image's `postgres` user (999)**, not root, with no capabilities; `fsGroup` makes the new volume writable, and the data lives in a subfolder (`PGDATA=/var/lib/postgresql/data/pgdata`) because `initdb` wants an empty folder it owns.
- **The data is not kept between sessions.** PolicyPal ingests the sample policies on every start, so a new cluster is useful again within minutes.

## Consequences

- No extra cost, and nothing extra to create or wait for: on a rebuilt node the API ingested all 8 policies (119 chunks) by itself and passed the smoke test 4 minutes after `terraform apply` started.
- No managed backups, patching or failover. The database lives and dies with the node. Phase C7 adds a restic backup of the database and the uploads volume to S3 with a tested restore; until then anything uploaded during a session is lost at `make cloud-down`.
- The `local-path` volume is a folder on the node's root disk, so it shares the 20 GB volume with the images (6.7 GB used with PolicyPal running).
- The application user is the database superuser. In production I'd create the extension once as an admin and give the app a role that owns only its own tables.
- `kubectl apply` reports `statefulset.apps/postgres configured` on every run even when nothing changed: it replaces `volumeClaimTemplates` as a whole, and the server adds default fields to it. The StatefulSet's generation stays the same and the pod isn't restarted (`kubectl diff` shows nothing).
- If the data ever had to outlive the node, RDS (or at least an EBS volume kept outside the ephemeral stack) would be the next step, and this ADR would be revisited with the measured cost.
