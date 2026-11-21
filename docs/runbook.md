# Runbook

Commands are run from the repo root in WSL2. `make help` lists every target.

## Prerequisites

- WSL2 Ubuntu with Docker Desktop running (WSL integration on for this distro).
- `make check` must pass. It checks `git`, `ssh`, `curl`, `make`, `jq`, `openssl`, `ansible`, `python3` and its venv module, `docker`, `kubectl`, `k3d`, `helm`, that the Docker daemon is reachable, and that `~/.ssh/id_ed25519` exists. `make install` apt-installs missing packages with sudo, generates the SSH key, and prints install links for Docker, kubectl, k3d and helm.
- hadolint, Trivy, promtool and yq are not needed on the host; they run as pinned Docker images.
- Host port 8080 must be free (the k3d load balancer).
- For the AWS targets: AWS CLI v2.32 or newer, the Session Manager plugin and Terraform 1.11 or newer. `make ansible-check` also needs ansible-lint (`pipx install ansible-lint`).

## Everything from scratch

```bash
make all       # check, infra-up, provision, test, build, cluster-up, deploy, monitoring-up (~3.5 min)
make destroy   # delete the k3d cluster and the node containers
```

The fresh-clone run is recorded in [fresh-clone-test.md](fresh-clone-test.md).

## Local cluster and app

```bash
make cluster-up      # create k3d cluster "devops" (1 server, 2 agents, host 8080 -> Traefik), or start it if it exists
make deploy          # build the image, import it into k3d, apply k8s/overlays/dev and the backup CronJob, roll out
kubectl -n devops get pods,svc,ingress
```

The ingress matches on host name, so send it:

```bash
curl -H "Host: devops-app.localhost" http://localhost:8080/healthz   # {"status":"ok"}
curl -H "Host: devops-app.localhost" http://localhost:8080/readyz    # {"status":"ready","dependencies":{"database":"connected"}}
```

`make deploy` always restarts the deployment, because the dev image tag is always `latest`. A restart takes about 25 seconds and drops no requests: the new pod must be Ready first, and the old pod keeps serving for 10 seconds (`preStop` sleep) while Traefik stops routing to it.

