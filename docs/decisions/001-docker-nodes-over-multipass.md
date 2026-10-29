# ADR 001: Docker containers instead of Multipass VMs

## Status

Accepted

## Context

The plan called for three Ubuntu VMs provisioned by Ansible. Multipass would normally provide them, but this project runs on Windows 10 Home. Windows 10 Home has no Hyper-V, and WSL2 cannot run nested virtualization without Windows 11.

## Decision

Run three Ubuntu 24.04 containers as "nodes" on a `homelab` bridge network. Each runs a small startup script as PID 1: it installs the operator's SSH public key (mounted read-only from `~/.ssh/id_ed25519.pub`), starts any init scripts Ansible enabled, then runs `sshd` in the foreground with key-only login. There is no systemd. Ansible connects over real SSH to `localhost:2201-2203`.

## Consequences

- Ansible uses the same SSH and privilege model as it would on VMs, so the playbook and roles carry over. `make provision` is idempotent against these nodes: a second run reports `changed=0`.
- Without systemd, services need another way to run. The `node_exporter` role checks the host's service manager and installs a SysV init script on these nodes, or a systemd unit on hosts that have systemd. Only the SysV path has been exercised.
- The nodes share the host kernel, so kernel-level work such as sysctl tuning or kernel modules cannot be tested here.
- The inventory (`ansible/inventory/docker-nodes.yml`) is the part that should change when targeting real VMs. That has not been tried yet.
