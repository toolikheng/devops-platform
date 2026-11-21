# ADR 013: Ansible reaches the AWS node with SSH inside Session Manager

## Status

Accepted

## Context

Phase C3 runs the homelab's Ansible roles (`common`, `node_exporter`) and a new `k3s` role on the AWS node, and gets a kubeconfig to my laptop. Ansible needs a way in, and so does kubectl. The node's security group has no inbound rule except TCP 80 from my /32, and I want to keep it that way: an open 22 or 6443 is what scanners look for first.

The SSM agent on the node already keeps an outbound HTTPS connection to AWS (Phase C2), and `aws ssm start-session` gives a shell through it. Session Manager can also carry other traffic: the `AWS-StartSSHSession` document carries an SSH connection, and `AWS-StartPortForwardingSession` forwards a port.

Options for Ansible:

| Option | Inbound port | What it needs | Notes |
|---|---|---|---|
| SSH from my /32 | 22 open to one address | security group rule, my IP kept current | simplest, but an admin port on the internet, and my IP changes |
| `community.aws.aws_ssm` connection plugin | none | an S3 bucket for file transfer, `s3:*Object` on the node role, boto3 on the laptop | no SSH at all, but every module copy goes through S3; slower and more IAM |
| SSH as the Session Manager transport (`ProxyCommand`) | none | the Session Manager plugin, and my key in the C2 key pair | plain SSH as far as Ansible knows |

## Decision

- **SSH inside Session Manager.** `ansible/inventory/group_vars/aws.yml` sets `ProxyCommand="aws ssm start-session --target %h --document-name AWS-StartSSHSession --parameters portNumber=%p"`. The inventory names the host by its **instance ID**, so `%h` is the Session Manager target. Ansible logs in as `ubuntu` with the key from the C2 key pair, then uses sudo.
- **The inventory is generated each session** from `terraform output` (`make cloud-config`) and gitignored, because the instance ID changes every time.
- **Host keys are not recorded** (`UserKnownHostsFile=/dev/null`). The node and its host key are new every session, and the Session Manager session already goes to that exact instance ID after an IAM check, so a host-key prompt would add nothing.
- **kubectl uses Session Manager port forwarding.** `make cloud-tunnel` forwards the node's 6443 to `localhost:16443`, and the kubeconfig that Ansible copies to `~/.kube/devops-aws.yaml` points there. k3s's certificate is valid for 127.0.0.1, so TLS is verified normally.
- **Pipelining on** for this group: one SSH round trip per task instead of several.

## Consequences

- No inbound admin port, measured live: 22, 6443, 9100 (node_exporter) and 10250 (kubelet) were dropped from my laptop while Ansible and kubectl worked. Two checks protect the node: IAM decides who may open a session, then the SSH key decides who may log in.
- The roles see plain SSH, so the same `common` and `node_exporter` roles run on the Docker nodes and on EC2 unchanged, which was the point of ADR 003. The only role change was making `PermitRootLogin` and the login banner title variables, with defaults that leave the homelab output byte for byte the same.
- Every round trip goes through AWS, so it is slower than direct SSH, and Session Manager latency varies: single runs with nothing to change took anywhere from 30 s to 136 s. A full build from a fresh node took 103 s. Pipelining cuts a no-change run from 151 SSH calls to 39, but because SSH already reuses one connection it saves only about 10% of the time (35–39 s against 39–44 s, measured back to back). An earlier single comparison (136 s → 30 s) had made it look like a 4× gain.
- The laptop needs the Session Manager plugin and a valid `aws login`; an expired login shows up as an SSH connection error.
- The admin kubeconfig (cluster-admin credentials) leaves the node, but only over the SSH session, to a 0600 file outside the repo. It is useless without the tunnel, which needs my AWS login.
- A tunnel closed with Ctrl-C ends its session on the AWS side too. Killing the plugin with SIGTERM left the session listed as active until it was terminated.
- CI can't use this path, because it needs my key and my login. The plan for Phase C6 (not built yet) is SSM Run Command from a GitHub-hosted runner with its own narrow role, rather than a self-hosted runner on the node.
