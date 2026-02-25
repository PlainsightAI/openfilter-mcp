DOCKERHUB_REPO := plainsightai/openfilter-mcp
PLATFORMS      := linux/amd64,linux/arm64
VERSION        := $(shell git describe --tags --abbrev=0 2>/dev/null || echo dev)
SHA            := $(shell git rev-parse --short HEAD)
GCP_PROJECT    := plainsightai-dev
CLOUDBUILD_SA  := cloudbuild-dev@$(GCP_PROJECT).iam.gserviceaccount.com
PSCTL_TOKEN    := $(shell psctl token path 2>/dev/null || echo $$HOME/.config/plainsight/token)

.PHONY: help test build.slim build.full build.run.slim build.run.full \
        run.slim run.full \
        release.dev release.slim-dev release.prod \
        cloud.slim cloud.full \
        index index.extract smoke

# ─── Help ─────────────────────────────────────────────────────────────────────

help: ## Show this help
	@grep -E '^[a-zA-Z_.-]+:.*##' $(MAKEFILE_LIST) | \
		awk -F ':.*## ' '{printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}'

# ─── Dev ──────────────────────────────────────────────────────────────────────

test: ## Run tests
	uv sync --group dev
	uv run python -m pytest tests/ -v --tb=short

# ─── Build ────────────────────────────────────────────────────────────────────

build.slim: ## Build slim Docker image (no ML deps, ~370MB)
	docker build -f Dockerfile.slim -t $(DOCKERHUB_REPO):slim .

build.full: indexes ## Build full Docker image (with code search indexes)
	docker build -f Dockerfile.gpu -t $(DOCKERHUB_REPO):full .

indexes: ## Ensure indexes exist (extract from published image if missing)
	@test -d indexes || $(MAKE) index.extract

build.run.slim: build.slim ## Build and run slim image (with auth)
	docker run --rm -p 3000:3000 -v "$(PSCTL_TOKEN):/root/.config/plainsight/token:ro" $(DOCKERHUB_REPO):slim

build.run.full: build.full ## Build and run full image (with auth)
	docker run --rm -p 3000:3000 -v "$(PSCTL_TOKEN):/root/.config/plainsight/token:ro" $(DOCKERHUB_REPO):full

run.slim: ## Run published slim image (with auth)
	docker run --rm -p 3000:3000 -v "$(PSCTL_TOKEN):/root/.config/plainsight/token:ro" $(DOCKERHUB_REPO):latest-slim

run.full: ## Run published full image (with auth)
	docker run --rm -p 3000:3000 -v "$(PSCTL_TOKEN):/root/.config/plainsight/token:ro" $(DOCKERHUB_REPO):latest

# ─── Index ────────────────────────────────────────────────────────────────────

index: ## Build code search indexes from source
	uv sync --group code-search
	uv run index

index.extract: ## Extract indexes from published amd64 image
	docker pull --platform linux/amd64 $(DOCKERHUB_REPO):latest
	docker create --platform linux/amd64 --name extract-tmp $(DOCKERHUB_REPO):latest
	docker cp extract-tmp:/app/indexes/ ./
	docker cp extract-tmp:/app/openfilter_repos_clones/ ./
	docker rm extract-tmp

# ─── Cloud Build (manual) ────────────────────────────────────────────────

cloud.slim: ## Submit slim-only Cloud Build (V=0.0.0 for tag)
	@test -n "$(V)" || { echo "Usage: make cloud.slim V=0.0.0"; exit 1; }
	gcloud builds submit \
		--config=cloudbuild.yaml \
		--project=$(GCP_PROJECT) \
		--service-account=projects/$(GCP_PROJECT)/serviceAccounts/$(CLOUDBUILD_SA) \
		--substitutions=TAG_NAME=v$(V)-slim-dev,SHORT_SHA=$(SHA),_DRY_RUN=false,_GCS_BUCKET=$(GCP_PROJECT)-build-artifacts \
		.

cloud.full: ## Submit full Cloud Build (V=0.0.0 for tag)
	@test -n "$(V)" || { echo "Usage: make cloud.full V=0.0.0"; exit 1; }
	gcloud builds submit \
		--config=cloudbuild.yaml \
		--project=$(GCP_PROJECT) \
		--service-account=projects/$(GCP_PROJECT)/serviceAccounts/$(CLOUDBUILD_SA) \
		--substitutions=TAG_NAME=v$(V)-dev,SHORT_SHA=$(SHA),_DRY_RUN=false,_GCS_BUCKET=$(GCP_PROJECT)-build-artifacts \
		.

# ─── Release ──────────────────────────────────────────────────────────────────

release.dev: ## Tag and push a dev build (full + slim → GAR)
	@test -n "$(V)" || { echo "Usage: make release.dev V=0.2.0"; exit 1; }
	git tag v$(V)-dev && git push origin v$(V)-dev

release.slim-dev: ## Tag and push a slim-only dev build (→ GAR)
	@test -n "$(V)" || { echo "Usage: make release.slim-dev V=0.2.0"; exit 1; }
	git tag v$(V)-slim-dev && git push origin v$(V)-slim-dev

release.prod: ## Tag and push a production release (→ Docker Hub)
	@test -n "$(V)" || { echo "Usage: make release.prod V=0.2.0"; exit 1; }
	git tag v$(V) && git push origin v$(V)

# ─── Smoke Test ───────────────────────────────────────────────────────────────

PS_API_URL ?= http://localhost:8080

smoke: ## Smoke-test entity parsing against a running plainsight-api
	@curl -sf $(PS_API_URL)/openapi.json >/dev/null 2>&1 || \
		{ printf "\033[31mError: no plainsight-api at %s\033[0m\n" "$(PS_API_URL)"; \
		  echo "Start one first:"; \
		  echo "  cd ../plainsight-api && eval \"\$$(direnv export bash 2>/dev/null)\" && DB_HOST=127.0.0.1 APP_ENV=prod go run ./cmd/api-server"; \
		  echo "Or override the URL:"; \
		  echo "  make smoke PS_API_URL=http://host:port"; \
		  exit 1; }
	PS_API_URL=$(PS_API_URL) uv run python scripts/smoke_entity_spec.py
