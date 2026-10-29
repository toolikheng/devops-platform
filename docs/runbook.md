# Runbook

Commands are run from the repo root. `make help` lists every target.

## Prerequisites

- WSL2 Ubuntu with Docker Desktop (WSL integration enabled)
- `k3d`, `kubectl`, `helm`, `ansible`, `hadolint`, `trivy`, `restic` on the WSL `PATH` (`make check` reports what is missing)
- Nothing else is listening on host port 8080, which the k3d load balancer uses

## Local cluster

```bash
make cluster-up      # k3d cluster "devops": 1 server, 2 agents, host port 8080 -> Traefik
make build           # build devops-app:latest and run hadolint and trivy
make deploy          # kubectl apply -k k8s/overlays/dev, then rollout status
kubectl -n devops get pods,svc,ingress
```

Check the app. The ingress matches on Host header:

```bash
curl -H "Host: devops-app.localhost" http://localhost:8080/healthz
curl -H "Host: devops-app.localhost" http://localhost:8080/readyz
```

Tear down:

```bash
make cluster-down
```

## Manual CD on GitHub

1. Open **Actions → CD → Run workflow** on master.
2. The job creates an ephemeral k3d cluster, builds the image, applies `k8s/overlays/dev`, waits for rollout, and checks `/healthz` and `/readyz` inside the pod.
3. The cluster is deleted when the job ends. Nothing persists.

## Scale

Edit `replicas` in the overlay, then re-apply:

```bash
# k8s/overlays/dev/kustomization.yaml -> replicas: count
make deploy
```

Do not scale above one replica with the current SQLite database.

## Roll back

Re-deploy the previous commit, or undo the image change:

```bash
kubectl -n devops rollout undo deployment/devops-app
kubectl -n devops rollout status deployment/devops-app
```

## Logs and state

```bash
kubectl -n devops get pods
kubectl -n devops describe pod -l app=devops-app
kubectl -n devops logs deploy/devops-app --tail=100
```

## Monitoring

```bash
make monitoring-up     # helm upgrade --install kube-prometheus-stack into namespace monitoring
kubectl -n monitoring port-forward svc/kube-prometheus-stack-grafana 3000:80
```

Grafana is at http://localhost:3000. The login is `admin`, and the password is in the `grafana-admin` Secret:

```bash
kubectl -n monitoring get secret grafana-admin -o jsonpath='{.data.admin-password}' | base64 -d; echo
```

Remove the stack with `make monitoring-down`.

## Infrastructure nodes

```bash
make infra-up         # start node1-node3 containers
make provision        # run the Ansible playbook (Linux or WSL only)
make infra-down
```

Ansible on Windows Python fails with `os.get_blocking()`. Run `make provision` from WSL.

## Backup and restore

The CronJob `devops-app-backup` runs daily at 02:00 in namespace `devops`.

```bash
make backup         # create a Job from the CronJob, wait for it, print its log
make restore-test   # write items, back up, wipe /data/app.db, restore, verify the count
```

The restic repository lives on the `devops-backup-repo` PVC (5Gi). Snapshots survive pod restarts, but they are on one node's disk with no off-site copy.

`make restore-test` deletes `/data/app.db` in the `devops` namespace after a successful backup. Run it only against a dev cluster.

The restic password is generated into the `backup-credentials` Secret by `make secrets-init`. Read it with `kubectl -n devops get secret backup-credentials -o jsonpath='{.data.password}' | base64 -d`.

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| Pod in `CrashLoopBackOff`, log shows `No module named 'app'` | A volume mounted at `/app` hides the code in the image | Keep the PVC at `/data` (the current manifests do this) |
| `kubectl` connects to `localhost:8080` and is refused | Current context is not the k3d cluster | `kubectl config get-contexts`, then `kubectl config use-context k3d-devops` |
| `k3d: command not found` in one shell | k3d is installed only in WSL | Run k3d commands from WSL |
| `make cluster-up` fails with "port is already allocated" | Something else uses host port 8080 | Stop the container holding port 8080, or run `make cluster-down` first |
| `failed to find unique target for patch` | Overlay uses `bases`/`patchesStrategicMerge` | Use `resources` and `patches` with a `target` (dev and prod already do) |
| Rollout times out, pod `Ready: False` | Readiness probe failing | Check `describe pod` events and `logs`; `/readyz` returns 503 when the database check fails |
| Grafana dashboards empty | Prometheus has not scraped yet, or targets are down | Wait 30 seconds, then check `http://localhost:9090/targets` after port-forwarding Prometheus |
