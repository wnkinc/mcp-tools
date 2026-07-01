# Substrates: one tool, many deploy targets

This repo separates an MCP tool into **three planes**, and only the top one is Python.
The single rule that lets one tool ride every target: **it never hardcodes a writable
path or a transport — both come from env.** Everything else is a consequence.

```
┌─ TOOL ─────────────── in-repo Python, portable ────────────────┐
│  tools/<name>/server.py — the FastMCP object. Knows nothing    │
│  about how it runs or what it may touch; reads paths/secrets/   │
│  transport from ENV.                                            │
├─ RUNTIME SELECTOR ─── in-repo, ~1 function ────────────────────┤
│  security/serve.py — at startup picks TRANSPORT (http|stdio)    │
│  and the SECURITY POSTURE (auth / approval / guardrail) from    │
│  ENV. One entrypoint, no per-target code fork.                  │
├─ SUBSTRATE ────────── mostly NOT Python; declarative artifacts ─┤
│  "what runs, as whom, what's writable, what egress." Several,   │
│  one per target — each enforces the sandbox in its own idiom.   │
└─────────────────────────────────────────────────────────────────┘
```

## The substrate matrix

| | Desktop-native | Container (compose / cloud) | Hardened host |
|---|---|---|---|
| **Artifact** | `pyproject`/uvx entry | `Dockerfile` + `docker-compose.yml` | `systemd/*.service` + `scripts/install-system.sh` + `squid.conf` |
| **Enforces "what's writable / egress"** | the OS user (weakest) | image: `USER app`, read-only rootfs + tmpfs, network policy | systemd `ReadWritePaths`/`StateDirectory`/`ProtectSystem` + squid egress wall + kernel `IPAddressDeny` |
| **Transport** (`MCP_TRANSPORT`) | `stdio` | `http` | `http` |
| **Inbound auth** (`MCP_AUTH_ENABLED`) | off (you own the machine) | off locally / on in cloud | on (Google OAuth behind Cloudflare Tunnel) |
| **Trust model** | you own the box | portable, medium | your fleet, strongest |

Same tool. Three native answers to "what may be written where" — not duplication; each
platform sandboxing in its own idiom (systemd `StateDirectory` ≈ a container's read-only
rootfs + tmpfs ≈ a desktop barely at all).

The **container is the bridge** between "cloud" and "local desktop": one image runs on a
laptop (`docker compose up`) and in the cloud (ECR/Fargate) carrying its write-policy with
it. The systemd hardening is deliberately host-bound and does not travel.

## Security posture is env, not code

`serve()` applies the shared layers, but *whether each runs* is env-selected, so the same
image is safe-by-default on a laptop and fully guarded on the public tunnel. The per-tool
`serve(...)` call sets the **default**; a substrate may flip it (tri-state: unset → keep the
default, so silence never weakens a tool).

| Layer | Env var | Default | Desktop | Compose (local) | Hardened / cloud |
|---|---|---|---|---|---|
| Inbound OAuth | `MCP_AUTH_ENABLED` | off | off | off | **on** + `MCP_ALLOWED_GOOGLE_EMAILS` |
| Human approval | `MCP_REQUIRE_APPROVAL` | per-tool arg | off | off | **on** |
| Output guardrail | `MCP_UNTRUSTED_OUTPUT` | per-tool arg | off | **on** | **on** |
| Guardrail endpoint | `GUARDRAIL_URL` | `127.0.0.1:8071` | — | `http://guardrail:8071` (sidecar) | `:8071` systemd unit |

> `MCP_UNTRUSTED_OUTPUT=on` is a **promise the substrate must honor**: `GUARDRAIL_URL` has
> to resolve to a running screener, or every tool call fails **closed** at the middleware.
> That is why the guardrail is its own substrate (below), shipped alongside the tool.

## The guardrail is its own substrate

The guardrail screener carries a multi-GB ML stack (torch/transformers + the gated
PromptGuard model). It must never leak into a tool image, so it is a **separate unit** that
tools reach over HTTP:

- **Hardened host:** its own systemd unit on `:8071`; tools HTTP-call loopback.
- **Container:** its own image (`security/guardrail/service/Dockerfile`) run as a **compose
  sidecar** named `guardrail`; tools reach `http://guardrail:8071` over the private network.
- Without an HF token + model cache it runs **DEGRADED** (HiddenASCII-only) and says so on
  `/healthz` — still fail-closed, just without the ML classifier.

This keeps the tool image slim (x-mcp: ~300 MB, ML-free) regardless of substrate.

## Running each

```bash
# Container — the "two substrates" picture (tool + guardrail sidecar):
X_BEARER_TOKEN=... docker compose up --build

# Desktop — local stdio, no auth/guardrail:
MCP_TRANSPORT=stdio MCP_AUTH_ENABLED=0 MCP_UNTRUSTED_OUTPUT=0 \
  tools/xmcp/.venv/bin/python tools/xmcp/server.py

# Hardened host — the systemd substrate (unchanged; defaults preserve prod):
sudo scripts/install-system.sh   # units set MCP_AUTH_ENABLED=1, egress via squid, etc.
```
