# syntax=docker/dockerfile:1
# Portable substrate for the telegram tool (stamped by scripts/new-tool.sh).
# BUILD CONTEXT = repo root, because the tool imports the shared ./security package:
#   docker build -f tools/telegram/Dockerfile -t mcp-telegram .
ARG BASE=public.ecr.aws/amazonlinux/amazonlinux@sha256:336b735f8f0aa1d591802beb01d2ef85c6a4a3f411ea4ffa35cad8ba5db282af

# --- builder: resolve the venv from the hashed lock (tamper-evident install) ----------
FROM ${BASE} AS builder
RUN dnf install -y python3.12 python3.12-pip python3.12-devel gcc tar gzip && dnf clean all
WORKDIR /app
COPY tools/telegram/requirements.lock ./requirements.lock
RUN python3.12 -m venv /app/.venv \
    && /app/.venv/bin/pip install --no-cache-dir --upgrade pip \
    && /app/.venv/bin/pip install --no-cache-dir --require-hashes -r requirements.lock

# --- vendor: the engine, pinned by commit + sha256 (same tamper-evidence as the lock).
# A SOURCE checkout on purpose: the PyPI name 'telegram-mcp' belongs to an UNRELATED
# project (the engine's own install_guard refuses it), and pip's --require-hashes
# can't lock git installs -- so the engine code is pinned here and only its deps go
# through the lock. server.py runs it as a stdio child from this directory.
ARG TG_COMMIT=b2f71f1317e06b5041afa62dfac2d586f647f6f4
ARG TG_SHA256=c4571d0fb97481730babdd57c6422259fc27000406c59ad720a298dac9357e33
RUN curl -fsSL "https://github.com/chigwell/telegram-mcp/archive/${TG_COMMIT}.tar.gz" \
        -o /tmp/telegram-mcp.tar.gz \
    && echo "${TG_SHA256}  /tmp/telegram-mcp.tar.gz" | sha256sum -c - \
    && mkdir -p /app/vendor \
    && tar -xzf /tmp/telegram-mcp.tar.gz -C /app/vendor \
    && mv "/app/vendor/telegram-mcp-${TG_COMMIT}" /app/vendor/telegram-mcp \
    && rm /tmp/telegram-mcp.tar.gz

# --- runtime: slim, non-root ----------------------------------------------------------
FROM ${BASE}
RUN dnf install -y python3.12 procps-ng shadow-utils && dnf clean all \
    && groupadd --system app && useradd --system --gid app --home-dir /app app
WORKDIR /app
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8063 \
    MCP_TRANSPORT=http \
    # Writable state carve-out (OAuth store, caches). HOME points here too so any
    # ~/.cache or ~/.config writes land somewhere the non-root user owns.
    HOME=/app/state \
    FASTMCP_HOME=/app/state

COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/vendor /app/vendor
COPY security/ /app/security/
COPY tools/telegram/ /app/tools/telegram/
RUN mkdir -p /app/state && chown -R app:app /app/state

USER app
WORKDIR /app/tools/telegram
EXPOSE 8063
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os,socket; socket.create_connection(('127.0.0.1', int(os.environ.get('MCP_PORT','8063'))), timeout=3).close()" || exit 1
ENTRYPOINT ["python", "server.py"]
