# ADR 001: Docker systemd containers instead of Multipass VMs

## Status

Accepted

## Context

The plan called for three Ubuntu VMs provisioned by Ansible. Multipass would normally provide them, but this project runs on Windows 10 Home. Windows 10 Home has no Hyper-V, and WSL2 cannot run nested virtualization without Windows 11.

## Decision

Run three Ubuntu 24.04 containers as "nodes". Each runs systemd-like services, sshd with key-only login, and a `homelab` bridge network. Ansible connects over real SSH to `localhost:2201-2203`.

## Consequences

- Ansible uses the same SSH and privilege model as it would on VMs, so the roles and playbooks carry over.
- The nodes share the host kernel, so kernel-level work such as sysctl tuning or kernel modules cannot be tested here.
- The inventory file is the only thing that changes when targeting real VMs. See `ansible/inventory/docker-nodes.yml`.
