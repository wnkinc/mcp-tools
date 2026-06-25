# Vendored: xmcp (official X API MCP server)

Read-only X (Twitter) search/lookup as MCP tools, for the DeerFlow agent.

- **Upstream:** https://github.com/xdevplatform/xmcp (X's official dev platform)
- **Vendored commit:** `63d34362d88ed9f94d54ccd5ecd5bb4d12e11759`
- **Vendored on:** 2026-06-23
- **License:** see upstream `LICENSE`
- **Vendored AND patched** (like `tools/trading-agents/`). Upstream forces an
  **interactive browser OAuth1 flow on every startup** (`create_mcp →
  build_oauth1_client → run_oauth1_flow → webbrowser.open`), so it can't run as a
  headless service. Our patch in `server.py::create_mcp` adds an **app-only Bearer**
  path (the upstream `get_auth_headers` bearer helper, previously unused): when
  `X_OAUTH_CONSUMER_KEY`/`SECRET` are absent it skips OAuth1 and signs requests with
  a static `Authorization: Bearer <X_BEARER_TOKEN>` header — read-only, no browser,
  no act-as-account. The original OAuth1 path is preserved when consumer keys are set.
  Patch sites are marked `# PATCHED (secure-agentic-engineering)`.

## Gotcha

`main()` reads `MCP_PORT` from the process env **before** `.env` is loaded, so set
`MCP_PORT` in the **systemd unit `Environment=`** (or shell), not just in `.env`.

## Grok x_search tool (xAI) — PATCHED

In addition to the raw X-API tools, a custom **`grok_x_search`** tool (patched into
`server.py::create_mcp` via `mcp.add_tool`) calls **xAI's Responses API** with the
`x_search` tool — Grok searches X and returns a **cited natural-language summary**
(vs raw post objects). Two search styles, one server:
`searchPostsRecent` (raw) vs `grok_x_search` (Grok-summarized).

- **Credential:** `XAI_API_KEY` in `.env` (the preserved key from the old Grok
  x-search). Read-only search; no account access.
- **🔧 MODEL LEVER:** **`XAI_MODEL` in `.env`** selects the Grok model. Default
  `grok-4-1-fast` (non-reasoning, cheaper/faster); set a `*-reasoning` model for
  deeper synthesis. (Change one line in `.env`, restart the service.)
- **Egress:** xMCP now also reaches **`api.x.ai`** (Grok) alongside `api.x.com`.
- **L4 detect:** free — the `GuardrailMiddleware` screens *all* tool results, so
  Grok answers are screened too.

## Hardened systemd service

`~/.config/systemd/user/xmcp.service` runs the server (enabled + linger-backed →
reboot-proof). Hardening uses the **namespace/seccomp subset that works in a
`--user` unit**: `ProtectSystem=strict`, `ProtectHome=read-only`, `PrivateTmp`,
`RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX`, `RestrictNamespaces`,
`RestrictRealtime`, `RestrictSUIDSGID`, `LockPersonality`, `NoNewPrivileges`,
`SystemCallFilter=@system-service`.

