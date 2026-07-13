# syntax=docker/dockerfile:1
# Portable substrate for the data TOOL (see docs/SUBSTRATE.md). Same shape as
# tools/xmcp/Dockerfile; data is a TRUSTED tool. BUILD CONTEXT = repo root:
#   docker build -f tools/data/Dockerfile -t mcp-data .
ARG BASE=public.ecr.aws/amazonlinux/amazonlinux@sha256:336b735f8f0aa1d591802beb01d2ef85c6a4a3f411ea4ffa35cad8ba5db282af

# --- builder: resolve the venv from the hashed lock, then PREBUILD OpenBB ------------
FROM ${BASE} AS builder
RUN dnf install -y python3.12 python3.12-pip python3.12-devel gcc gcc-c++ && dnf clean all
WORKDIR /app
COPY tools/data/requirements.lock ./requirements.lock
RUN python3.12 -m venv /app/.venv \
    && /app/.venv/bin/pip install --no-cache-dir --upgrade pip \
    && /app/.venv/bin/pip install --no-cache-dir --require-hashes -r requirements.lock
# OpenBB assembles a static package on first import; do it now (build time, writable
# site-packages) so runtime can set OPENBB_AUTO_BUILD=false on a read-only rootfs.
RUN OPENBB_AUTO_BUILD=true /app/.venv/bin/python -c "import openbb; openbb.obb; print('openbb prebuilt')"

# --- runtime: slim, non-root ---------------------------------------------------------
FROM ${BASE}
RUN dnf install -y python3.12 procps-ng shadow-utils && dnf clean all \
    && groupadd --system app && useradd --system --gid app --home-dir /app app \
    # Shared group for the lean-data volume (fixed gid: must match the lean image,
    # whose 'app' has a different uid) + a group-writable, setgid mount point.
    && groupadd -g 1500 leandata && usermod -aG leandata app \
    && mkdir -m 2775 /lean-data && chgrp leandata /lean-data
WORKDIR /app
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8062 \
    MCP_TRANSPORT=http \
    # Writable state carve-out. HOME points here so OpenBB's ~/.openbb_platform settings
    # + FastMCP OAuth state land on a writable path; DATA_ROOT is the parquet lake.
    HOME=/app/state \
    FASTMCP_HOME=/app/state \
    DATA_ROOT=/app/state/data \
    # Prebuilt above -> never rebuild at runtime (rootfs is read-only to the app user).
    OPENBB_AUTO_BUILD=false

COPY --from=builder /app/.venv /app/.venv
COPY security/ /app/security/
COPY tools/data/ /app/tools/data/
RUN mkdir -p /app/state/data && chown -R app:app /app/state

USER app
WORKDIR /app/tools/data
EXPOSE 8062
# GET /healthz (serve()'s liveness route) rather than a bare TCP connect: the probe
# runs in the server's event loop, where it doubles as the sidecar catalog
# registration that puts this tool in the manage panel at startup.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('MCP_PORT','8062') + '/healthz', timeout=4)" || exit 1
ENTRYPOINT ["python", "server.py"]
