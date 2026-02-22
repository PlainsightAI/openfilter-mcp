#!/usr/bin/env bash
# Shared Docker build logic for Cloud Build and local testing.
# Dry-run by default — images are built but NOT pushed unless DRY_RUN=false.
#
# Usage:
#   TAG_NAME=v1.5.0 SHORT_SHA=abc123 _DOCKERHUB_REPO=plainsightai/openfilter-mcp \
#     bash scripts/docker-build.sh --dockerfile Dockerfile.slim --tag-suffix "-slim" --latest-tag "latest-slim"
set -euo pipefail

# ---------------------------------------------------------------------------
# Parse arguments
# ---------------------------------------------------------------------------
DOCKERFILE=""
TAG_SUFFIX=""
LATEST_TAG=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dockerfile) DOCKERFILE="$2"; shift 2 ;;
    --tag-suffix) TAG_SUFFIX="$2"; shift 2 ;;
    --latest-tag) LATEST_TAG="$2"; shift 2 ;;
    *) echo "Unknown arg: $1" >&2; exit 1 ;;
  esac
done

if [[ -z "$DOCKERFILE" ]]; then
  echo "Error: --dockerfile is required" >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# Dry-run control (default: true — safe for local runs and gcloud builds submit)
# ---------------------------------------------------------------------------
DRY_RUN="${DRY_RUN:-true}"
if [[ "$DRY_RUN" == "false" ]]; then
  echo "==> LIVE mode: images will be pushed"
  PUSH_FLAG="--push"
else
  echo "==> DRY-RUN mode: images will be built but NOT pushed"
  PUSH_FLAG=""
fi

# ---------------------------------------------------------------------------
# Configure GAR auth (PR and dev builds)
# ---------------------------------------------------------------------------
if [[ -n "${_PR_NUMBER:-}" ]] || [[ "${TAG_NAME:-}" =~ ^v.*-dev$ ]]; then
  if [[ -f /workspace/.gar_token ]]; then
    GAR_TOKEN=$(cat /workspace/.gar_token)
    AUTH_B64=$(echo -n "oauth2accesstoken:${GAR_TOKEN}" | base64 -w0)
    mkdir -p /builder/home/.docker
    printf '{\n  "auths": {\n    "us-west1-docker.pkg.dev": {\n      "auth": "%s"\n    }\n  }\n}\n' \
      "${AUTH_B64}" > /builder/home/.docker/config.json
    echo "GAR auth configured"
  fi
fi

# ---------------------------------------------------------------------------
# Set up QEMU + buildx (each Cloud Build step is a fresh container)
# ---------------------------------------------------------------------------
docker run --privileged multiarch/qemu-user-static --reset -p yes
docker buildx rm multiarch-builder 2>/dev/null || true
DOCKER_CONFIG=/builder/home/.docker docker buildx create --name multiarch-builder \
  --driver docker-container --use
docker buildx inspect --bootstrap

# ---------------------------------------------------------------------------
# Determine tags based on build type
# ---------------------------------------------------------------------------
_GAR_REPO="${_GAR_REPO:-}"
_DOCKERHUB_REPO="${_DOCKERHUB_REPO:-}"
TAGS=""

# --- PR builds → GAR only ---
if [[ -n "${_PR_NUMBER:-}" ]]; then
  echo "PR build #${_PR_NUMBER}: ${DOCKERFILE}"
  TAGS="-t ${_GAR_REPO}:pr-${_PR_NUMBER}${TAG_SUFFIX} -t ${_GAR_REPO}:${SHORT_SHA}${TAG_SUFFIX}"

# --- Dev tag builds (v*-dev) → GAR only ---
elif [[ "${TAG_NAME:-}" =~ ^v.*-dev$ ]]; then
  DEV_VERSION=$(echo "${TAG_NAME}" | sed 's/^v//')
  echo "Dev tag build: ${TAG_NAME} (${DOCKERFILE})"
  TAGS="-t ${_GAR_REPO}:${DEV_VERSION}${TAG_SUFFIX} -t ${_GAR_REPO}:${SHORT_SHA}${TAG_SUFFIX}"

# --- Release / main builds → DockerHub ---
else
  if [[ "$DRY_RUN" == "false" ]]; then
    echo "${DOCKERHUB_TOKEN}" | docker login -u "${DOCKERHUB_USERNAME}" --password-stdin
  fi

  if [[ "${TAG_NAME:-}" =~ ^v ]]; then
    echo "Version tag build: ${TAG_NAME} (${DOCKERFILE})"
    NEW_VERSION=$(echo "${TAG_NAME}" | sed 's/^v//')

    # Fetch current latest version from DockerHub
    LATEST_DIGEST=$(curl -s "https://hub.docker.com/v2/repositories/${_DOCKERHUB_REPO}/tags/${LATEST_TAG}" | \
      jq -r '.images[0].digest // empty')

    CURRENT_VERSION=""
    if [[ -n "${LATEST_DIGEST}" ]]; then
      # Build a jq filter that matches tags with or without the suffix
      if [[ -n "${TAG_SUFFIX}" ]]; then
        JQ_FILTER=".results[] | select(.images[].digest == \$digest) | .name | select(test(\"^v?[0-9]+\\\\.[0-9]+\\\\.[0-9]+${TAG_SUFFIX}$\")) | sub(\"${TAG_SUFFIX}$\"; \"\")"
      else
        JQ_FILTER='.results[] | select(.images[].digest == $digest) | .name | select(test("^v?[0-9]+\\.[0-9]+\\.[0-9]+$"))'
      fi
      CURRENT_VERSION=$(curl -s "https://hub.docker.com/v2/repositories/${_DOCKERHUB_REPO}/tags?page_size=100" | \
        jq -r --arg digest "${LATEST_DIGEST}" "${JQ_FILTER}" | \
        sed 's/^v//' | head -1)
    fi
    CURRENT_VERSION="${CURRENT_VERSION:-0.0.0}"
    echo "Current latest: ${CURRENT_VERSION}, New: ${NEW_VERSION}"

    # Semver compare: true if $1 >= $2
    version_gte() {
      [ "$(printf '%s\n' "$1" "$2" | sort -V | tail -n1)" = "$1" ]
    }

    if version_gte "${NEW_VERSION}" "${CURRENT_VERSION}"; then
      echo "New >= current, tagging as ${LATEST_TAG}"
      TAGS="-t ${_DOCKERHUB_REPO}:${SHORT_SHA}${TAG_SUFFIX} -t ${_DOCKERHUB_REPO}:${LATEST_TAG} -t ${_DOCKERHUB_REPO}:${NEW_VERSION}${TAG_SUFFIX}"
    else
      echo "New < current, skipping ${LATEST_TAG} tag"
      TAGS="-t ${_DOCKERHUB_REPO}:${SHORT_SHA}${TAG_SUFFIX} -t ${_DOCKERHUB_REPO}:${NEW_VERSION}${TAG_SUFFIX}"
    fi
  else
    echo "Main branch build: SHA ${SHORT_SHA} (${DOCKERFILE})"
    TAGS="-t ${_DOCKERHUB_REPO}:${SHORT_SHA}${TAG_SUFFIX}"
  fi
fi

# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------
echo "Building with tags: ${TAGS}"
# shellcheck disable=SC2086
DOCKER_CONFIG=/builder/home/.docker docker buildx build \
  --platform linux/amd64,linux/arm64 \
  ${TAGS} \
  ${PUSH_FLAG} \
  -f "${DOCKERFILE}" .