> **User-unit caveat:** capability-dropping directives fail with `218/CAPABILITIES`
> in `--user` units (the user manager can't drop caps; the process has none anyway).
> Omitted: `CapabilityBoundingSet`, `PrivateDevices`, `ProtectClock`,
> `ProtectHostname`, `ProtectKernel{Tunables,Modules,Logs}`, `ProtectControlGroups`.

**L5 deps vetted** (OSV, 2026-06-23): fastmcp 3.4.2, httpx 0.28.1, mcp 1.28.0,
oauthlib 3.3.1, requests-oauthlib 2.0.0, python-dotenv 1.2.2, starlette 1.3.1,
uvicorn 0.49.0, xai-sdk 1.17.0, xdk 0.9.0 — all clean.

**L2 egress (refinement):** code-scoped to `api.x.com`; a domain-level egress
allowlist would need a sidecar proxy (Cloudflare-fronted → IP allowlists brittle).

## Wired into DeerFlow (2a.3)

`deerflow/extensions_config.json` (gitignored — may hold secrets later; reproduce
manually):

```json
{ "mcpServers": { "xmcp": { "enabled": true, "type": "http",
    "url": "http://127.0.0.1:8051/mcp",
    "description": "X read-only search/lookup (app-only bearer; 8 allowlisted read tools)." } },
  "skills": { "x-research": { "enabled": true } } }
```

The DeerFlow gateway hot-reloads this via mtime (no restart needed). The
**x-research skill** (`deerflow/skills/custom/x-research/SKILL.md`, force-tracked)
tells the agent when/how to use the X tools and frames returned posts as untrusted
data. Verified end-to-end: the DeerFlow lead agent calls `searchPostsRecent` →
`:8051` → X API → real posts.

## Content screening — guardrail middleware (L4 detect)

xMCP's returned X content is screened through the guardrail service (`:8041`)
**before** it reaches the model. Unlike the SAE instance (which screens via
DeerFlow's `mcpInterceptors` hook), this public instance serves Claude directly, so
screening is a **FastMCP middleware** — `shared/guardrail.py::GuardrailMiddleware`,
registered in `server.py::create_mcp`:

- It screens the RESULT of **every** tool call (all of them return untrusted X
  content): **allow** → wrapped in `<untrusted_x_content source="xmcp"
  trust="UNTRUSTED">`; **block / HITL / guardrail-down** → WITHHELD (fail closed).
  Error and empty results pass through. Drops `structured_content` so the model
  sees only the screened text.
- **Order:** added *after* `ApprovalMiddleware`, so it sits INSIDE the approval gate
  (FastMCP wraps `reversed(middleware)` → first-added is outermost). It only screens
  results of calls the human already approved; a pending-approval message is never
  screened. Toggle with `GUARDRAIL_ENABLED`; point at the service via `GUARDRAIL_URL`.
- ⚠️ Guardrail is still **degraded** (HiddenASCII only) until the PromptGuard HF
  gated-model grant — so plaintext-injection detection isn't active yet.

> **L4 isolate (not yet ported):** the SAE instance also has the deterministic
> backstop — a tool-deprived `x-researcher` subagent (DeerFlow). On this Claude-facing
> instance there is no subagent layer; today L4 rests on detect (degraded) + the
> approval gate. Carrying an isolation equivalent over is tracked separately.

## What it is

A FastMCP server that fetches X's OpenAPI spec (`api.x.com/2/openapi.json`) and
exposes its operations as MCP tools. The full spec is **165 operations: 97 read
(GET) and 68 write/mutate (POST/DELETE/PUT)** — post/delete tweets, DMs, follow,
block, like, etc.

## Security posture (THREAT-MODEL)

- **L1 — minimize grant (server-side):** read-only is **code-enforced**, not just
  a `.env` convention. The allowlist defaults to a hardcoded 8-op read set
  (`DEFAULT_READ_ALLOWLIST`) when `X_API_TOOL_ALLOWLIST` is blank — a missing/typo'd
  `.env` fails **closed to read-only**, not open to all 165 ops. A write-guard
  drops every non-GET op unless `X_API_ALLOW_WRITES=1`, so **all write ops never
  exist** as tools regardless of the allowlist. Read-only **bearer token** only;
  OAuth1 user flow left empty (no act-as-account). Enforced at the source.
- **L2/L3:** loopback-bound (`127.0.0.1:8051`); only reaches `api.x.com`; runs in
  its own venv under a hardened systemd unit (2a.2).
- **L4/L5:** content screening + allowed-tools isolation on the DeerFlow side
  (2a.4/2a.5); deps vetted via OSV/deps.dev (2a.2).

## Run

```bash
uv pip install --python .venv/bin/python -r requirements.txt   # done
# secrets live in .env (gitignored, 600): X_BEARER_TOKEN + the read-only allowlist
.venv/bin/python server.py    # serves MCP on http://127.0.0.1:8051/mcp
```

`xai-sdk` / `xdk` are pulled by upstream's requirements (the optional Grok test
client); the MCP server itself only needs the X bearer token to serve read tools.
