# ADR 003: Ansible for node configuration, Terraform deferred

## Status

Accepted. Terraform is deferred until there is infrastructure to create.

## Context

The project needs repeatable configuration of the nodes: packages, timezone, SSH hardening, and node_exporter. Terraform is the usual choice for creating infrastructure such as cloud VMs. Here the nodes already exist as containers.

## Decision

Use Ansible roles for configuration, run from WSL. Nothing in this repo creates cloud infrastructure, so Terraform would have no resources to manage.

## Consequences

- Idempotency is checked by running `make provision` twice against freshly created nodes. The first run changes 12 tasks per node; the second reports `changed=0`.
- Creating and destroying nodes is done with `make infra-up` and `make infra-down`, not by an infrastructure tool.
- When real cloud VMs are added, Terraform would create them and Ansible would configure them with the same roles.
