# ADR 008: k3s on one EC2 instance instead of EKS

## Status

Accepted

## Context

The AWS environment has to run PolicyPal (UI, API and Postgres with pgvector) on Kubernetes, reuse what the homelab already does with k3s and Ansible, and cost as little as possible on a paid account with a USD 15 monthly cap. It is destroyed after every working session.

The managed option is EKS. Its control plane alone costs USD 0.10 per hour in ap-southeast-1 (about USD 73 a month), before any worker nodes (AWS Price List API, checked 2026-11-21). It also brings things this project would have to add and explain: node groups, the VPC CNI and its IP planning, and in practice a NAT gateway or VPC endpoints for private nodes. The project's cost rules forbid EKS unless I approve it in writing with a cost estimate.

## Decision

- Run **k3s on a single EC2 instance** that Terraform creates in the ephemeral stack and Ansible configures (Phase C3).
- Phase C2 builds only the instance and its network. Its shape is chosen for k3s from the start:
  - Ubuntu 24.04 amd64, because the PolicyPal image is amd64 only
  - a 20 GB encrypted gp3 root volume
  - IMDSv2 required with a hop limit of 1, so ordinary pods can't read the node's credentials
  - no inbound SSH or Kubernetes API; access through SSM Session Manager
- The instance type comes from the project's allowlist and is the smallest that fits the measured workload: t3.micro for C2, t3.small for C3, c7i-flex.large once PolicyPal runs.

## Consequences

- Cost while running: the instance plus a public IPv4 and the volume. For t3.micro that is about USD 0.021 per hour on-demand and USD 0.012 on Spot, measured in C2 (see [aws-test-log.md](../aws-test-log.md)). Nothing at all is billed once the stack is destroyed.
- The same k3s and Ansible approach as the homelab, so the skills and roles carry over.
- **No high availability.** One node is one failure domain. A Spot interruption or a terminated instance takes the app down until it is rebuilt, and the data since the last backup is lost (backups to S3 come in C7).
- **I run the control plane myself:** k3s upgrades, certificates and etcd/SQLite are my problem, not AWS's.
- **No per-pod IAM.** EKS offers IRSA or Pod Identity. Here every process that can reach the instance metadata service shares the node role, which is why the hop limit keeps ordinary pods away from it and the role grants so little.
- At a real company with a team and an SLA, EKS (or another managed control plane) would usually be worth its price.
