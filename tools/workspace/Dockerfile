# syntax=docker/dockerfile:1
# Portable substrate for the workspace tool (stamped by scripts/new-tool.sh).
# BUILD CONTEXT = repo root, because the tool imports the shared ./security package:
#   docker build -f tools/workspace/Dockerfile -t mcp-workspace .
ARG BASE=public.ecr.aws/amazonlinux/amazonlinux@sha256:336b735f8f0aa1d591802beb01d2ef85c6a4a3f411ea4ffa35cad8ba5db282af

# --- builder: resolve the venv from the hashed lock (tamper-evident install) ----------
FROM ${BASE} AS builder
RUN dnf install -y python3.12 python3.12-pip python3.12-devel gcc && dnf clean all
WORKDIR /app
COPY tools/workspace/requirements.lock ./requirements.lock
RUN python3.12 -m venv /app/.venv \
    && /app/.venv/bin/pip install --no-cache-dir --upgrade pip \
    && /app/.venv/bin/pip install --no-cache-dir --require-hashes -r requirements.lock

# --- vendor: the engine, pinned by commit + sha256 (same tamper-evidence as the lock).
# A SOURCE checkout on purpose (taylorwilsdon/google_workspace_mcp): the engine's
# FastMCP instance is imported NATIVELY by server.py -- no code modified, pip's
# --require-hashes can't lock git installs, so the code pins here and only its deps
# go through the lock.
RUN dnf install -y tar gzip && dnf clean all
ARG GWS_COMMIT=3f19c7d4a2654238e5fed1b767174fac5615555b
ARG GWS_SHA256=a12448483d1697aef75547029cddb3c20623e447c66428a7e28524be78e32573
RUN curl -fsSL "https://github.com/taylorwilsdon/google_workspace_mcp/archive/${GWS_COMMIT}.tar.gz" \
        -o /tmp/gws.tar.gz \
    && echo "${GWS_SHA256}  /tmp/gws.tar.gz" | sha256sum -c - \
    && mkdir -p /app/vendor \
    && tar -xzf /tmp/gws.tar.gz -C /app/vendor \
    && mv "/app/vendor/google_workspace_mcp-${GWS_COMMIT}" /app/vendor/google_workspace_mcp \
    && rm /tmp/gws.tar.gz

# --- runtime: slim, non-root ----------------------------------------------------------
FROM ${BASE}
RUN dnf install -y python3.12 procps-ng shadow-utils && dnf clean all \
    && groupadd --system app && useradd --system --gid app --home-dir /app app
WORKDIR /app
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8066 \
    MCP_TRANSPORT=http \
    # Writable state carve-out (OAuth store, caches). HOME points here too so any
    # ~/.cache or ~/.config writes land somewhere the non-root user owns.
    HOME=/app/state \
    FASTMCP_HOME=/app/state

COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/vendor /app/vendor
COPY security/ /app/security/
COPY tools/workspace/ /app/tools/workspace/
RUN mkdir -p /app/state && chown -R app:app /app/state

USER app
WORKDIR /app/tools/workspace
EXPOSE 8066
# GET /healthz (serve()'s liveness route) rather than a bare TCP connect: the probe
# runs in the server's event loop, where it doubles as the sidecar catalog
# registration that puts this tool in the manage panel at startup.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('MCP_PORT','8066') + '/healthz', timeout=4)" || exit 1
ENTRYPOINT ["python", "server.py"]
