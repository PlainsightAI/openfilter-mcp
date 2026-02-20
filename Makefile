DOCKERHUB_REPO := plainsightai/openfilter-mcp
PLATFORMS := linux/amd64,linux/arm64

.PHONY: help docker-extract-indexes docker-build docker-push docker-build-push docker-inspect docker-build-slim docker-push-slim

help:
	@echo "Docker build targets:"
	@echo "  make docker-extract-indexes  - Extract indexes from working amd64 image"
	@echo "  make docker-build            - Build multi-arch image locally"
	@echo "  make docker-push             - Build and push multi-arch image"
	@echo "  make docker-inspect          - Inspect image contents per platform"
	@echo "  make docker-build-slim       - Build slim multi-arch image (no code search)"
	@echo "  make docker-push-slim        - Build and push slim multi-arch image"

# Extract indexes and repo clones from a working amd64 image
docker-extract-indexes:
	@echo "Extracting indexes and repo clones from amd64 image..."
	docker pull --platform linux/amd64 $(DOCKERHUB_REPO):latest
	docker create --platform linux/amd64 --name extract-tmp $(DOCKERHUB_REPO):latest
	docker cp extract-tmp:/app/indexes/ ./
	docker cp extract-tmp:/app/openfilter_repos_clones/ ./
	docker rm extract-tmp
	@echo "Done. indexes/ and openfilter_repos_clones/ extracted."

# Build multi-arch image locally (no push)
docker-build:
	@test -d indexes || (echo "Error: indexes/ not found. Run 'make docker-extract-indexes' or 'uv run index' first." && exit 1)
	@test -d openfilter_repos_clones || (echo "Error: openfilter_repos_clones/ not found. Run 'make docker-extract-indexes' or 'uv run index' first." && exit 1)
	docker buildx create --use --name multiarch 2>/dev/null || docker buildx use multiarch
	docker buildx build \
		--platform $(PLATFORMS) \
		--no-cache \
		-t $(DOCKERHUB_REPO):latest \
		-t $(DOCKERHUB_REPO):0.1.0 \
		-f Dockerfile.prebuilt .

# Build and push multi-arch image
docker-push:
	@test -d indexes || (echo "Error: indexes/ not found. Run 'make docker-extract-indexes' or 'uv run index' first." && exit 1)
	@test -d openfilter_repos_clones || (echo "Error: openfilter_repos_clones/ not found. Run 'make docker-extract-indexes' or 'uv run index' first." && exit 1)
	docker buildx create --use --name multiarch 2>/dev/null || docker buildx use multiarch
	docker buildx build \
		--platform $(PLATFORMS) \
		--no-cache \
		-t $(DOCKERHUB_REPO):latest \
		-t $(DOCKERHUB_REPO):0.1.0 \
		--push \
		-f Dockerfile.prebuilt .

# Convenience alias
docker-build-push: docker-push

# Inspect image contents per platform
docker-inspect:
	@echo "=== amd64 ==="
	docker pull --platform linux/amd64 $(DOCKERHUB_REPO):latest
	docker run --platform linux/amd64 --rm $(DOCKERHUB_REPO):latest sh -c "du -sh /app/indexes/ /app/openfilter_repos_clones/ && ls /app/indexes/*/"
	@echo ""
	@echo "=== arm64 (via export) ==="
	docker pull --platform linux/arm64 $(DOCKERHUB_REPO):latest
	docker create --platform linux/arm64 --name arm64-inspect $(DOCKERHUB_REPO):latest
	docker export arm64-inspect | tar -tvf - | grep "app/indexes"
	docker rm arm64-inspect

# Build slim multi-arch image locally (no code search, no indexes)
docker-build-slim:
	docker buildx create --use --name multiarch 2>/dev/null || docker buildx use multiarch
	docker buildx build \
		--platform $(PLATFORMS) \
		--no-cache \
		-t $(DOCKERHUB_REPO):latest-slim \
		-t $(DOCKERHUB_REPO):0.1.0-slim \
		-f Dockerfile.slim .

# Build and push slim multi-arch image
docker-push-slim:
	docker buildx create --use --name multiarch 2>/dev/null || docker buildx use multiarch
	docker buildx build \
		--platform $(PLATFORMS) \
		--no-cache \
		-t $(DOCKERHUB_REPO):latest-slim \
		-t $(DOCKERHUB_REPO):0.1.0-slim \
		--push \
		-f Dockerfile.slim .
