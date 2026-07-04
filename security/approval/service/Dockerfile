# syntax=docker/dockerfile:1
# ---------------------------------------------------------------------------------
# The APPROVAL sidecar is its own substrate, like the guardrail: one container owns
# ALL pending approvals + the Slack secrets, because Slack delivers every button
# click to a single app-level URL -- so approvals need a single owner or only one
# tool's buttons work. Tools reach it at http://approval:8072 over the compose
# network; the tunnel routes approval.<MCP_DOMAIN> to its public pages.
#
# BUILD CONTEXT = this dir (self-contained; no shared ./security import):
#   docker build -t mcp-approval security/approval/service
# ---------------------------------------------------------------------------------
ARG BASE=public.ecr.aws/amazonlinux/amazonlinux@sha256:336b735f8f0aa1d591802beb01d2ef85c6a4a3f411ea4ffa35cad8ba5db282af

# --- builder: resolve the venv from the hashed lock (tamper-evident install) ----------
FROM ${BASE} AS builder
RUN dnf install -y python3.12 python3.12-pip && dnf clean all
WORKDIR /app
COPY requirements.lock ./requirements.lock
RUN python3.12 -m venv /app/.venv \
    && /app/.venv/bin/pip install --no-cache-dir --upgrade pip \
    && /app/.venv/bin/pip install --no-cache-dir --require-hashes -r requirements.lock

# --- runtime: slim, non-root ----------------------------------------------------------
FROM ${BASE}
RUN dnf install -y python3.12 shadow-utils && dnf clean all \
    && groupadd --system app && useradd --system --gid app --home-dir /app app
WORKDIR /app
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    APPROVAL_HOST=0.0.0.0 \
    APPROVAL_PORT=8072 \
    HOME=/app/state

COPY --from=builder /app/.venv /app/.venv
COPY service.py /app/service.py
RUN mkdir -p /app/state && chown -R app:app /app/state

USER app
EXPOSE 8072
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('APPROVAL_PORT','8072')+'/healthz',timeout=3)" || exit 1
ENTRYPOINT ["python", "service.py"]
