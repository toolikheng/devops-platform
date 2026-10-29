# ADR 003: Ansible for node configuration, Terraform deferred

## Status

Accepted. Terraform is deferred to a later phase.

## Context

The project needs repeatable configuration of the nodes: packages, SSH hardening, and node_exporter. Terraform is the usual choice for creating infrastructure such as cloud VMs. Here the nodes already exist as containers.

## Decision

Use Ansible roles for configuration. Nothing in this repo creates infrastructure, so Terraform would have no resources to manage.

## Consequences

- Idempotency is demonstrated by re-running `make provision`, which should report `changed=0` on the second run. That check requires a Linux environment; see the runbook.
- No infrastructure lifecycle management. Creating or destroying nodes is done with `make infra-up` and `make infra-down`.
- When real cloud VMs are added, Terraform would create them and Ansible would configure them. The roles stay the same.
