# ADR 015: Only PolicyPal's UI is exposed; the API stays inside the cluster

## Status

Accepted

## Context

PolicyPal has two parts in one image: a FastAPI API on port 8000 and a Streamlit UI on port 8501, which calls the API server-side. The API has public endpoints (`/ask`, `/feedback`, the documents list, the Swagger page at `/docs`) and admin endpoints protected only by a shared token in the `X-Admin-Token` header (upload, re-ingest, `/analytics` with every stored question). It has no rate limiting. The Streamlit admin page asks for the same token.

The node's security group already lets in only my /32 on port 80. The question is what Traefik should publish on that port. I want this with no changes to the PolicyPal repository.

| Option | What's reachable on port 80 | Notes |
|---|---|---|
| UI and API both behind the ingress | everything, including Swagger and the admin endpoints | the token is the only protection on uploads and analytics |
| Traefik basicAuth in front of both | everything, after a password | another secret to manage, and the API's own token still applies |
| **UI only; the API is ClusterIP** | the UI; the admin page still needs the token | the API is reached by the UI inside the cluster, and by me with `kubectl port-forward` |
| `ADMIN_TOKEN` empty | the UI; admin endpoints switched off | simplest, but then nobody can upload or re-ingest |

## Decision

- **The Ingress routes every path to `policypal-ui:8501` and nothing else.** There is no default backend, and every Service is `ClusterIP` (on k3s a `LoadBalancer` or `NodePort` Service would open a port on the node). A test on the rendered YAML enforces both.
- **The admin token stays on**, from the `policypal-admin` Secret, so uploads work through the UI's admin page for someone who has the token.
- **The smoke test reaches the API the way I do**, through `kubectl port-forward svc/policypal-api` over the Session Manager tunnel, and checks through the ingress that `/docs` and `/openapi.json` never return the API's pages.
- **On AWS the ingress answers on any host name**: the public IP changes every session and there is no DNS. Locally it uses `policypal.localhost`, because the v1 app shares k3d's Traefik.

## Consequences

- Measured on AWS from my laptop: `/`, `/docs`, `/openapi.json` and `/health` on port 80 all returned Streamlit's page (status 200, Streamlit's own "Page not found" in the browser), never Swagger; connections to 8000, 8501, 5432, 6443 and 22 got no answer (the security group drops them).
- Streamlit answers 200 for unknown paths, so a status-code check would prove nothing; the smoke test looks at the content instead.
- PolicyPal's embeddable chat widget calls the API from the browser, so it can't work on AWS with this setup. The Streamlit UI covers the same questions.
- Anyone who can reach the UI can still ask questions (and fill the question history); only my IP can reach it. A public demo would need written approval to open port 80 more widely plus Traefik basicAuth in front of the UI.
- The first idea for the smoke test, `kubectl create --raw` through the API server's service proxy, failed: kubectl sends no `Content-Type`, so FastAPI rejected the JSON body with a 422. `kubectl port-forward` plus `curl` with the header works, and it is the same path CI can use through a tunnel.
