# Runbook

Commands are run from the repo root in WSL2. `make help` lists every target.

## Prerequisites

- WSL2 Ubuntu with Docker Desktop running (WSL integration on for this distro).
- `make check` must pass. It checks `git`, `ssh`, `curl`, `make`, `jq`, `openssl`, `ansible`, `python3` and its venv module, `docker`, `kubectl`, `k3d`, `helm`, that the Docker daemon is reachable, and that `~/.ssh/id_ed25519` exists. `make install` apt-installs missing packages with sudo, generates the SSH key, and prints install links for Docker, kubectl, k3d and helm.
- hadolint, Trivy, promtool and yq are not needed on the host; they run as pinned Docker images.
- Host port 8080 must be free (the k3d load balancer).
- For the AWS targets: AWS CLI v2.32 or newer, the Session Manager plugin and Terraform 1.11 or newer. `make ansible-check` also needs ansible-lint (`pipx install ansible-lint`). The pre-commit hook needs pre-commit (`pipx install pre-commit==4.7.0`, then `pre-commit install` once per clone).

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

The rules are unit-tested with `bash ci/rules-test.sh` (Docker only); CI runs the same script. It uses the Prometheus image the chart runs; after a chart bump, `tests/test_monitoring_platform.py` fails until `PROMETHEUS_IMAGE` in that script follows.

### Monitoring on AWS

