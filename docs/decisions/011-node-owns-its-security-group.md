# ADR 011: The compute module owns the node's security group

## Status

Accepted

## Context

The ephemeral stack has two modules: `network` (VPC, subnet, internet gateway, route) and `compute` (the node). At first the node's security group lived in `network`. Every rule in it is about the node, though:

- TCP 80 from my /32, for the app behind Traefik
- no 22 or 6443, because shell, Ansible and kubectl go through Session Manager
- outbound 443 and 80 only, because the node has no NAT gateway and uses its own public IPv4

So the node's exposure was split across two modules. `compute` decided that the node gets a public IPv4 and how it reaches AWS, and `network` decided who may connect to it and where it may connect out. `network` also took the sensitive `owner_cidr` input only to pass it to those rules. An architecture review of Phase C2 flagged the split.

## Decision

- The `compute` module creates the node's security group, with the same inline rules as before.
- Its inputs change from `security_group_ids` to `vpc_id` plus `owner_cidr`. `owner_cidr` keeps its rule: exactly one IPv4 address as a /32.
- The `network` module is plumbing only: VPC, public subnet, internet gateway, default route. Its outputs are `vpc_id` and `subnet_id`, and it has no sensitive input.
- The security-group tests moved to `terraform/modules/compute/tests/security_group.tftest.hcl`.

## Consequences

- Everything about how the node is reached is in one module and tested together: public IPv4, security group, IMDSv2 and the instance profile it is given.
- Behaviour is unchanged: the same rules, and the same group name (`devops-platform-node`). The tests (46 `terraform test` runs) and a mutation check that breaks each rule in turn pass as before.
- The group's Terraform address changed from `module.network.aws_security_group.node` to `module.compute.aws_security_group.node`. No state move was needed, because the ephemeral stack is destroyed at the end of every session. Making this change while a stack is running would make Terraform replace the group; a `moved` block would avoid that.
- Rejected: a separate module just for the security group. A module holding one resource would be almost all interface, and the rules belong with the node they protect.
