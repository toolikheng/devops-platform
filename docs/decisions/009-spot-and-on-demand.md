# ADR 009: Spot by default, on-demand when an interruption would waste my time

## Status

Accepted

## Context

The node runs only during working sessions of a few hours and is destroyed afterwards, on a paid account. Spot instances use spare EC2 capacity at a discount, but AWS can take them back with two minutes' notice. In ap-southeast-1 (prices fetched for the plan, before tax):

| Type | On-demand | Spot (typical) | Monthly interruption rate (Spot Instance Advisor) |
|---|---|---|---|
| t3.micro | USD 0.0132/h | about USD 0.0043–0.0047/h | 5–10% |
| t3.small | USD 0.0264/h | about USD 0.011/h | 5–10% |
| c7i-flex.large | USD 0.0978/h | about USD 0.053/h | 5–10% |

A public IPv4 (USD 0.005/h) and the volume (about USD 0.0026/h for 20 GB) cost the same either way, so on the smallest type Spot saves less in proportion than the instance price suggests.

## Decision

- One variable, `use_spot` (default `false`), switches the node between on-demand and Spot: `make cloud-up CLOUD_ARGS='-var use_spot=true'`.
- Spot is a **one-time** request that **terminates** on interruption, with **no maximum price**. The price cap is then the on-demand price, so the node is never priced out mid-session, and nothing is left stopped or waiting to relaunch.
- **On-demand** for the first ever apply of a phase and for recording the demo, where an interruption would waste human time. **Spot** for routine sessions from Phase C3 on.
- `make cost-check` lists open or active Spot requests in the home region, because an open request could start an instance by itself.

## Consequences

- Measured in C2 (see [aws-test-log.md](../aws-test-log.md)): the Spot request was one-time, terminate on interruption, with a cap of USD 0.0132, the on-demand price. The whole stack cost about USD 0.012/h on Spot against USD 0.021/h on-demand.
- **The first Spot apply failed.** AWS refuses to change `instanceInitiatedShutdownBehavior` on a Spot instance (`UnsupportedOperation`), and the AWS provider sets it with a second call after launch. The offline tests could not see this, because a mock provider makes no API calls. The instance was in Terraform state, so a normal destroy cleaned it up. The attribute is now set for on-demand nodes only; AWS already gives one-time Spot instances `terminate`, which the retry confirmed.
- An interruption costs one rebuild (about 15 minutes once Ansible exists) and any data written since the last backup. The data is thrown away at the end of each session anyway.
- If Spot capacity runs out, the apply fails with `InsufficientInstanceCapacity`. The way out is on-demand (`use_spot=false`) or another Availability Zone.
- Savings Plans and Reserved Instances don't fit a node that runs a few hours a week, and the cost rules forbid them.
