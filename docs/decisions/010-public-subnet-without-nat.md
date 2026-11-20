# ADR 010: A public subnet with an internet gateway; no NAT gateway, Elastic IP or VPC endpoints; access only through Session Manager

## Status

Accepted

## Context

The node needs outbound internet access: the SSM agent talks to AWS endpoints, k3s and Helm download releases, apt uses Ubuntu's mirrors, and the PolicyPal image comes from ghcr.io. I also need a shell on it, and later kubectl access, without exposing anything to the internet.

The usual production layout puts instances in a private subnet behind a NAT gateway, with VPC endpoints for AWS services. In ap-southeast-1 a NAT gateway costs USD 0.059 per hour plus USD 0.059 per GB processed, and each interface endpoint USD 0.013 per hour per Availability Zone (AWS Price List API, checked 2026-11-21). The NAT gateway alone costs more than four times the t3.micro node, and the project's cost rules forbid both without written approval. An Elastic IP would give a fixed address, but it is billed at USD 0.005 per hour whether or not the instance runs.

## Decision

- **Network:** one VPC (10.20.0.0/16) with one public subnet (10.20.1.0/24) in ap-southeast-1a. A route table sends 0.0.0.0/0 to an internet gateway. The subnet does not hand out public IPs by default; the instance asks for its own.
- **Public address:** the instance's auto-assigned public IPv4, billed only while it runs. It changes every session, which is fine because nothing outside points at it. ghcr.io has no IPv6, so IPv6-only was not an option.
- **Security group,** with inline rules so Terraform owns the whole set and removes anything added by hand:
  - inbound: TCP 80 from my own /32 only, stored in the SSM parameter `/devops-platform/owner-cidr` by `make owner-ip`
  - no inbound 22 (SSH) or 6443 (Kubernetes API) from anywhere
  - outbound: TCP 443 and 80 only
- **Access without inbound ports:** a shell through SSM Session Manager. The agent on the node connects out to AWS, and my session reaches it through AWS after an IAM check. From C3, Ansible uses SSH *tunnelled through* Session Manager, and kubectl a Session Manager port forward.
- **Node role:** an inline policy with exactly the 11 actions the SSM agent needs for Session Manager and Run Command. The usual managed policy, `AmazonSSMManagedInstanceCore`, also allows reading every SSM parameter, which would let anything on the node read every secret once C5 adds them.

## Consequences

- Measured in C2 (see [aws-test-log.md](../aws-test-log.md)): Session Manager worked with the minimal policy. From my IP, port 80 was reachable while 22 and 6443 were dropped even though sshd was listening. Outbound HTTPS and HTTP worked; other outbound ports were blocked.
- The node has a public IP, so the security group is the only thing between it and the internet. Only one port is open, to one address, and only while a session runs.
- Outbound 443 and 80 go to `0.0.0.0/0`, because the endpoints (SSM, GitHub, GHCR, apt mirrors) have changing addresses and there is no NAT, proxy or endpoint to narrow it. Trivy flags this (AWS-0104) and the finding is accepted with that reason in the code. The same goes for VPC flow logs (AWS-0178): they would need CloudWatch log ingestion, which the cost rules exclude.
- If my IP changes, `make owner-ip && make cloud-up` updates the rule in place. Until then the app is unreachable, while Session Manager keeps working because it needs no inbound rule.
- Security groups don't filter traffic to the VPC DNS resolver, the instance metadata service or Amazon Time Sync, so those keep working without rules.
- At a real company: private subnets, a NAT gateway or endpoints, and an ALB with TLS in front of the app.
