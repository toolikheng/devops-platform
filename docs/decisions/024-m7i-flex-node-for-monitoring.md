# ADR 024: The node is an m7i-flex.large once the monitoring runs on it

## Status

Accepted

## Context

From Phase C4 the node was a c7i-flex.large (2 vCPU, 4 GiB), the smallest allowlisted amd64 type that held k3s and PolicyPal; C5 added External Secrets Operator. Phase C7 adds kube-prometheus-stack, trimmed for a small node ([ADR 022](022-one-addon-script-for-every-chart.md), `values-aws.yaml`).

The rule I set before measuring: stay on c7i-flex.large if the node's memory peak is under about 75%, otherwise move to m7i-flex.large (8 GiB), the next type on the allowlist. The peak includes the PolicyPal API's start (database migrations, ingestion, loading the embedding model), because a restart is a normal event: every deploy and every rotation of the admin token restarts it.

## Measurement (2026-11-24, ap-southeast-1, Spot)

Node memory in use is `1 - MemAvailable/MemTotal` from the node's own node_exporter, read through Prometheus (`max_over_time` over each window) and, during the restart, also once a second from inside the cluster. Everything ran: k3s, PolicyPal (API, UI, Postgres), ESO and the monitoring.

| | c7i-flex.large (3,812 MiB) | m7i-flex.large (7,776 MiB) |
|---|---|---|
| Idle, 3 minutes | 75.9% | 38.3% |
| Answering 16 questions (8 one after another, 8 at once) | 76.3% | 38.3% |
| `rollout restart` of the API, until ready and 70 s after | **76.9%** (1-second samples: 76.9%) | **38.1%** (1-second samples: 38.3%) |

The same work takes about 2.9–3.0 GiB on either type. On the 4 GiB node the pods used 1.6 GiB (PolicyPal 717 MiB, monitoring 680, ESO 122, k3s's own add-ons 78) and k3s, containerd and Ubuntu the rest. Nothing ran out of memory on the c7i-flex.large, but its peak is above the line.

## Decision

The default node is an **m7i-flex.large on Spot** (`terraform/envs/aws/variables.tf`, and the default of `terraform.yml`'s instance type input). c7i-flex.large stays on the allowlist; a session without the monitoring can still choose it with `-var instance_type=c7i-flex.large`.

## Consequences

- Spot prices were nearly the same during the measurement: USD 0.0527/h for m7i-flex.large and USD 0.0543/h for c7i-flex.large in ap-southeast-1a, so the stack costs about USD 0.060/h. On-demand is more: USD 0.1197/h against USD 0.0978/h, about USD 0.127/h for the stack. The demo recording, the one planned on-demand run, costs about USD 0.04 more per hour.
- The Spot Instance Advisor listed m7i-flex.large below 5% monthly interruptions, against 5–10% for c7i-flex.large.
- Changing the type of a running node is not an in-place change here: Terraform plans stop, change, start, but a one-time Spot instance can't be stopped. The node is replaced with `terraform apply -replace=module.compute.aws_instance.node` and rebuilt (`make cloud-config`, `cloud-addons`, `cloud-deploy`), which took about 7 minutes.
- Revisit if the monitoring is removed or PolicyPal's memory changes: the measurement is a few commands in the test log.
