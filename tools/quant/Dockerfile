# syntax=docker/dockerfile:1
# Portable substrate for the quant TOOL (see docs/SUBSTRATE.md). Same shape as the other
# tools, but on a DEBIAN base (not amazonlinux): nautilus_trader ships only manylinux_2_35
# wheels (glibc >= 2.35), and amazonlinux 2023 has glibc 2.34 -> pip would fall back to a
# (Rust) source build and fail. python:3.12-slim (glibc 2.41) installs the wheel directly.
# BUILD CONTEXT = repo root (imports shared ./security):
#   docker build -f tools/quant/Dockerfile -t mcp-quant .
ARG BASE=python:3.12-slim@sha256:423ed6ab25b1921a477529254bfeeabf5855151dc2c3141699a1bfc852199fbf

# --- builder: resolve the venv from the hashed lock ---------------------------------
FROM ${BASE} AS builder
WORKDIR /app
COPY tools/quant/requirements.lock ./requirements.lock
RUN python -m venv /app/.venv \
    && /app/.venv/bin/pip install --no-cache-dir --upgrade pip \
    && /app/.venv/bin/pip install --no-cache-dir --require-hashes -r requirements.lock

# --- runtime: slim, non-root ---------------------------------------------------------
FROM ${BASE}
RUN groupadd --system app && useradd --system --gid app --home-dir /app --no-create-home app
WORKDIR /app
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8064 \
    MCP_TRANSPORT=http \
    # Writable state carve-out (container analog of the systemd StateDirectory). HOME
    # points here so ~/.config, ~/.cache (matplotlib via vectorbt, etc.) land writable.
    HOME=/app/state \
    FASTMCP_HOME=/app/state \
    # numba JIT cache must land somewhere writable (non-root can't write into the venv).
    NUMBA_CACHE_DIR=/app/state/numba-cache

COPY --from=builder /app/.venv /app/.venv
COPY security/ /app/security/
COPY tools/quant/ /app/tools/quant/
RUN mkdir -p /app/state/numba-cache && chown -R app:app /app/state

USER app
WORKDIR /app/tools/quant
EXPOSE 8064
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os,socket; socket.create_connection(('127.0.0.1', int(os.environ.get('MCP_PORT','8064'))), timeout=3).close()" || exit 1
ENTRYPOINT ["python", "server.py"]