`make cloud-addons` installs the monitoring after External Secrets Operator (`k8s/platform/kube-prometheus-stack/values-aws.yaml`: one day of metrics in memory, no Alertmanager, Prometheus scrapes the node's own node_exporter on 9100). PolicyPal's alert and dashboard ship with its aws overlay, so `make cloud-addons` must run before `make cloud-deploy` (the PrometheusRule needs the chart's CRD). With `make cloud-tunnel` running in another terminal:

```bash
export KUBECONFIG=~/.kube/devops-aws.yaml
kubectl -n monitoring port-forward svc/kube-prometheus-stack-grafana 3000:80      # http://localhost:3000/d/policypal
kubectl -n monitoring get secret kube-prometheus-stack-grafana -o jsonpath='{.data.admin-password}' | base64 -d; echo   # user: admin
kubectl -n monitoring port-forward svc/kube-prometheus-stack-prometheus 9090:9090 # http://localhost:9090/alerts
```

The Grafana password is random, made by the chart in the cluster on its first install; a new node has a new one. The dashboard "PolicyPal on AWS" shows the API's available replicas, the alert, container restarts, node memory with the 75% line ([ADR 024](decisions/024-m7i-flex-node-for-monitoring.md)), and memory and CPU per PolicyPal container. Grafana's dashboard sidecar reads ConfigMaps labelled `grafana_dashboard: "1"` in the `policypal` namespace only.

| Alert | Fires when |
|-------|------------|
| PolicyPalApiUnavailable | `policypal-api` has had no available replica for 2 minutes, or kube-state-metrics has no count for it |

To see it fire: `kubectl -n policypal scale deployment/policypal-api --replicas=0`. On k3d it was pending after 31 s and firing after 3 minutes (the 2 minutes plus the 30-second scrape and evaluation intervals); with the API back, it cleared within a minute. Scale it back with `--replicas=1`.

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
make cost-check              # AWS_PROFILE=devops by default; home region always ap-southeast-1
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

`make tf-check` runs the offline checks (no credentials needed): fmt, validate, `terraform test` with a mock provider, tflint and trivy. Trivy's version, like the node's Kubernetes version, is pinned once in `ci/versions.env` ([ADR 019](decisions/019-shared-tool-versions.md)); a bump is one edit there, and `make test-infra` then names the k3s role or CI input that must follow.

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

Everything billed by the hour lives in `terraform/envs/aws` and exists only during a session. The default node is an m7i-flex.large on Spot: about USD 0.060/h, or USD 0.127/h on-demand (instance, public IPv4 and volume together; [ADR 024](decisions/024-m7i-flex-node-for-monitoring.md)). To change the type of a running node, replace it: `terraform apply -replace=module.compute.aws_instance.node` (a one-time Spot instance can't be stopped, so an in-place change fails), then `make cloud-config`, `cloud-addons` and `cloud-deploy`.

```bash
aws login --profile devops
make cost-check                                  # start from a clean account
make cloud-up                                    # stores your current IP, shows the plan, applies only after you type yes
#   on-demand instead: make cloud-up CLOUD_ARGS='-var use_spot=false'
make cloud-config                                # Ansible over Session Manager: common, node_exporter, k3s (about 2 min)
make cloud-tunnel                                # in a second terminal: localhost:16443 -> the node's 6443; Ctrl-C closes it
KUBECONFIG=~/.kube/devops-aws.yaml kubectl get nodes
make cloud-addons                                # External Secrets Operator, once per new node (see "Secrets" below)
make cloud-deploy                                # PolicyPal: deploy (ESO makes its Secrets), smoke test (see "PolicyPal" below)
aws ssm start-session --target "$(cd terraform/envs/aws && terraform output -raw instance_id)"   # a shell, if needed
# ... work ...
# Ctrl-C in the tunnel's terminal first (see "Closing the tunnel")
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

### The tunnel stops by itself after 20 minutes

`Your session timed out due to inactivity and has been terminated.` Session Manager ends a session after 20 minutes with no traffic (the account's default idle timeout), and kubectl then gets "connection refused" on `127.0.0.1:16443`. Run `make cloud-tunnel` again. Nothing on the node is affected; the app keeps running.

### Closing the tunnel

Press Ctrl-C in the tunnel's terminal **before** `make cloud-down`: that ends the session on the AWS side too. If the node is destroyed first, the close never reaches AWS and the session stays listed as connected to the terminated instance (seen in C4). Killing the plugin another way (closing the terminal, `kill`) can do the same. It costs nothing, but to tidy up:

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

- `InsufficientInstanceCapacity`: no Spot capacity for that type in the AZ. Use on-demand (`make cloud-up CLOUD_ARGS='-var use_spot=false'`) or try again later.
- A failed apply can leave a launched instance behind, marked *tainted* in the state. It is still billed, but Terraform knows about it: run `make cloud-down`, then `make cost-check`. If cost-check finds something Terraform does not know about, delete it in the console.

## PolicyPal

PolicyPal's manifests are in `k8s/policypal/` (base plus `local` and `aws` overlays). Always try a change on k3d first; it costs nothing.

```bash
make k8s-check                 # offline: render both overlays, kubeconform -strict
make policypal-local           # k3d: cluster-up, random local Secrets (once), deploy, smoke test
#   UI: http://policypal.localhost:8080
make cloud-deploy              # AWS, with make cloud-tunnel running and ESO installed: deploy, smoke test
make cloud-smoke               # AWS: smoke test only
#   UI: http://<public_ip from terraform output>/  (only your IP can reach it)
```

The deploy (`scripts/deploy-policypal.sh`) renders the overlay with the same pinned kustomize as the checks, applies it, waits up to 2 minutes for the Secrets `policypal-db` and `policypal-admin` (on AWS the ExternalSecrets in this apply make them; on k3d the script made them before), waits for Postgres, the API and the UI (`ROLLOUT_TIMEOUT`, default 15m, covers the first image pull), then runs the smoke test. If the apply, a rollout or the smoke test fails, every Deployment whose revision this deploy changed is rolled back to the revision it had, and the script exits 1. If one rollback fails, the script says which (`rolling deployment/<name> back failed`) and still rolls back the others; check that one with `kubectl -n policypal rollout history`. Postgres is never rolled back. A first deploy has nothing to roll back to.

To deploy another PolicyPal commit, change `newTag` in the overlay (keep the quotes: a hash of only digits would be a YAML number), run `make k8s-check`, then deploy on k3d first.

Reach the API directly (it has no ingress on purpose):

```bash
kubectl -n policypal port-forward svc/policypal-api 18000:8000    # http://127.0.0.1:18000/docs
kubectl -n policypal get secret policypal-admin -o jsonpath='{.data.ADMIN_TOKEN}' | base64 -d; echo   # for the UI's admin page
```

Measure memory: `kubectl top pods -n policypal --containers`, and the peak since start for one container: `kubectl -n policypal exec deploy/policypal-api -- cat /sys/fs/cgroup/memory.peak`.

### `deploy-policypal: waiting for the Secrets policypal-db policypal-admin failed`

The deploy applied the manifests, but a Secret the pods need didn't appear within 2 minutes. Nothing new was rolled out (the Postgres pod shows `CreateContainerConfigError` meanwhile).

- **On AWS**, External Secrets Operator makes them: `kubectl -n policypal describe externalsecret` shows why it couldn't (see "Secrets" below). Is ESO installed (`make cloud-addons`)?
- **On k3d**, run `scripts/policypal-secrets.sh` (`make policypal-local` does it for you). It never replaces an existing Secret: Postgres keeps the password it was first started with, so a new one would lock the API out. To start over on k3d, `kubectl delete namespace policypal` removes the data and the Secrets together.

Then run the deploy again.

### The smoke test fails

It prints one `PASS` or `FAIL` line per check and runs them all:

- `/health did not answer 200`: the database is unreachable or not migrated. `kubectl -n policypal logs deploy/policypal-api` and `kubectl -n policypal logs statefulset/postgres`.
- `/health` not ok, or policies not loaded: the API's JSON log lines `ingest.document` / `ingest.completed` say which file failed.
- g04 not cited from Leave Policy 6.3, or g41 not refused: the image or `MIN_SIMILARITY` changed. Check the tag in the overlay.
- `UI not healthy through the ingress`: on AWS, your IP may have changed (`make cloud-up` again); locally, the ingress needs the host `policypal.localhost`.
- `kubectl port-forward to the API did not start`: kubectl can't reach the cluster (on AWS: is `make cloud-tunnel` running?) or the `policypal-api` Service is missing; kubectl's own error follows. The smoke test then stops, because without its own forward it can't be sure which API it would be talking to. It lets kubectl pick a free local port each time, so a forward you left open (like the one below) never gets in its way.

### The first deploy on a new cluster is slow, or stops with `exceeded its progress deadline`

A new cluster pulls the PolicyPal image (641 MB compressed) on every node that runs a PolicyPal pod. On k3d the first pull took between 2 minutes and 11 minutes in different runs, depending on the network and on other pulls on the same node. The local-path provisioner's helper pod can queue behind that pull and fail with `create process timeout after 120 seconds`; it retries on its own, so the Postgres volume is just late.

The Deployments allow 15 minutes without progress (`progressDeadlineSeconds: 900`, the same as the deploy script's `ROLLOUT_TIMEOUT`); with the default of 10 minutes a slow pull stopped the deploy. If it still runs out, Kubernetes keeps pulling anyway: wait for the pods (`kubectl -n policypal get pods -w`), then run the deploy again. It has nothing to change and goes straight to the smoke test. For a known-slow network, `ROLLOUT_TIMEOUT=30m` gives the script more time (raise `progressDeadlineSeconds` with it).

### A deploy rolled back

`kubectl -n policypal get pods` and `kubectl -n policypal describe pod <name>` show why (often `ImagePullBackOff`: the tag doesn't exist on GHCR). kubectl warns that `rollout undo` doesn't update the last-applied annotation; the next deploy with a good tag sets it again. While a bad API version was being tried the API was down (`Recreate`); the UI kept serving. `deploy.yml` checks the tag on GHCR before it touches the node, so a rollback there means the image exists but didn't start or failed the smoke test: its run log shows the node's output.

### `statefulset.apps/postgres configured` on every deploy

Harmless. `kubectl apply` replaces `volumeClaimTemplates` as a whole, and the server adds default fields to it, so apply always sends a patch. The StatefulSet's generation doesn't change and Postgres isn't restarted; `kubectl diff -f <(bash scripts/render-k8s.sh aws)` shows nothing.

## Backups on AWS (restic to S3)

`make cloud-config` installs the backup on the node ([ADR 025](decisions/025-restic-backups-from-the-node.md)): restic, `/usr/local/sbin/policypal-backup` and an hourly `policypal-backup.timer`. Each run backs up the Postgres dump, then the uploads, to `s3://toolikheng-devops-platform-backups/restic`, then applies the retention (7 daily, 4 weekly, 12 monthly), prunes and checks. The repository password is read from SSM Parameter Store by restic at run time and is never on disk.

On the node (`aws ssm start-session --target "$(bash scripts/node-id.sh)"`, then `sudo -i`):

```bash
systemctl start policypal-backup                 # a backup now (the timer runs one every hour)
systemctl status policypal-backup                # "failed" if the last run failed
journalctl -u policypal-backup -n 50             # what it did
systemctl list-timers policypal-backup.timer     # when the next one runs
# The snapshots (the same repository and password command as the service):
export RESTIC_REPOSITORY=s3:s3.ap-southeast-1.amazonaws.com/toolikheng-devops-platform-backups/restic
export RESTIC_PASSWORD_COMMAND="/usr/local/sbin/ssm-parameter /policypal/aws/restic-password" AWS_DEFAULT_REGION=ap-southeast-1
restic snapshots
```

A successful backup also writes `policypal_backup_last_success_timestamp_seconds` to node_exporter's textfile directory (`/var/lib/node_exporter/textfile`); the dashboard's "Since the last backup" turns red after 2 hours. A failed one changes nothing there.

### Restore

On the node, as root (the settings are in `/etc/policypal-backup.env`):

```bash
policypal-backup show latest                 # "<run> <database snapshot> <uploads snapshot>"
policypal-backup restore latest              # or a run: policypal-backup restore run-20261125T020000Z
```

A restore stops `policypal-api`, recreates the database and loads the run's dump (`pg_restore`, stopping at the first error), makes the uploads directory exactly the run's (files the backup didn't have are deleted), then starts the API, whose start-up ingestion then agrees with both. If a step fails, the API is left at 0 replicas on purpose (starting it on half-restored data could delete rows): fix the cause, run the restore again, or `kubectl -n policypal scale deployment/policypal-api --replicas=1`.

### Restore test across a rebuild

With `make cloud-tunnel` running:

```bash
make cloud-restore-test-before      # sentinels (a question and a policy), a backup now, the run recorded in ~/.cache/devops-platform/restore-test.env
make cloud-down                     # then a new node with empty data:
make cloud-rebuild && make cloud-addons && make cloud-deploy
make cloud-restore-test-after       # refuses the same node or data that has the sentinels; restores the run; checks
make cloud-smoke
```

`after` passes only if the restore reports the recorded snapshots, the question and the policy are back, and the policy file's sha256 matches the uploaded one. Node commands go through SSM Run Command (no charge).

The scripts were run against k3d first, then on AWS on 2026-11-25: the backup, `restic snapshots`, `show`, and the restore test across a rebuild passed ([test log](aws-test-log.md)).

A failed run leaves the unit failed and does not apply retention, so older snapshots stay. The first run creates the repository; later runs refuse to continue if it can't be opened (a wrong password, a lock held by another run). Restic's `Fatal: repository does not exist` on the very first run is expected: the script then creates it.

## Secrets (SSM Parameter Store and External Secrets Operator)

| Secret | Kept in | Read by | Created by | Rotation |
|---|---|---|---|---|
| `/policypal/aws/db-password` | SSM SecureString, Standard, key `aws/ssm` | ESO → Secret `policypal-db` (`POSTGRES_PASSWORD`) → Postgres, and the API, which builds `DATABASE_URL` from it | Terraform, write-only, one entry of `terraform/persistent/secret-catalog.yaml` ([ADR 023](decisions/023-one-catalog-of-secrets.md)) | `secret_versions["db-password"]`, after `ALTER ROLE` (below) |
| `/policypal/aws/admin-token` | same | ESO → Secret `policypal-admin` (`ADMIN_TOKEN`) → API; me, for the UI's admin page | same | `secret_versions["admin-token"]` (below) |
| `/devops-platform/owner-cidr` | SSM String (not secret) | the ephemeral stack's plan | Terraform; `make owner-ip` sets the value | when my IP changes |
| k3s admin kubeconfig | the node; `~/.kube/devops-aws.yaml` (600) | me | k3s | new cluster every session |
| SSH key | `~/.ssh` | Ansible, through Session Manager | me | — |
| AWS access for CI | nothing stored: GitHub OIDC, one role per job (`terraform/persistent/github_oidc.tf`) | the three AWS workflows | STS, per job, for one hour at most | nothing to rotate |

On k3d there is no AWS identity: `scripts/policypal-secrets.sh` creates random local values instead.

The values are never in Git, the Terraform plan or its state. To check the state (read-only; prints only names and counts):

```bash
make check-state-secrets   # every count must be 0
```

ESO is installed once per new node, before the first deploy: `make cloud-addons` (needs `make cloud-tunnel`). It runs `scripts/addon.sh install external-secrets aws`: the pinned chart from `k8s/platform/external-secrets/chart.env` with `values-aws.yaml`, then the `ClusterSecretStore`. Running it again upgrades in place. Its controller runs on the host network to reach the node's credentials ([ADR 017](decisions/017-imds-hop-limit-and-host-network.md)).

```bash
kubectl get clustersecretstore aws-parameter-store                 # READY True
kubectl -n policypal get externalsecret                            # STATUS SecretSynced
kubectl -n external-secrets logs deploy/external-secrets | tail    # errors, with the AWS message
```

### Add a secret

1. Add an entry to `terraform/persistent/secret-catalog.yaml`: its name under `/policypal/aws/`, a description, and the namespace whose ExternalSecret reads it (leave the namespace out if the node reads it itself).
2. Add its version (1) to `secret_versions` in `terraform/persistent/variables.tf`; Terraform refuses a catalog and a version list that don't match.
3. For a namespace: an ExternalSecret there that reads exactly that key, and a pod that references the Secret. `make test-infra` fails until each catalogued secret is read by exactly one ExternalSecret in its namespace, and the `ClusterSecretStore` serves exactly the catalog's namespaces (today `policypal` only).
4. Plan (one parameter to add, $0), apply, `make check-state-secrets`.

### Rotate the admin token

1. Raise `"admin-token"` in `secret_versions` by 1 in `terraform/persistent/variables.tf` and commit it (the commit is the record; the value never appears). A change to the secret's description in `secret-catalog.yaml` belongs in the same commit: on its own it would make the provider write a new version without a value ([ADR 023](decisions/023-one-catalog-of-secrets.md)).
2. `cd terraform/persistent && terraform plan -out tfplan` shows one in-place change (`value_wo_version`); `terraform apply tfplan`.
3. `make check-state-secrets`: still 0.
4. If the node is running: `kubectl -n policypal annotate externalsecret policypal-admin force-sync="$(date +%s)" --overwrite` (otherwise ESO picks it up within the hour), then `kubectl -n policypal rollout restart deployment/policypal-api`. The API reads the token only at start; until it restarts, the old token still works.

The old value stays in the parameter's history: `aws ssm get-parameter --name /policypal/aws/admin-token:1 --with-decryption`.

### Rotate the database password

Postgres keeps the password it was initialised with, so changing only SSM would lock the API out. Every session starts a new database, so between sessions just raise `"db-password"` in `secret_versions` and apply. On a running node (written down, not yet demonstrated):

1. Raise `"db-password"` in `secret_versions`, commit, plan, apply.
2. Set the new value in Postgres without printing it or putting it in a command line (`printf` is a shell builtin; the SQL goes to `psql` on stdin, and the password is letters and digits only):

   ```bash
   pw="$(aws ssm get-parameter --region ap-southeast-1 --name /policypal/aws/db-password --with-decryption --query Parameter.Value --output text)"
   printf "ALTER ROLE policypal PASSWORD '%s';\n" "$pw" | kubectl -n policypal exec -i statefulset/postgres -- psql -U policypal -d policypal
   unset pw
   ```
3. Force-sync `policypal-db`, then `kubectl -n policypal rollout restart deployment/policypal-api`. Until the restart, new database connections from the old API pod fail.

### The store is not Ready, or an ExternalSecret shows `SecretSyncedError`

- `kubectl get clustersecretstore aws-parameter-store -o yaml` → `status.conditions`. "unable to validate store" on AWS means the controller got no credentials: is it on the host network (`kubectl -n external-secrets get pod -o wide`: its IP must be the node's)? On k3d this is expected: there is no AWS identity.
- The controller's log has the AWS error. `AccessDeniedException` means the parameter is outside `/policypal/aws/` or the node role lacks its `policypal-parameters` policy (persistent stack). `ParameterNotFound`: the persistent stack hasn't been applied.
- "is not allowed from namespace": the store serves only `policypal` (`spec.conditions`).

### `make cloud-addons` fails with "context deadline exceeded" and a Pending controller pod

`kubectl -n external-secrets describe pod` says "didn't have free ports for the requested pod ports": a rolling update tried to start the new controller next to the old one, and on one node both need the same host ports. `values-aws.yaml` sets `strategy: Recreate` to prevent this. If it happens anyway, `kubectl -n external-secrets delete pod <old controller pod>` lets the new one start; the Secrets stay.

## CI/CD on AWS (GitHub Actions with OIDC)

Three workflows reach AWS, each job with short-lived credentials for its own role (`terraform/persistent/github_oidc.tf`, permissions in `github_permissions.tf`). No AWS key is stored in GitHub. The persistent stack, which holds IAM, is applied only from my laptop.

| Workflow | When | Role and environment | What it does |
|---|---|---|---|
| `terraform.yml` | pull request touching `terraform/` | `gha-tf-plan`, no environment | static checks, read-only plan, PR comment with resource addresses and actions only |
| `terraform.yml` | on demand: apply or destroy, instance type, Spot | plan: `gha-tf-plan`; apply: `gha-tf-apply` in `aws-apply` (I approve) | plan and hash in the run summary; after approval it plans again and applies the saved plan only if the hash matches |
| `deploy.yml` | on demand: a PolicyPal commit | `gha-deploy` in `aws-deploy` | renders and validates the `aws` overlay for that commit, then runs the deploy on the node through SSM Run Command; rolls back and fails red if the smoke test fails |
| `nightly-destroy.yml` | 02:17 MYT and on demand | `gha-tf-destroy` in `aws-nightly` | destroys the ephemeral stack unless a skip is set, then `scripts/cost-check.sh` |

The role ARNs are built from the repository secret `AWS_ACCOUNT_ID`, so the account ID is masked in the public logs. The node must already run k3s and External Secrets Operator before `deploy.yml`: `make cloud-config` and `make cloud-addons` from the laptop.

### One-time GitHub settings

After `terraform apply` of the persistent stack (it creates the OIDC provider and the four roles):

```bash
REPO=toolikheng/devops-platform
ME="$(gh api user --jq .id)"

# aws-apply: master only, and every run waits for my approval (I start the runs, so self-review is allowed)
gh api -X PUT "repos/$REPO/environments/aws-apply" --input - <<EOF
{"wait_timer": 0, "prevent_self_review": false, "reviewers": [{"type": "User", "id": $ME}],
 "deployment_branch_policy": {"protected_branches": false, "custom_branch_policies": true}}
EOF
gh api -X POST "repos/$REPO/environments/aws-apply/deployment-branch-policies" -f name=master -f type=branch

# aws-deploy and aws-nightly: master only, no reviewer (the nightly destroy must run unattended)
for env in aws-deploy aws-nightly; do
  gh api -X PUT "repos/$REPO/environments/$env" --input - <<'EOF'
{"deployment_branch_policy": {"protected_branches": false, "custom_branch_policies": true}}
EOF
  gh api -X POST "repos/$REPO/environments/$env/deployment-branch-policies" -f name=master -f type=branch
done

# The account ID goes straight from AWS into the secret, never onto the screen.
aws sts get-caller-identity --profile devops --query Account --output text | gh secret set AWS_ACCOUNT_ID
# Terraform's node needs my SSH public key (Ansible uses it through Session Manager).
gh variable set SSH_PUBLIC_KEY < ~/.ssh/id_ed25519.pub
```

Check:

```bash
gh api "repos/$REPO/environments" --jq '.environments[] | {name, rules: [.protection_rules[].type]}'
gh secret list     # AWS_ACCOUNT_ID only: no AWS keys
gh variable list   # SSH_PUBLIC_KEY (and NIGHTLY_DESTROY_SKIP_UNTIL while a skip is set)
```

### Running the workflows

GitHub only dispatches and schedules workflows that are on `master`, and the three environments accept `master` only.

```bash
gh workflow run terraform.yml -f action=apply -f instance_type=t3.micro -f use_spot=true
gh run watch          # the apply job waits: Actions > the run > Review deployments > aws-apply > Approve
gh workflow run deploy.yml -f policypal_tag=<PolicyPal commit, 40 characters>
gh workflow run nightly-destroy.yml        # the nightly destroy, now

make skip-nightly-destroy                  # hold off the nightly destroy for 8 hours (UNTIL=2026-11-24T00:00:00Z for longer, 7 days max)
gh variable delete NIGHTLY_DESTROY_SKIP_UNTIL   # cancel the skip
```

A skip only stops the nightly job: the stack still bills every hour, so `make cloud-down` stays the way to end a session.

**Working in the morning.** A scheduled destroy can start hours late (07:30 and 11:57 so far) and has no time window on purpose, so it can remove a stack I'm using. Before a session in the hours after 02:17 MYT, I set a skip:

```bash
make skip-nightly-destroy                              # the next 8 hours: one session
make skip-nightly-destroy UNTIL=2026-11-24T06:00:00Z   # anything longer, in UTC (here 14:00 MYT); 7 days at most
```

Eight hours is the default so that a skip set in the morning ends before that night's 02:17 run, and a stack I forgot is still destroyed. A skip set after 18:17 MYT does cover that night's run, which is how to keep a stack overnight on purpose. When the session ends early, `gh variable delete NIGHTLY_DESTROY_SKIP_UNTIL`.

While a run waits at the `aws-apply` gate, `gh run view --log` refuses ("still in progress"). The finished plan job's log is available through the jobs API, so the change list and hash can be read from a terminal before approving:

```bash
run=<run id>
job="$(gh run view "$run" --json jobs --jq '.jobs[] | select(.name | startswith("Plan (")) | .databaseId')"
gh api "repos/toolikheng/devops-platform/actions/jobs/$job/logs" | sed -E 's/^[^ ]+ //' \
  | grep -E '^(create|update|delete|replace) |^No changes|^Hash of these'
```

### `Not authorized to perform sts:AssumeRoleWithWebIdentity`

The job's token subject is not one its role trusts: the job runs in another environment or branch, a pull request comes from a fork (forks get no token at all), the repo's subject format changed, or `AWS_ACCOUNT_ID` is wrong. `tests/test_ci_roles.py` checks every job's subject against the trust policies offline; compare the job's `environment:` with `github_role_subjects` in `terraform/persistent/github_oidc.tf`. Two read-only checks show what GitHub really sends:

```bash
gh api repos/toolikheng/devops-platform/actions/oidc/customization/sub   # immutable subject: owner@id/repo@id
aws cloudtrail lookup-events --region ap-southeast-1 --profile devops \
  --lookup-attributes AttributeKey=EventName,AttributeValue=AssumeRoleWithWebIdentity \
  --query 'Events[].CloudTrailEvent' --output text | jq -r '[.errorCode, .userIdentity.userName] | @tsv'
```

The second prints each refused token's subject; the trust policies use `github_subject_repo` in `github_oidc.tf`.

### `plan-summary: the changes have changed since they were approved`

Between the plan I approved and the apply, something changed what Terraform would do: a laptop apply, another run, or a new owner IP. Nothing was applied. I run the workflow again and review the new plan.

### An apply or destroy fails with `UnauthorizedOperation` or `AccessDenied`

The role lacks a permission the provider needs (a new provider version can call something new). AWS returns an encoded message: `aws sts decode-authorization-message --encoded-message <message> --profile devops` shows the action and resource. Add the action to `github_permissions.tf` and its test, then plan and apply the persistent stack from the laptop. `make cloud-down` from the laptop still works meanwhile.

### `deploy-render: ghcr.io/toolikheng/policypal:<commit> is not on GHCR`

`deploy.yml` stopped before anything reached the node: GHCR has no image with that tag. The input must be PolicyPal's full 40-character commit hash, and PolicyPal's CI must have published an image for it (its Actions run for that commit). "could not check … on GHCR" means GHCR gave no clear answer (an outage, a rate limit): run it again later. Nothing was deployed either way.

### `deploy-over-ssm: expected one running node`

No running instance carries `Project=devops-platform` and `Stack=ephemeral`: create the node first (`terraform.yml` apply or `make cloud-rebuild`), then `make cloud-addons`.

### The nightly destroy didn't run

GitHub can start scheduled runs late or skip them under load, and disables schedules after 60 days without repository activity (`gh workflow enable nightly-destroy.yml`). On the first night (2026-11-23), with the cron at 02:00 MYT, the run started 5 h 30 min late, at 07:30. GitHub's docs name the start of every hour as a busy time, so the cron moved to 02:17 MYT; the next run still started 9 h 40 min late, at 11:57. The nightly destroy is a safety net for a forgotten session; `make cloud-down` at the end of every session is the cleanup. Check with `gh run list --workflow nightly-destroy.yml --event schedule`: no run after 02:17 MYT means it hasn't started *yet*, not that it never will, so don't wait for it if a stack is still up. The budget alerts and `make cost-check` are the backup; `gh workflow run nightly-destroy.yml` or `make cloud-down` destroys now.

## Secret scanning

```bash
bash ci/gitleaks.sh            # full history of every branch; findings shown with values redacted
bash ci/gitleaks.sh --staged   # only what is staged (the pre-commit hook)
pipx install pre-commit==4.7.0 && pre-commit install   # once per clone: the hook scans every commit
```

`.gitleaks.toml` adds one rule to gitleaks' defaults, `plain-password-assignment`: a `NAME_PASSWORD` set to a literal of 8 or more characters, also as a `${NAME:-default}`. The defaults missed exactly that shape in `7ca581f` (below). References (`$VAR`, `${VAR}`, `$(command)`), printf formats and `<placeholders>` are not flagged. `.github/workflows/secret-scan.yml` scans the full history on every pull request, every Monday and on demand. The hook uses the pre-commit hook type only; other hooks in `.git/hooks` are left alone.

A clean result means no *known pattern* was found, not that there are no secrets. When something is found:

- **A false positive** (example text, a test value made at run time): change the text, or narrow the rule with a test that shows the case, as was done when the hook flagged the words "`*_PASSWORD=` assignments" in a comment.
- **A known finding in history that needs no action** goes in `.gitleaksignore` (its fingerprint, `commit:file:rule:line`, with the reason in a comment). A test fails if anything other than the two listed below is in it, so adding one means changing that test too, on purpose.
- **A real credential:**
  1. Stop and rotate or revoke it first. Rotation makes the leaked value useless; removing it from history needs a force-push and is a separate decision.
  2. Record it below with the type, where it was, the dates and what was done. Never record the value.

### Leaked credentials log

| Date found | Type | Where | In history | Action |
|------------|------|-------|------------|--------|
| 2026-11-20 | restic repository password (two values) | `backup/scripts/backup.sh`, `backup/scripts/restore-test.sh` | Added in `7ca581f` (2026-09-16), removed in `8e7665b` (2026-10-19) | Found by manual review after a clean gitleaks scan. The passwords only encrypted local restic repositories of demo data inside k3d clusters that no longer exist, they were not used anywhere else, and the current code generates a random password (`make secrets-init`). No rotation needed; history not rewritten. Since C5 the `plain-password-assignment` rule finds both lines; their fingerprints are in `.gitleaksignore`. |

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
