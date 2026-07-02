# syntax=docker/dockerfile:1
# ---------------------------------------------------------------------------------
# Portable substrate for the x-mcp TOOL (three-substrate model; see docs/SUBSTRATE.md).
# This image carries the tool + its write/transport policy so it runs identically on a
# laptop (`docker compose up`) and in the cloud (ECR/Fargate) -- the systemd unit stays
# the hardened-host substrate. Security POSTURE is env-selected at runtime by serve()
# (MCP_AUTH_ENABLED / MCP_REQUIRE_APPROVAL / MCP_UNTRUSTED_OUTPUT), not baked in here.
#
# BUILD CONTEXT = repo root, because the tool imports shared ./security:
#   docker build -f tools/xmcp/Dockerfile -t mcp-xmcp .
# ---------------------------------------------------------------------------------

# Base pinned by digest (Amazon Linux 2023), mirroring the awslabs example. Dependabot
# should bump this hash.
ARG BASE=public.ecr.aws/amazonlinux/amazonlinux@sha256:336b735f8f0aa1d591802beb01d2ef85c6a4a3f411ea4ffa35cad8ba5db282af

# --- builder: resolve the venv from the hashed lock (tamper-evident install) ---------
FROM ${BASE} AS builder

RUN dnf install -y python3.12 python3.12-pip python3.12-devel gcc && dnf clean all

WORKDIR /app

# Only the lock first, so the (slow) dependency layer caches across source changes.
COPY tools/xmcp/requirements.lock ./requirements.lock

# --require-hashes: every wheel must match the OSV-vetted lock or the build fails. The
# lock is fully pinned incl. transitives, so a rebuild reproduces the audited set.
RUN python3.12 -m venv /app/.venv \
    && /app/.venv/bin/pip install --no-cache-dir --upgrade pip \
    && /app/.venv/bin/pip install --no-cache-dir --require-hashes -r requirements.lock

# --- runtime: slim image, non-root, source copied over the prebuilt venv -------------
FROM ${BASE}

RUN dnf install -y python3.12 procps-ng shadow-utils && dnf clean all \
    && groupadd --system app && useradd --system --gid app --home-dir /app app

WORKDIR /app
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    # Container substrate defaults (a compose file / task def overrides as needed):
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8061 \
    MCP_TRANSPORT=http \
    # Writable state carve-out -- the container analog of the systemd StateDirectory.
    # With auth on, FastMCP's OAuth proxy persists client registrations + tokens here;
    # /app itself is root-owned and non-writable to the app user. Mount a volume at this
    # path (see docker-compose.yml) to persist OAuth state across restarts.
    FASTMCP_HOME=/app/state

COPY --from=builder /app/.venv /app/.venv
# The tool imports the shared security/ package at ../../security (parents[2] == /app).
COPY security/ /app/security/
COPY tools/xmcp/ /app/tools/xmcp/

# Writable state dir owned by the non-root user (FASTMCP_HOME points here).
RUN mkdir -p /app/state && chown app:app /app/state

USER app
WORKDIR /app/tools/xmcp
EXPOSE 8061

# Liveness: the MCP HTTP transport listens on MCP_PORT. A TCP connect is the right probe
# -- the MCP endpoint is /mcp and speaks a handshake (a plain GET 404/406s), so "is the
# port accepting connections" is the honest up/down signal. (Guardrail reachability is a
# separate concern owned by the guardrail sidecar's own healthcheck.)
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os,socket; socket.create_connection(('127.0.0.1', int(os.environ.get('MCP_PORT','8061'))), timeout=3).close()" || exit 1

ENTRYPOINT ["python", "server.py"]
