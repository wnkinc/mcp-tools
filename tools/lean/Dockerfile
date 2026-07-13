# syntax=docker/dockerfile:1
# Portable substrate for the lean TOOL. Unlike the slim amazonlinux tools, the BASE
# here IS the pinned QuantConnect Lean engine image: the compiled launcher
# (/Lean/Launcher/bin/Debug), the exact miniconda Python 3.11 the algorithms expect
# (pythonnet interop is baked in), and the sample data (/Lean/Data). We add only the
# MCP wrapper on top, in its own venv so its deps never touch the engine's
# algorithm environment. BUILD CONTEXT = repo root (imports the shared ./security):
#   docker build -f tools/lean/Dockerfile -t mcp-lean .
ARG BASE=quantconnect/lean@sha256:8a74ea513abc3a2f9bce19c0afb44bab79bbd4e52a121bc2306dfc8efbe5c3fd  # quantconnect/lean:17886 (same tag as vendor/Lean), pin by digest

# --- builder: resolve the wrapper venv from the hashed lock (tamper-evident) ----------
FROM ${BASE} AS builder
WORKDIR /app
COPY tools/lean/requirements.lock ./requirements.lock
RUN python -m venv /app/.venv \
    && /app/.venv/bin/pip install --no-cache-dir --upgrade pip \
    && /app/.venv/bin/pip install --no-cache-dir --require-hashes -r requirements.lock

# --- runtime: the engine image, non-root, wrapper venv first on PATH ------------------
FROM ${BASE}
RUN groupadd --system app && useradd --system --gid app --home-dir /app app \
    # Shared group for the lean-data volume (fixed gid: must match the data image,
    # whose 'app' has a different uid) + a group-writable, setgid mount point.
    && groupadd -g 1500 leandata && usermod -aG leandata app \
    && mkdir -m 2775 /lean-data && chgrp leandata /lean-data
WORKDIR /app
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8064 \
    MCP_TRANSPORT=http \
    # Writable state carve-out: backtest jobs/results + FastMCP OAuth store. HOME
    # points here too so anything the engine's Python writes to ~/.cache lands on
    # a path the non-root user owns (/Lean stays read-only).
    HOME=/app/state \
    FASTMCP_HOME=/app/state

COPY --from=builder /app/.venv /app/.venv
COPY security/ /app/security/
COPY tools/lean/ /app/tools/lean/
RUN mkdir -p /app/state && chown -R app:app /app/state

USER app
WORKDIR /app/tools/lean
EXPOSE 8064
# GET /healthz (serve()'s liveness route) rather than a bare TCP connect: the probe
# runs in the server's event loop, where it doubles as the sidecar catalog
# registration that puts this tool in the manage panel at startup.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('MCP_PORT','8064') + '/healthz', timeout=4)" || exit 1
# Overrides the base image's `dotnet QuantConnect.Lean.Launcher.dll` entrypoint: the
# wrapper is the process; it invokes the launcher per backtest.
ENTRYPOINT ["python", "server.py"]
