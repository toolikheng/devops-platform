# ADR 007: Cost Anomaly Detection stays in the console, not in Terraform

## Status

Accepted

## Context

AWS Cost Anomaly Detection uses machine learning to flag unusual spend. AWS sets up an "AWS services" monitor with an email alert for most accounts automatically. Managing it in Terraform means calling the Cost Explorer API, which is billed at USD 0.01 per request, on every plan. The detector also needs about 10 days of history for a new service, and can take up to 24 hours to notice an anomaly.

## Decision

Don't manage Cost Anomaly Detection in Terraform. In Phase C0 the owner confirmed in the Billing console that the AWS services monitor and its email alert subscription are in place. No script or workflow in this repository calls the Cost Explorer API.

## Consequences

- No per-request Cost Explorer charges from plans or CI.
- The monitor is the one AWS resource configured by hand, as part of the one-time account-safety setup. If it is deleted, it has to be re-created in the console.
- For working sessions of a few hours, the faster guards are the ones in code: the zero-spend and monthly-cap budgets with the deny action, the nightly destroy, and `make cost-check`.
