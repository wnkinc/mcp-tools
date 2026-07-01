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
ARG BASE=public.ecr.aws/amazonlinux/amazonlinux@sha256:a450a74bfebfa936e7106d79c8b4b4dd0ca891c790513f84624da02a0e5531db

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
    MCP_TRANSPORT=http

COPY --from=builder /app/.venv /app/.venv
# The tool imports the shared security/ package at ../../security (parents[2] == /app).
COPY security/ /app/security/
COPY tools/xmcp/ /app/tools/xmcp/

USER app
WORKDIR /app/tools/xmcp
EXPOSE 8061

# Liveness: the MCP HTTP transport answers on MCP_PORT. (Guardrail reachability is a
# separate concern owned by the guardrail sidecar's own healthcheck.)
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen(f\"http://127.0.0.1:{os.environ.get('MCP_PORT','8061')}/\", timeout=3)" || exit 1

ENTRYPOINT ["python", "server.py"]
