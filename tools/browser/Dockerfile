# syntax=docker/dockerfile:1
# Browser tool: python proxy + node engine (@playwright/mcp) + headed Chromium +
# the X/VNC live-view stack. BUILD CONTEXT = repo root (imports ./security):
#   docker build -f tools/browser/Dockerfile -t mcp-browser .
#
# Diverges from the stamped amazonlinux substrate ON PURPOSE: Chromium's runtime
# dependencies are what playwright's own install-deps resolves on Debian, and the
# view stack (xvfb/x11vnc/novnc/websockify/fluxbox) is stock Debian packaging --
# replicating either on amazonlinux is a maintenance trap. Both bases are pinned
# by digest like the substrate image is.
ARG PY_BASE=python:3.12-slim-bookworm@sha256:d50fb7611f86d04a3b0471b46d7557818d88983fc3136726336b2a4c657aa30b
ARG NODE_BASE=node:22-bookworm-slim@sha256:6c74791e557ce11fc957704f6d4fe134a7bc8d6f5ca4403205b2966bd488f6b3

# --- builder: resolve the venv from the hashed lock (tamper-evident install) ----------
FROM ${PY_BASE} AS builder
WORKDIR /app
COPY tools/browser/requirements.lock ./requirements.lock
RUN python -m venv /app/.venv \
    && /app/.venv/bin/pip install --no-cache-dir --upgrade pip \
    && /app/.venv/bin/pip install --no-cache-dir --require-hashes -r requirements.lock

# --- engine: npm-ci the exact-pinned @playwright/mcp + its version-matched Chromium ---
# The lockfile pins the whole tree (tamper-evident like the python lock); the browser
# download uses the SAME playwright-core the lock resolved, so engine and browser
# can't drift apart (a mismatch crash-loops at launch).
FROM ${NODE_BASE} AS engine
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
WORKDIR /app/engine
COPY tools/browser/package.json tools/browser/package-lock.json ./
RUN npm ci --omit=dev --no-audit --no-fund \
    # --no-shell: skip the headless-shell build; the full (headed) Chromium binary
    # serves both modes, and headed is this tool's default (the live view).
    && ./node_modules/.bin/playwright-core install --no-shell chromium

# --- runtime: python base + node binary + chromium deps + view stack, non-root --------
FROM ${PY_BASE}
# The node RUNTIME only (a single static-ish binary) from the pinned node image;
# npm never ships -- the engine tree arrives fully resolved from the stage above.
COPY --from=engine /usr/local/bin/node /usr/local/bin/node
COPY --from=engine /app/engine /app/engine
COPY --from=engine /ms-playwright /ms-playwright
RUN apt-get update && apt-get install -y --no-install-recommends \
        # the live view chain (entrypoint.sh): virtual display -> vnc -> web page
        xvfb x11vnc novnc websockify fluxbox \
        procps \
    # Chromium's runtime libraries, resolved by playwright itself for THIS pinned
    # version (the canonical list; hand-maintaining it goes stale).
    && node /app/engine/node_modules/playwright-core/cli.js install-deps chromium \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system app && useradd --system --gid app --home-dir /app app
WORKDIR /app
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8067 \
    MCP_TRANSPORT=http \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    # Writable state carve-out (OAuth store, the persistent Chromium profile, tool
    # output). HOME points here too so ~/.cache writes land somewhere app owns.
    HOME=/app/state \
    FASTMCP_HOME=/app/state

COPY --from=builder /app/.venv /app/.venv
COPY security/ /app/security/
COPY tools/browser/ /app/tools/browser/
# state/output is its own mount point (the browser-artifacts volume the sync
# sidecar drains); pre-creating it app-owned is what gives the volume app
# ownership on first use.
RUN mkdir -p /app/state/output && chown -R app:app /app/state

USER app
WORKDIR /app/tools/browser
EXPOSE 8067 6080
# GET /healthz (serve()'s liveness route) rather than a bare TCP connect: the probe
# runs in the server's event loop, where it doubles as the sidecar catalog
# registration that puts this tool in the manage panel at startup. start-period is
# generous: first start spawns Xvfb + the node child + Chromium.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('MCP_PORT','8067') + '/healthz', timeout=4)" || exit 1
ENTRYPOINT ["bash", "entrypoint.sh"]
