#!/bin/sh
# PID 1 for the Docker "VMs", which have no systemd.
set -e

# 1. Install the operator's public key (bind-mounted read-only by docker-compose) with the
#    root ownership and 0600 mode that sshd requires.
install -m 600 -o root -g root /etc/ssh/operator_key.pub /root/.ssh/authorized_keys

# 2. Start the SysV services Ansible enabled (e.g. node_exporter), so they come back after a
#    container restart. sshd is skipped: it runs below as PID 1.
for svc in /etc/rc2.d/S*; do
  case "$svc" in *ssh) continue ;; esac
  if [ -x "$svc" ]; then "$svc" start || echo "node-init: $svc failed to start" >&2; fi
done

# 3. Run sshd in the foreground, logging to the container log.
exec /usr/sbin/sshd -D -e
