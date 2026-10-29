# ADR 002: k3d instead of kind

## Status

Accepted

## Context

A local Kubernetes cluster is needed for the deployment, rolling-update and monitoring work. The two common options are kind and k3d.

## Decision

Use k3d, which runs k3s (the same distribution used in production edge and small clusters) inside Docker containers.

## Consequences

- k3s ships Traefik as the ingress controller and a local-path storage class, so the manifests use the same ingress and PVC patterns they would use on a real k3s cluster.
- k3d exposes the load balancer on the host with `-p 8080:80@loadbalancer`. Requests to `localhost:8080` reach Traefik, but the ingress rule matches on Host header, so requests must send `Host: devops-app.localhost`.
- kind is lighter and would also work, but it does not exercise k3s-specific behavior.
