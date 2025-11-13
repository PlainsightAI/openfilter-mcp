FROM nvidia/cuda:13.0.1-devel-ubuntu22.04 AS indexer

RUN apt-get update && apt-get install -y \
    git \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.8.19 /uv /uvx /bin/
WORKDIR /app

# Copy project files
COPY pyproject.toml uv.lock ./
COPY src/ src/
COPY README.md ./

RUN uv sync
RUN uv run index

FROM python:3.12-slim AS server
COPY --from=indexer /bin/uv /bin/uvx /bin/

RUN apt-get update && apt-get install -y \
    git \
    curl \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy project files
COPY pyproject.toml uv.lock ./
COPY src/ src/
COPY README.md ./

# Copy the built index from the indexer stage
COPY --from=indexer /app/indexes/ indexes/

# Install dependencies (CPU-only for serving)
RUN uv sync

CMD ["uv", "run", "serve"]
