# Fresh clone test

Goal: show that a machine with the toolchain installed can go from `git clone` to the whole platform running, using only the Makefile.

## Steps

Run in WSL2, with Docker Desktop running and nothing left from earlier runs:

```bash
make destroy                                             # in any existing checkout: remove the cluster and nodes
git clone https://github.com/toolikheng/devops-platform.git /tmp/fresh-test
cd /tmp/fresh-test
make all
```

Then check the result:

```bash
kubectl get pods -A                                      # everything Running and Ready
curl -H "Host: devops-app.localhost" http://localhost:8080/healthz
curl -H "Host: devops-app.localhost" http://localhost:8080/readyz
make provision                                           # second run: changed=0
```

## Expected results

| Step | Expected |
|------|----------|
| `make check` | every check passes |
| `make infra-up` | node1-node3 started and SSH-ready |
| `make provision` | `failed=0` on all three nodes |
| `make test` | `app/.venv` created, 13 passed |
| `make build` | image built, hadolint clean, no fixable HIGH/CRITICAL vulnerabilities |
| `make cluster-up` | cluster created, nodes Ready |
| `make deploy` | `deployment "devops-app" successfully rolled out` |
| `make monitoring-up` | chart deployed, ServiceMonitor, alert rules and dashboard applied |
| `/healthz` | `{"status":"ok"}` |
| `/readyz` | `{"status":"ready","dependencies":{"database":"connected"}}` |
| second `make provision` | `changed=0` on all three nodes |

## Result log

| Date | Machine | Result | Notes |
|------|---------|--------|-------|
| 2026-10-30 | WSL2 Ubuntu 24.04, 8 CPU, 11 GB RAM, Docker Desktop | Pass, first attempt, 3m26s | All steps above passed. Afterwards: all pods Ready (Grafana 0 restarts), app reachable through the ingress, Prometheus targets for the app and all three nodes UP, four alert rules loaded and inactive, Grafana datasource healthy, second provision `changed=0`. Docker's layer cache and the Trivy database cache were warm, so a new machine will take longer. `make install` was not needed. |

Record each run here, including failures and the workaround.
