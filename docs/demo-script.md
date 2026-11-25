# Demo script (about 2 minutes 45 seconds)

A short walkthrough of the AWS environment for a recording or a live call. Each step comes from a real session ([test log](aws-test-log.md)), with two exceptions to try once before recording: `policypal-backup enable`, added after the last AWS session, and an m7i-flex.large on-demand (on-demand was tested on a t3.micro). The slow parts (building the node, the add-ons, the alert's 2-minute wait, the restore across a rebuild) are done before recording, so the recording shows their results instead of waiting for them.

## Before recording (about 30 minutes, about USD 0.07)

The recording session runs **on-demand**, so a Spot interruption can't end it halfway ([ADR 009](decisions/009-spot-and-on-demand.md)): an m7i-flex.large stack costs about USD 0.127/h on-demand.

```bash
aws login --profile devops
make cost-check                                        # clean before starting
make skip-nightly-destroy                              # if recording near 02:17 MYT
make cloud-rebuild CLOUD_ARGS='-var use_spot=false'    # plan, "yes", then Ansible (~3 min)
make cloud-tunnel                                      # second terminal, leave it open
make cloud-addons                                      # ~3 min
make cloud-deploy                                      # ~2 min, smoke test 6/6
```

Then, on the node (`aws ssm start-session --target "$(bash scripts/node-id.sh)"`, `sudo -i`): `policypal-backup restore latest` if the bucket has a backup to bring back, otherwise `policypal-backup enable`.

Have these ready in tabs or terminals:

1. The README's AWS architecture diagram.
2. The UI at `http://<node-ip>` (`bash scripts/node-ip.sh` prints the IP; only my IP can reach it).
3. Grafana through a port-forward, logged in, on the "PolicyPal on AWS" dashboard ([runbook: Monitoring on AWS](runbook.md#monitoring-on-aws)).
4. The output of the last `make cloud-restore-test-after` (the PASS line), from the [test log](aws-test-log.md) or a fresh run.
5. A terminal with `KUBECONFIG=~/.kube/devops-aws.yaml`.

About 3 minutes before the monitoring part, scale the API to 0 so the alert is firing by then:

```bash
kubectl -n policypal scale deployment/policypal-api --replicas=0
```

## The script

| Time | On screen | What I say |
|---|---|---|
| 0:00–0:15 | README, the AWS architecture diagram | "This is my DevOps platform. It started as a homelab on my laptop; this part runs the same practices on AWS. Terraform builds a one-node Kubernetes environment for each working session and destroys it at the end, and it runs PolicyPal, an HR-policy question-answering app I wrote." |
| 0:15–0:45 | The `make cloud-rebuild` output: the plan (9 to add) and the Ansible recap | "There are two Terraform stacks. The persistent one holds what costs nothing at rest: state, budgets, IAM, secrets. Everything billed by the hour is in the ephemeral one: a VPC with no NAT gateway and one Spot or on-demand instance. Nothing is applied until I've read the plan. Ansible then installs k3s, but the node has no SSH port open: Ansible's SSH runs inside an AWS Session Manager session, and so does kubectl." |
| 0:45–1:10 | Browser: the UI. Ask *"How many days of compassionate leave do I get when my grandmother passes away?"*, then *"How many months of bonus will we get at the end of the year?"* | "Only the UI is public, and only to my IP. The first answer quotes Leave Policy 6.3; the second question isn't covered by any policy, so it refuses instead of making something up." |
| 1:10–1:35 | Terminal: `kubectl -n policypal get externalsecrets` (SecretSynced) | "The database password and the admin token are in SSM Parameter Store. Terraform writes them with write-only arguments, so they're not even in its state, and External Secrets Operator copies them into the cluster with the node's instance role. There are no AWS keys anywhere: GitHub Actions gets one-hour credentials through OIDC, one role per job." |
| 1:35–2:00 | Grafana, the dashboard: "API replicas available" at 0, the alert panel firing. Then `kubectl -n policypal scale deployment/policypal-api --replicas=1` | "Prometheus and Grafana run on the node, sized by measurement: with them, a 4 GB instance peaked at 77% of its memory, over my 75% line, so it's an 8 GB one. I scaled the API to zero a few minutes ago; the alert went pending, then firing after two minutes. Scaling it back clears it." |
| 2:00–2:25 | The `make cloud-restore-test-after` PASS output | "Backups run hourly on the node with restic, to S3, with the node's role. To prove they work I destroyed the whole environment and restored onto a brand-new node: the test checks the exact snapshot, the data and the file's checksum. That test also found a real bug: a new node's first backup could have pushed the good one out of the repository, so on a new node backups now start only after a restore, or when I turn them on." |
| 2:25–2:45 | `make cloud-down`: the plan (9 to destroy), "yes", then the cost check "No billable leftovers" | "At the end of every session I destroy everything billed by the hour and a read-only scan checks nothing is left. The whole AWS build so far cost about 34 cents, with budgets and a nightly destroy as the backstop." |

## After recording

```bash
# Ctrl-C in the tunnel terminal first, then:
make cloud-down        # if not already done on camera
make cost-check
```

Record the session in the cost log. If `make skip-nightly-destroy` was set, it expires by itself.
