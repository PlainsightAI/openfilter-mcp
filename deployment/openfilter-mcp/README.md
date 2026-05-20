# openfilter-mcp Helm chart

Helm chart for the **managed slim** openfilter-mcp deployment — platform
tools only, no code-search, no embedding-model PVC, no GPU. ArgoCD renders
this chart directly from the repo; there is no published chart artifact.

## Layout

```
deployment/openfilter-mcp/
├── Chart.yaml            # chart metadata (appVersion pinned)
├── values.yaml           # inert defaults — real values live in overrides/
├── templates/            # Deployment, Service, Ingress, ServiceAccount, PDB
└── overrides/
    ├── development.yaml
    ├── staging.yaml
    └── production.yaml
```

## How it deploys

`.github/workflows/deployments.yaml` ("Deployments") runs on every merge to
`main`:

1. **build** — `PlainsightAI/gh-actions/publish-docker-image` builds the slim
   image (`Dockerfile.slim`) and pushes it to GAR
   (`us-west1-docker.pkg.dev/plainsightai-prod/oci/openfilter-mcp`), tagged
   with the commit SHA.
2. **deploy** — `PlainsightAI/gh-actions/.github/workflows/deploy-service.yaml`
   rolls the new SHA through `development` → `staging` → `production`. Each
   step rewrites `image.tag` in `overrides/<env>.yaml` (keyed off the
   `$imagepolicy` annotation) and commits it back to `main`; ArgoCD then syncs.
   `production` pauses for manual approval from the Machine Learning team —
   that gate is config-as-code in `.github/environments/`, self-applied by
   `.github/workflows/apply-environments.yaml`.

The chart is consumed by the `openfilter-mcp` ApplicationSet in
`PlainsightAI/gitops`, which points at this directory.

`push` and `pull_request` events that touch only `deployment/**` are ignored
so the deploy-bump commits don't retrigger the workflow.

## Editing values

Per-environment values go in `overrides/<env>.yaml`. `values.yaml` holds only
inert defaults — anything left at `change.me` fails Argo sync rather than
shipping a wrong value. Lint every override before pushing:

```bash
make helm.lint
```