Run the app outside Kubernetes with `make run-app` (http://localhost:8000, API docs at `/docs`).

## Manual CD on GitHub

1. Open **Actions → CD → Run workflow** on master.
2. The job builds the image, creates a throwaway k3d cluster on the GitHub-hosted runner, applies `k8s/overlays/dev`, waits for the rollout, and checks `/healthz` and `/readyz` inside the pod.
3. The cluster is deleted when the job ends. Nothing persists.

## Releasing the prod overlay's image

CI pushes `ghcr.io/toolikheng/devops-platform:master` and `:sha-<commit>` for every master commit whose gates pass. The prod overlay pins a sha tag. To move it, set `newTag` in `k8s/overlays/prod/kustomization.yaml` to a sha whose CI run passed, then check:

```bash
kubectl kustomize k8s/overlays/prod | grep image:
```

The overlay always points at an earlier commit, never the one that changes it.

## Scale, roll back, inspect

Do not scale above one replica with the current SQLite database.

```bash
kubectl -n devops rollout undo deployment/devops-app
kubectl -n devops rollout status deployment/devops-app

kubectl -n devops describe pod -l app=devops-app
kubectl -n devops logs deploy/devops-app --tail=100
```

## Monitoring

```bash
make monitoring-up     # kube-prometheus-stack (chart 92.16.0), ServiceMonitor, alert rules, Grafana dashboard
make monitoring-down   # remove the stack
```

Grafana:

```bash
kubectl -n monitoring port-forward svc/kube-prometheus-stack-grafana 3000:80
kubectl -n monitoring get secret grafana-admin -o jsonpath='{.data.admin-password}' | base64 -d; echo   # user: admin
```

The app dashboard is at http://localhost:3000/d/devops-app-metrics. It is loaded automatically from the `devops-app-dashboard` ConfigMap. Panels: request rate by route and status, p95/p99 latency, and the share of requests that were success (2xx/3xx), client errors (4xx) and server errors (5xx).

Prometheus:

```bash
kubectl -n monitoring port-forward svc/kube-prometheus-stack-prometheus 9090:9090
```

Check http://localhost:9090/targets (job `devops-app` and job `docker-nodes`) and http://localhost:9090/alerts. The rules are:

| Alert | Fires when |
|-------|------------|
| AppDown | the app's scrape fails, or there are no app pods at all, for 1 minute |
| HighErrorRate | more than 5% of requests return 5xx for 2 minutes |
| HighLatency | p95 latency is above 0.5 s for 3 minutes |
| NodeExporterDown | a node's node_exporter can't be scraped for 1 minute |

The rules are unit-tested with `bash ci/rules-test.sh` (Docker only); CI runs the same script.

## Infrastructure nodes

```bash
make infra-up         # build the node image and start node1-node3 (SSH on 2201-2203, node_exporter on 9100-9102)
make provision        # run the Ansible playbook from WSL; a second run reports changed=0
make infra-down       # stop and remove the node containers
```

The nodes mount `~/.ssh/id_ed25519.pub` and install it as root's `authorized_keys` at startup. node_exporter runs from a SysV init script on these nodes and restarts with the container. On a host with systemd the role installs a systemd unit instead; that path runs on the AWS node (first run in Phase C3).

`make ansible-check` runs ansible-lint and `--syntax-check` on both playbooks (the homelab's `site.yml` and the AWS node's `aws-node.yml`) without contacting any host. CI runs it too.

## Backup and restore

The CronJob `devops-app-backup` runs daily at 02:00 in namespace `devops`. Each run has two steps:

1. The `snapshot-db` init container copies the live `/data/app.db` with SQLite's online backup API and checks the copy with `PRAGMA integrity_check`. This is safe while the app is serving writes.
2. The `backup` container (restic 0.19.1) backs up that copy as `/app/app.db`, runs `restic check`, and applies retention.

```bash
make backup         # create a Job from the CronJob, wait for it, print both containers' logs
make restore-test   # write items, back up, write a sentinel, wipe /data/app.db, restore, verify exact data
```

`make restore-test` passes only if the app serves exactly the items that were in the backup, the sentinel item written after the backup is gone, and the restore used the snapshot the test just created. If any step fails it still scales the app back up.

To restore for real, scale the app to 0, run the restore Job, and scale back up:

```bash
kubectl -n devops scale deploy/devops-app --replicas=0
job=$(kubectl -n devops create -f backup/restore-job.yaml -o name)   # restores the latest snapshot over /data/app.db
kubectl -n devops wait --for=condition=complete "$job" --timeout=300s && kubectl -n devops logs "$job"
kubectl -n devops scale deploy/devops-app --replicas=1
```

The restic repository lives on the `devops-backup-repo` PVC (5Gi). Snapshots survive pod restarts, but they are on one node's disk with no off-site copy.

**Known limitation: one snapshot per day.** Retention is `--keep-daily 7 --keep-weekly 4 --keep-monthly 12`, applied after every backup. It keeps only the newest snapshot of each day, so running `make backup` (or `make restore-test`) during the day replaces that day's earlier snapshot, including the scheduled 02:00 one. You can restore to the last backup of each of the past 7 days, not to an earlier point within a day.

`make restore-test` wipes and restores `/data/app.db` in the `devops` namespace and leaves its test items behind. Run it only against a dev cluster.

The restic password is generated into the `backup-credentials` Secret by `make secrets-init` (part of `make deploy`). Read it with `kubectl -n devops get secret backup-credentials -o jsonpath='{.data.password}' | base64 -d`.

## AWS cost check

The AWS environment is billed by the hour, so every working session ends with a check that nothing billable is left:

```bash
aws login --profile devops   # short-lived credentials for the IAM user, never root
make cost-check              # AWS_PROFILE=devops and AWS_REGION=ap-southeast-1 by default
```

It is read-only: only describe, list and get calls, which are free. It never calls the Cost Explorer API, which is billed per request. It checks ap-southeast-1 in full, including open or active Spot requests and everything the budget deny policy blocks creating (Route 53 zones, ECR, Lightsail, SageMaker, customer-managed KMS keys), and every other enabled region for instances, volumes and Elastic IPs. That takes about a minute and a half. Closed and cancelled one-time Spot requests are history and are not reported.

| Exit code | Meaning | What to do |
|-----------|---------|------------|
| 0 | No billable leftovers | Nothing |
| 1 | Billable leftovers found (listed with their IDs and region) | Destroy them with Terraform, or in the console if Terraform lost track of them, then run it again |
| 2 | A check could not run (expired login, access denied) | `aws login --profile devops` and run it again. An incomplete scan is never reported as clean |

Persistent resources that cost nothing at rest (S3 state bucket, budgets, IAM, SSM Standard parameters) are not listed.

## Terraform persistent stack

`terraform/persistent` holds the state bucket, budgets and IAM: resources that cost about $0 at rest and are never destroyed with the hourly stack. It is planned and applied from the laptop only:

```bash
aws login --profile devops
cd terraform/persistent
cp terraform.tfvars.example terraform.tfvars   # first time only: set alert_email (the file is gitignored)
terraform init
terraform plan -out=tfplan                     # review it, then:
terraform apply tfplan                         # applies exactly the reviewed plan
```

`make tf-check` runs the offline checks (no credentials needed): fmt, validate, `terraform test` with a mock provider, tflint and trivy.

### Bootstrap in a new account

The backend points at a bucket this stack creates, so in an empty account:

1. `mv backend.tf backend.tf.off`, then `terraform init` and `terraform apply`. This creates the bucket, with state in a local file.
2. `mv backend.tf.off backend.tf`, then `terraform init -migrate-state` to copy the state into the bucket.
3. Check that `terraform plan` shows no changes, then delete the local `terraform.tfstate*` files.

### "Error acquiring the state lock"

Another run holds the lock, a `.tflock` object next to the state. Wait for it to finish. If a run crashed or its login expired mid-apply, the lock can stay behind:

1. Make sure nothing is running: other terminals, and from Phase C6 on, GitHub Actions.
2. Run `terraform force-unlock <LOCK_ID>`, with the ID from the error message.

### The budget action fired

At USD 15 actual spend in a month, AWS Budgets attaches `devops-platform-deny-new-spend` to `toolikheng-admin` and emails the alert address. Starting anything billable now fails with an explicit deny. Deleting, IAM and Terraform state reads and writes still work.

1. Destroy the hourly stack and run `make cost-check` until it is clean.
2. In the Billing console (Bills, by service and region), find what cost money.
3. Fix the cause, then detach the policy deliberately (Billing → Budgets → `monthly-cap` → Actions, or `aws iam detach-user-policy`). It is not removed automatically next month.

## AWS working session (ephemeral stack)

Everything billed by the hour lives in `terraform/envs/aws` and exists only during a session. The default node is a t3.small on Spot: about USD 0.019/h, or USD 0.034/h on-demand (instance, public IPv4 and volume together).

```bash
aws login --profile devops
make cost-check                                  # start from a clean account
make cloud-up                                    # stores your current IP, shows the plan, applies only after you type yes
#   on-demand instead: make cloud-up CLOUD_ARGS='-var use_spot=false'
make cloud-config                                # Ansible over Session Manager: common, node_exporter, k3s (about 2 min)
make cloud-tunnel                                # in a second terminal: localhost:16443 -> the node's 6443; Ctrl-C closes it
KUBECONFIG=~/.kube/devops-aws.yaml kubectl get nodes
aws ssm start-session --target "$(cd terraform/envs/aws && terraform output -raw instance_id)"   # a shell, if needed
# ... work ...
make cloud-down                                  # shows the destroy plan, asks, then runs cost-check
```

`make cloud-rebuild` does `cloud-up` and then `cloud-config`: from nothing to a Ready k3s node in about three minutes, with one "yes" to type. It stops if the apply fails.

`make cloud-config` writes `ansible/inventory/aws.yml` from `terraform output` (the host is the instance ID; the file is gitignored) and runs `ansible/playbooks/aws-node.yml`. SSH goes inside a Session Manager session (`AWS-StartSSHSession`), so port 22 stays closed; the settings are in `ansible/inventory/group_vars/aws.yml`. It changes nothing in AWS. A second run reports `changed=0`. Extra Ansible arguments go in `ANSIBLE_ARGS`, e.g. `make cloud-config ANSIBLE_ARGS='--check --diff'` to see drift on a configured node.

The kubeconfig at `~/.kube/devops-aws.yaml` (mode 600) holds cluster-admin credentials. It points at `https://127.0.0.1:16443`, so it only works while `make cloud-tunnel` runs, and it is replaced on every `make cloud-config`: each session is a new cluster.

`make cloud-up` and `make cloud-down` read `~/.ssh/id_ed25519.pub` (set `SSH_PUBLIC_KEY_FILE` to use another key). `make cloud-down` destroys everything in the stack's state, Spot or not, so it needs no `CLOUD_ARGS`.

The session is over only when `make cost-check` exits 0. Measured runs are recorded in [aws-test-log.md](aws-test-log.md).

### `/devops-platform/owner-cidr does not hold one IPv4 address as a /32`

The plan reads `/devops-platform/owner-cidr`, which still holds the placeholder `unset` or something that isn't a single IPv4 address. This happens when Terraform runs without `make cloud-up`, whose first step stores your current IP. Run `make owner-ip`, or `make cloud-up`.

### The app is unreachable on port 80, but Session Manager works

Your public IP has probably changed. Run `make cloud-up` again: it stores the new IP, and only the security group rule changes, in place. Session Manager needs no inbound rule, so it keeps working.

### The SSM agent doesn't show up, or `start-session` says the target is not connected

The agent normally registers within about a minute of launch. Check `aws ssm describe-instance-information --filters Key=InstanceIds,Values=<id>`. If it never appears:

1. The instance profile must be `devops-platform-node`: `aws ec2 describe-instances --instance-ids <id> --query 'Reservations[0].Instances[0].IamInstanceProfile'`.
2. Outbound 443 must be allowed in the security group, and the subnet must route `0.0.0.0/0` to the internet gateway.
3. The node needs its public IPv4: there is no NAT gateway.

### `Cannot perform start session: EOF` in a script

`session-manager-plugin` needs a terminal. Without one, its input closes at once and the session ends, sometimes before the command has run. Interactive use is not affected. In a script, give it a pseudo-terminal:

```bash
script -qec "aws ssm start-session --target <id> --document-name AWS-StartNonInteractiveCommand --parameters file://params.json" /dev/null
```

The `AWS-StartNonInteractiveCommand` document runs the command without a shell, so wrap pipelines in `bash -c '...'`.

### `kubectl`: connection refused on `127.0.0.1:16443`

The tunnel isn't running. Start `make cloud-tunnel` in another terminal and leave it open. If it says the target is not connected, see the SSM agent section above.

### Closing the tunnel

Press Ctrl-C in the tunnel's terminal: that ends the session on the AWS side too. Killing the plugin another way (closing the terminal, `kill`) can leave the session listed as active. It costs nothing, but to tidy up:

```bash
aws ssm describe-sessions --state Active --query 'Sessions[].[SessionId,Target]' --output text
aws ssm terminate-session --session-id <id>
```

### `make cloud-config` reports the host UNREACHABLE

SSH runs through `aws ssm start-session`, so anything that breaks Session Manager breaks Ansible:

1. `aws sts get-caller-identity --profile devops`: an expired `aws login` shows up as an SSH connection error.
2. The SSM agent must be Online (see above). On a fresh node the playbook waits up to 5 minutes for it.
3. The Session Manager plugin must be installed on the laptop.

`make cloud-config ANSIBLE_ARGS=-vvv` shows the SSH and ProxyCommand output.

### `--check` on a fresh node stops at "Download node_exporter"

Expected. Check mode only pretends to create `/opt/node_exporter`, so the download into it can't run. Check mode is useful on a configured node, where `changed=0` and no diff mean the node matches the code. The waits and the package-list refresh run for real even in check mode; without the refresh, apt on a fresh cloud image (empty package lists) can't find any package.

### `ssh.socket` and `ssh.service` are both enabled on the node

Ubuntu 24.04 starts sshd through `ssh.socket`. The `common` role's "Ensure sshd is running" task, written for the homelab, also enables `ssh.service`. Both are then active and sshd uses the socket's listener; SSH works (checked live). The node is never rebooted, so a possible conflict at boot doesn't arise.

### `make ansible-check` hangs at the start

ansible-lint first installs `community.general` from Ansible Galaxy (`ansible/requirements.yml`) into `ansible/.ansible/collections`. If the Galaxy download stalls, fetch the file with resume, check it against the sha256 that Galaxy publishes for that version, and install the checked file:

```bash
v=8.3.0
url=https://galaxy.ansible.com/api/v3/plugin/ansible/content/published/collections/index/community/general/versions/$v/
curl -fsS "$url" | jq -r '.artifact.sha256'                    # expected checksum
curl -fL -C - -o /tmp/community-general.tar.gz "$(curl -fsS "$url" | jq -r .download_url)"   # repeat until complete
sha256sum /tmp/community-general.tar.gz                         # must match
cd ansible && ansible-galaxy collection install /tmp/community-general.tar.gz -p .ansible/collections
```

### A Spot apply fails

- `InsufficientInstanceCapacity`: no Spot capacity for that type in the AZ. Use on-demand (`make cloud-up` without `CLOUD_ARGS`) or try again later.
- A failed apply can leave a launched instance behind, marked *tainted* in the state. It is still billed, but Terraform knows about it: run `make cloud-down`, then `make cost-check`. If cost-check finds something Terraform does not know about, delete it in the console.

## Secret scanning

```bash
bash ci/gitleaks.sh   # full history of every branch; values are redacted in the output
```

A clean result means no *known pattern* was found, not that there are no secrets: gitleaks' default rules don't match plain `NAME_PASSWORD=value` lines, for example. If a real credential is ever found:

1. Stop and rotate or revoke it first. Rotation makes the leaked value useless; removing it from history needs a force-push and is a separate decision.
2. Record it below with the type, where it was, the dates and what was done. Never record the value.

### Leaked credentials log

| Date found | Type | Where | In history | Action |
|------------|------|-------|------------|--------|
| 2026-11-20 | restic repository password (two values) | `backup/scripts/backup.sh`, `backup/scripts/restore-test.sh` | Added in `7ca581f` (2026-09-16), removed in `8e7665b` (2026-10-19) | Found by manual review after a clean gitleaks scan. The passwords only encrypted local restic repositories of demo data inside k3d clusters that no longer exist, they were not used anywhere else, and the current code generates a random password (`make secrets-init`). No rotation needed; history not rewritten. |

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `curl localhost:8080` returns `404 page not found` | The ingress matches on host name | Add `-H "Host: devops-app.localhost"` |
| `make check` fails on "Docker daemon reachable" | Docker Desktop is stopped, or WSL integration is off | Start Docker Desktop and enable integration for this distro |
| `make infra-up` fails on the bind mount | `~/.ssh/id_ed25519.pub` doesn't exist | `make install` generates the key |
| Ansible reports `Permission denied (publickey)` | The nodes were started with a different key | `make infra-down && make infra-up` to remount the current key |
| `make cluster-up` fails with "port is already allocated" | Something else uses host port 8080 | Stop whatever holds port 8080 |
| `kubectl` talks to the wrong cluster | Current context isn't `k3d-devops` | `make cluster-up` switches to it, or `kubectl config use-context k3d-devops` |
| Rollout times out, pod `Ready: False` | Readiness probe failing | Check `describe pod` events and logs; `/readyz` returns 503 when the database check fails |
| Grafana crash-loops with "data source not found" after upgrading an older install of this repo | Grafana's database still holds a `Prometheus` datasource from older Helm values | Apply a ConfigMap labelled `grafana_datasource: "1"` containing `deleteDatasources: [{name: Prometheus, orgId: 1}]`, restart Grafana, then delete the ConfigMap. Fresh installs don't need this |
| Dashboard panels empty | No traffic yet, or targets down | Send a few requests, wait 30 seconds, check Prometheus targets |
