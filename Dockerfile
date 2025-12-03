# syntax=docker/dockerfile:1.4-labs
FROM python:3.12-slim AS indexer

RUN apt-get update && apt-get install -y \
    git \
    gcc \
    g++ \
    cmake \
    && rm -rf /var/lib/apt/lists/*

# Disable strict host key checking for container
RUN mkdir -p /root/.ssh && \
    echo "Host github.com\n\tStrictHostKeyChecking no\n\tUserKnownHostsFile /dev/null" > /root/.ssh/config

COPY --from=ghcr.io/astral-sh/uv:0.9.6 /uv /uvx /bin/
WORKDIR /app

# Copy project files
COPY pyproject.toml ./
COPY src/ src/
COPY README.md ./
COPY code-context/ code-context/

# Build with CPU-only dependencies
# Use CPU-only pre-built wheels from the llama-cpp-python-cpu index
# Falls back to building from source with CMAKE_ARGS if wheel not available
ENV CMAKE_ARGS="-DGGML_CUDA=off"
RUN uv sync --locked --index llama-cpp-python-cpu || uv sync --index llama-cpp-python-cpu
RUN uv run index

FROM python:3.12-slim AS server
COPY --from=indexer /bin/uv /bin/uvx /bin/

RUN apt-get update && apt-get install -y \
    git \
    curl \
    gcc \
    g++ \
    && rm -rf /var/lib/apt/lists/*

# Disable strict host key checking for container
RUN mkdir -p /root/.ssh && \
    echo "Host github.com\n\tStrictHostKeyChecking no\n\tUserKnownHostsFile /dev/null" > /root/.ssh/config

# Set working directory
WORKDIR /app

# Copy project files
COPY pyproject.toml ./
COPY src/ src/
COPY README.md ./
COPY code-context/ code-context/

# Copy the built index from the indexer stage
COPY --from=indexer /app/indexes/ indexes/

# Install dependencies (CPU-only for serving)
# Use CPU-only pre-built wheels from the llama-cpp-python-cpu index
ENV CMAKE_ARGS="-DGGML_CUDA=off"
RUN uv sync --locked --index llama-cpp-python-cpu || uv sync --index llama-cpp-python-cpu

CMD ["uv", "run", "python", "-m", "openfilter_mcp.server"]
