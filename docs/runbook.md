# Runbook

Commands are run from the repo root in WSL2. `make help` lists every target.

## Prerequisites

- WSL2 Ubuntu with Docker Desktop running (WSL integration on for this distro).
- `make check` must pass. It checks `git`, `ssh`, `curl`, `make`, `jq`, `openssl`, `ansible`, `python3` and its venv module, `docker`, `kubectl`, `k3d`, `helm`, that the Docker daemon is reachable, and that `~/.ssh/id_ed25519` exists. `make install` apt-installs missing packages with sudo, generates the SSH key, and prints install links for Docker, kubectl, k3d and helm.
- hadolint, Trivy, promtool and yq are not needed on the host; they run as pinned Docker images.
- Host port 8080 must be free (the k3d load balancer).

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

The nodes mount `~/.ssh/id_ed25519.pub` and install it as root's `authorized_keys` at startup. node_exporter runs from a SysV init script on these nodes and restarts with the container. On a host with systemd the role installs a systemd unit instead; that path has not been run yet.

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
