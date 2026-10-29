# Fresh clone test

Goal: show that a new machine can go from `git clone` to a running app using only the repo's documentation.

## Steps

Run in WSL, in an empty directory:

```bash
git clone https://github.com/toolikheng/devops-platform.git fresh
cd fresh
make check            # all required tools present
make test             # 9 passed
make lint             # ruff and mypy clean
make build            # image builds; hadolint and trivy report
make cluster-up       # k3d cluster starts
make deploy           # rollout succeeds
curl -H "Host: devops-app.localhost" http://localhost:8080/healthz
curl -H "Host: devops-app.localhost" http://localhost:8080/readyz
make cluster-down     # cleanup
```

## Expected results

| Step | Expected |
|------|----------|
| `make test` | 9 passed |
| `make lint` | `ruff` and `mypy` report no issues |
| `make build` | `devops-app:latest` built |
| `make deploy` | `deployment "devops-app" successfully rolled out` |
| `/healthz` | `{"status":"ok"}` |
| `/readyz` | `{"status":"ready","dependencies":{"database":"connected"}}` |

## Result log

| Date | Machine | Result | Notes |
|------|---------|--------|-------|
| _not yet run_ | | | |

Record each run here, including failures and the workaround.
