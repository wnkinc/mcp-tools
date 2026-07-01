# Architecture

## Request path (the deployed container stack)

```
Claude desktop / claude.ai web / mobile     (custom connector, private to your account)
        │  HTTPS
        ▼
xmcp.secure-agentic-engineering.com          Cloudflare edge — TLS, hides home IP, WAF
        │  Cloudflare Tunnel (cloudflared sidecar, outbound-only; NO Access policy)
        ▼
xmcp container :8061                          FastMCP server on an internal Docker network
        │  FastMCP owns OAuth: "Sign in with Google", locked to an email allowlist
        │  no internet of its own; output screened by the guardrail sidecar (:8071)
        ▼
egress sidecar (squid) :3128                  per-tool domain allowlist, default-deny, audit log
        ▼
api.x.com (read-only bearer, allowlisted ops) + api.x.ai (grok_x_search) + Google OAuth verify
```

The same image runs locally (`docker compose up`) and in the cloud — transport
(`http`/`stdio`) and the security posture (auth / approval / guardrail) are read from
**env**, not baked in, so nothing forks per environment.

## Why each choice

- **Auth in the MCP server, not Cloudflare Access.** claude.ai web/mobile connectors
  require a spec-compliant OAuth 2.1 flow whose `401` carries a `WWW-Authenticate:
  Bearer resource_metadata="..."` header (RFC 9728 / MCP auth spec). Cloudflare
  Access's Managed-OAuth MCP portal omits that header
  ([anthropics/claude-ai-mcp#410](https://github.com/anthropics/claude-ai-mcp/issues/410),
  closed "not planned"), so web/mobile fail there while Claude Code tolerates it.
  FastMCP's `OAuthProxy` emits the header + discovery metadata + DCR, so all surfaces
  work.

- **Tunnel = transport only.** The Cloudflare Tunnel provides TLS, hides the home IP,
  and exposes no inbound ports. The MCP hostname has **no Access policy** — stacking
  Access OAuth on top of MCP OAuth double-auths and breaks the connector.

- **One tool per container, own subdomain, isolated.** Each tool is its own image on
  an `internal` network — a bug or bad dep in one can't reach another's credentials or
  egress. The obvious "one endpoint, all tools" alternative is Cloudflare's MCP Portal,
  which is the thing broken by #410.

- **No internet except through the egress allowlist (the strongest single control).**
  Each tool sits on an `internal` Docker network with no gateway, so the squid sidecar
  is its only route off-box; squid enforces a per-tool domain allowlist (default-deny)
  and is the central egress audit log. Verified: a proxy-ignoring connection has no
  route out, while allowlisted hosts succeed and others get `TCP_DENIED/403`.

- **Google OAuth with a verified-email allowlist, fail-closed.** `GoogleProvider`
  authenticates *any* Google account; `security/auth.py` wraps its token verifier to
  reject any login whose verified email is not in `MCP_ALLOWED_GOOGLE_EMAILS`, and
  refuses to start if auth is enabled without an allowlist/credentials. While the Google
  consent screen is in "Testing", only added test-user emails can complete the login.

## Adding a tool

`scripts/new-tool.sh <name> <port>` stamps `tools/<name>/` (server stub wired to
`security/serve.py`, `env.example`) + its egress allowlist. Then add a `Dockerfile`
(copy an existing tool's) + a hashed `requirements.lock`, a service in
`docker-compose.yml`, a route in `security/ingress/cloudflared.config.yml`, one redirect
URI on the shared Google OAuth client, and the custom connector in Claude.
