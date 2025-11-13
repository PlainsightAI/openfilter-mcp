FROM nvidia/cuda:13.0.1-devel-ubuntu22.04 AS indexer

RUN apt-get update && apt-get install -y \
    git \
    && rm -rf /var/lib/apt/lists/*

# Disable strict host key checking for container
RUN mkdir -p /root/.ssh && \
    echo "Host github.com\n\tStrictHostKeyChecking no\n\tUserKnownHostsFile /dev/null" > /root/.ssh/config

COPY --from=ghcr.io/astral-sh/uv:0.8.19 /uv /uvx /bin/
WORKDIR /app

# Copy project files
COPY pyproject.toml uv.lock ./
COPY src/ src/
COPY README.md ./

RUN --mount=type=ssh uv sync

# Use CUDA stub libraries for build-time GPU code execution
RUN ln -s /usr/local/cuda/lib64/stubs/libcuda.so /usr/local/cuda/lib64/stubs/libcuda.so.1
ENV LD_LIBRARY_PATH=/usr/local/cuda/lib64/stubs:/usr/local/cuda/lib64:$LD_LIBRARY_PATH
RUN uv run index

FROM python:3.12-slim AS server
COPY --from=indexer /bin/uv /bin/uvx /bin/

RUN apt-get update && apt-get install -y \
    git \
    curl \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Disable strict host key checking for container
RUN mkdir -p /root/.ssh && \
    echo "Host github.com\n\tStrictHostKeyChecking no\n\tUserKnownHostsFile /dev/null" > /root/.ssh/config

# Set working directory
WORKDIR /app

# Copy project files
COPY pyproject.toml uv.lock ./
COPY src/ src/
COPY README.md ./

# Copy the built index from the indexer stage
COPY --from=indexer /app/indexes/ indexes/

# Install dependencies (CPU-only for serving)
RUN --mount=type=ssh uv sync

CMD ["uv", "run", "serve"]
