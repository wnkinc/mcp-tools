# syntax=docker/dockerfile:1
# ---------------------------------------------------------------------------------
# The GUARDRAIL is its own substrate (see docs/SUBSTRATE.md): a separate container so
# the multi-GB ML stack (torch/transformers + the gated PromptGuard model) never leaks
# into a tool image. Tools reach it over the compose network at http://guardrail:8071;
# the tool image stays slim. This mirrors the hardened-host split, where the guardrail
# is its own systemd unit and tools HTTP-call :8071.
#
# BUILD CONTEXT = this dir (self-contained; no shared ./security import):
#   docker build -t mcp-guardrail security/guardrail/service
# ---------------------------------------------------------------------------------
ARG BASE=public.ecr.aws/amazonlinux/amazonlinux@sha256:336b735f8f0aa1d591802beb01d2ef85c6a4a3f411ea4ffa35cad8ba5db282af

# --- builder: uv resolves the venv from the existing uv.lock (--frozen) ---------------
FROM ${BASE} AS builder

RUN dnf install -y python3.12 python3.12-pip && dnf clean all
# Pinned uv installer (this bootstrap tool is the one thing not from uv.lock).
RUN python3.12 -m pip install --no-cache-dir uv==0.11.21

WORKDIR /app
ENV UV_LINK_MODE=copy UV_PYTHON=python3.12
# Lock + project metadata first so the heavy dependency layer caches across code edits.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# --- runtime: slim-ish (still carries torch), non-root, writable model cache ----------
FROM ${BASE}

RUN dnf install -y python3.12 shadow-utils && dnf clean all \
    && groupadd --system app && useradd --system --gid app --home-dir /app app

WORKDIR /app
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    # Bind the compose network, not just loopback, so tool containers can reach us.
    GUARDRAIL_HOST=0.0.0.0 \
    GUARDRAIL_PORT=8071 \
    # HF model cache under a writable, non-root path (mount a volume here to persist /
    # to supply the gated PromptGuard model; otherwise the service runs DEGRADED).
    HF_HOME=/app/.cache/huggingface

COPY --from=builder /app/.venv /app/.venv
COPY service.py ./
RUN mkdir -p /app/.cache/huggingface && chown -R app:app /app/.cache

USER app
EXPOSE 8071

# Readiness: /healthz reports {ready, degraded, scanners}. 200 as soon as a firewall
# (even HiddenASCII-only degraded mode) is built.
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen(f\"http://127.0.0.1:{os.environ.get('GUARDRAIL_PORT','8071')}/healthz\", timeout=3)" || exit 1

ENTRYPOINT ["python", "service.py"]
