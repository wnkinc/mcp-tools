---
name: verify
description: Run this repo's MCP servers + approval sidecar locally and drive them over real MCP HTTP to verify a change end-to-end.
---

# Verify an mcp-tools change at the MCP surface

The surface is MCP over HTTP: a tool server composed by `security/serve.py`, talking
to the approval sidecar. Drive it with a `fastmcp.Client`, not unit tests.

## Environment

```bash
uv venv /tmp/verify-env && VIRTUAL_ENV=/tmp/verify-env uv pip install "fastmcp==3.4.2" uvicorn pynacl
PY=/tmp/verify-env/bin/python
```

(`fastmcp==3.4.2` matches the tools' locks; `pynacl` is for the sidecar's Discord path import.)

## Launch (from the repo root; pick free ports)

```bash
APPROVAL_PUBLIC_URL=http://127.0.0.1:18072 APPROVAL_PORT=18072 $PY security/approval/service/service.py &
APPROVAL_URL=http://127.0.0.1:18072 APPROVAL_PUBLIC_URL=http://127.0.0.1:18072 \
  APPROVAL_WIDGET=1 MCP_PORT=18065 $PY tools/gatekeeper/server.py &
curl -s http://127.0.0.1:18072/healthz   # sidecar up; provider slack/unconfigured is fine locally
```

- Auth is off by default locally (`MCP_AUTH_ENABLED` unset); no OAuth needed.
- The real telegram server needs live Telegram credentials + the vendored stdio engine.
  For changes in the shared layers (serve/middleware/gating), stand in a minimal
  `FastMCP(name="telegram")` with one write + one read tool and call
  `serve(mcp, port=..., require_approval=True)` with `MCP_APPROVAL_EXEMPT=<read tool>` —
  same composition path, same source name.

## Drive

`fastmcp.Client("http://127.0.0.1:<port>/mcp")` → `list_tools()` / `call_tool()`.

- Gated call round-trip without a chat channel: run servers with
  `APPROVAL_WIDGET=1`; the pending text carries
  `<!--APPROVAL {"token": ...}-->` — parse the token and
  `POST {sidecar}/approve/{token}` with form `decision=approve` (what the human's
  browser does), then re-call the tool with the same args.
- Gating/mode overrides: tool servers cache the sidecar's `/gating` for 15s — sleep
  16s after flipping a mode before asserting list/call behavior.

## Gotchas

- A gated tool's FIRST call short-circuits before FastMCP argument validation, so
  bad args still mint an approval card; validation errors only surface on the
  post-approval re-call.
- Approvals are one-shot: each approved call consumes its record; the next identical
  call mints a fresh card.
