# Setup runbook — x-mcp as the first Claude-connected tool

Goal: `https://xmcp.secure-agentic-engineering.com/mcp` reachable from Claude desktop,
web, and mobile, gated by "Sign in with Google" locked to your account.

Prereqs on this box: Docker + compose, a Cloudflare Tunnel (id + `credentials-file` in
`~/.cloudflared/`), domain on Cloudflare, an X app bearer token.

---

## 1. Google OAuth client  (you — ~5–10 min)

In the [Google Cloud Console](https://console.cloud.google.com/):

1. **Create / pick a project** (e.g. `mcp-tools`).
2. **OAuth consent screen:** User type **External**; app name + your emails; scopes
   `openid` + `.../auth/userinfo.email` (no sensitive scopes → no review). Add every
   allowed email as a **Test user**; leave status **Testing**.
3. **Credentials → Create OAuth client ID:** type **Web application**; redirect URI
   `https://xmcp.secure-agentic-engineering.com/auth/callback`. Copy the **Client ID**
   and **Client secret**.

(One OAuth client covers all tools; each new subdomain just adds another redirect URI.)

## 2. Fill secrets  (you)

```bash
cp tools/xmcp/env.example tools/xmcp/.env   # .env is gitignored
```
Set in `tools/xmcp/.env`: `X_BEARER_TOKEN`, `X_API_TOOL_ALLOWLIST` (e.g.
`getUsersByUsername,searchPostsRecent`), `XAI_API_KEY` (only for `grok_x_search`),
`MCP_AUTH_ENABLED=1`, `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` (step 1), and
`MCP_ALLOWED_GOOGLE_EMAILS` (your Google email; also a Test user above).

Stage the tunnel credentials for the ingress sidecar (gitignored):
```bash
mkdir -p security/ingress/secrets
cp ~/.cloudflared/<TUNNEL_ID>.json security/ingress/secrets/creds.json
```
Confirm the route for this host in `security/ingress/cloudflared.config.yml`
(`xmcp.secure-agentic-engineering.com → http://xmcp:8061`).

## 3. Bring up the public stack

Only one tunnel connector may run — stop any host one first:
```bash
systemctl --user disable --now cloudflared-openclaw   # if it exists
X_BEARER_TOKEN=... docker compose -f docker-compose.yml -f docker-compose.tunnel.yml up -d --build
```
This starts the tools + guardrail + egress wall + the Cloudflare ingress, with auth
**on** (the overlay). Watch it:
```bash
docker compose ps
docker compose logs -f xmcp        # expect: "OAuth enabled (Google) at https://xmcp..."
```
(Local, auth-off dev instead: `X_BEARER_TOKEN=... docker compose up --build`.)

## 4. Verify the public endpoint (the #410 check)

```bash
curl -s https://xmcp.secure-agentic-engineering.com/.well-known/oauth-authorization-server | head -c 300; echo
curl -s https://xmcp.secure-agentic-engineering.com/.well-known/oauth-protected-resource/mcp; echo
# 401 MUST carry WWW-Authenticate with resource_metadata=... :
curl -sD - -o /dev/null https://xmcp.secure-agentic-engineering.com/mcp | grep -i www-authenticate
```
The last line must print `WWW-Authenticate: Bearer ... resource_metadata=...`.

## 5. Add the custom connector in Claude

Settings → Connectors → Add custom connector →
`https://xmcp.secure-agentic-engineering.com/mcp` → Connect → Google login. Works on
**desktop** and **claude.ai web**; **mobile** inherits it. Then ask Claude to run
`searchPostsRecent` or `grok_x_search`.

---

## Troubleshooting

- **"Connection issue / server configuration issue" with repeated `invalid_token`** —
  Claude is holding an OAuth token from a *previous* instance of this server (e.g. after
  moving from systemd to the container, whose OAuth store is a fresh `xmcp-state`
  volume). Remove+re-add the connector is **not** enough: **fully quit and restart the
  Claude app**, then re-add the connector so it re-registers.
- **"Authorization failed" on web/mobile before any login** — the `WWW-Authenticate`
  header is missing. Re-run step 4; ensure no Cloudflare Access policy fronts `xmcp.*`
  (that reintroduces #410).
- **Google login succeeds but Claude is rejected** — your email isn't in
  `MCP_ALLOWED_GOOGLE_EMAILS`. Check `docker compose logs xmcp` for "Rejected Google login".
- **"Access blocked: app not verified"** — add the email as a **Test user** on the
  consent screen (Testing mode allows only those).
- **A real host is blocked** (Google login / `grok_x_search` fail) — the egress wall is
  denying it. Watch `docker compose exec egress tail -f /var/log/squid/access.log`
  (look for `TCP_DENIED`), add the host to `security/egress-proxy/allowlist/x-mcp.txt`,
  and `docker compose restart egress`.
- **Logs:** `docker compose logs -f xmcp` (or `guardrail` / `egress` / `cloudflared`).

Hardened home-box (systemd) path instead of containers: see
[SUBSTRATE.md](SUBSTRATE.md) and `sudo scripts/install-system.sh`.
