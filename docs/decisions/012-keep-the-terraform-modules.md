# ADR 012: Keep the network and compute modules instead of one flat ephemeral stack

## Status

Accepted

## Context

The ephemeral stack (`terraform/envs/aws`) calls two modules, `network` and `compute`, and each is used exactly once. The root stack mostly passes variables through and re-exports outputs, and each module carries its own `versions.tf` and lock file. An architecture review of Phase C2 asked whether one flat stack would be simpler. It would remove about 100 lines: two lock files, two `versions.tf`, and the forwarded inputs and outputs.

## Decision

Keep the modules. The root stays a thin composition root that wires them together.

The reason is testability. Each module's rules are tested through its own small interface with plain inputs:

- the security-group and `owner_cidr` rules in `compute`
- the IMDS, volume, allowlist and Spot rules in `compute`
- the network layout in `network`

In a flat stack, the `owner_cidr` rules would become checks on the SSM data source, and every test run would need mocked data-source values to reach them. The root tests already treat `compute` as a black box (`override_module`) and check only wiring, tags and the owner IP where it enters the stack.

## Consequences

- A provider upgrade updates the lock file in each Terraform directory.
- New inputs that only pass through the root are a smell. Prefer module defaults over forwarding knobs nobody sets, as was done for `root_volume_gb`.
- Revisit if a module stops having rules of its own to test, or if the forwarding grows larger than the modules.
