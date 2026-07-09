# Local deployment runbook

Goal: `https://<tool>.example.com/mcp` reachable from Claude desktop, web, and
mobile — served from your own Linux box, gated by "Sign in with Google" locked
to your account. `example.com` stands for your domain throughout; you set it
once as `MCP_DOMAIN` in the root `.env`.

Two local modes, same base file:

- **Dev (auth off, no tunnel):** `docker compose up --build` — for hacking on a
  tool. Skips everything below except steps 1–2.
- **Public (auth on, tunnel):** the rest of this runbook; both compose files.

Prereqs on the box: Docker + compose v2.20+, git, and (for step 3) the
[Pulumi CLI](https://www.pulumi.com/docs/install/).

## 1. Clone and pick your tools

```bash
git clone https://github.com/wnkinc/claude-custom-connector-server.git mcp-tools
cd mcp-tools
cp env.example .env
```

In `.env`, set `COMPOSE_PROFILES` to your tools plus the guardrail, e.g.
`xmcp,data,guardrail`. Every profile is opt-in; only listed services build and
run.

## 2. Guardrail (output screen)

Default provider is `llamafirewall` — a local model, no cloud account involved.

1. On [huggingface.co](https://huggingface.co), request access to the gated
   model `meta-llama/Llama-Prompt-Guard-2-86M` (Meta usually grants in
   minutes–hours) and create a read token.
2. Put the token in `.env` as `HF_TOKEN=hf_...`. First start pulls the model
   through the egress wall into a persistent volume; until access is granted
   the guardrail runs degraded (HiddenASCII-only) and says so on its
   `/healthz`.

Turning the screen **off** instead: remove `guardrail` from `COMPOSE_PROFILES`
**and** set `GUARDRAIL_ENABLED=0` — the pair matters, because untrusted tools
with screening on and the service absent withhold all results (fail closed).

## 3. Cloudflare: domain, tunnel, DNS (the shared Pulumi stack)

The tunnel and the wildcard DNS record are one Pulumi stack
([deploy/cloudflare/](../../deploy/cloudflare/)), shared by every deployment
path — create it once and it follows you if this deployment later moves to the
cloud (same domain, tunnel, and credentials; only the host changes).

1. Domain on Cloudflare (any plan). From the zone's **Overview** page grab the
   **Zone ID** and **Account ID**.
2. Dashboard → My Profile → **API Tokens** → create a token with
   **Cloudflare Tunnel:Edit** + **DNS:Edit** on that zone.
3. Create the stack:
   ```bash
   cd deploy/cloudflare
   python3 -m venv venv && venv/bin/pip install -r requirements.txt
   pulumi login --local            # state stays on this box as a file
   pulumi stack init prod          # pick a passphrase (PULUMI_CONFIG_PASSPHRASE)
   pulumi config set cloudflareAccountId <account-id>
   pulumi config set cloudflareZoneId <zone-id>
   export CLOUDFLARE_API_TOKEN=<token>
   pulumi up
   ```
4. Hand the outputs to the compose stack:
   ```bash
   mkdir -p ../../security/ingress/secrets
   pulumi stack output credsJson --show-secrets > ../../security/ingress/secrets/creds.json
   pulumi stack output tunnelId    # -> TUNNEL_ID in the root .env
   ```
   Set `TUNNEL_ID=<that id>` and `MCP_DOMAIN=example.com` in the root `.env`.

The wildcard record covers every current and future tool subdomain with zero
per-tool DNS steps, and adds no exposure: DNS does no security work in this
stack — the committed tunnel overlay is the allowlist of what's actually
served, and cloudflared answers 404 for any hostname without a route.
(Cloudflare wildcards cover one label: `xmcp.example.com`, never
`a.b.example.com`.)

## 4. Google OAuth client (~5–10 min)

In the [Google Cloud Console](https://console.cloud.google.com/):

1. **Create / pick a project** (e.g. `mcp-tools`).
2. **OAuth consent screen:** User type **External**; app name + your emails;
   scopes `openid` + `.../auth/userinfo.email` (default scopes; Google
   verification review is skipped). Add every allowed email as a **Test
   user**; leave status **Testing**.
3. **Credentials → Create OAuth client ID:** type **Web application**; one
   redirect URI per tool you enabled:
   `https://<tool>.example.com/auth/callback` (e.g.
   `https://xmcp.example.com/auth/callback`). Copy the **Client ID** and
   **Client secret**.

One OAuth client covers all tools; each new tool just adds another redirect
URI.

## 5. Approvals (Slack required)

The approval sidecar always runs, and Slack is the only channel that reaches
you: a gated tool call reports a plain pending status in chat while an
Approve/Deny card lands in Slack. (No link goes to the chat — a tool result
carrying an approval URL reads as prompt injection and gets flagged or refused.)
Without Slack configured, gated calls report the approval as undeliverable.

```bash
cp security/approval/service/env.example security/approval/service/.env
```

and follow the Slack-app steps inside that file — including pointing the app's
Interactivity Request URL at `https://approval.example.com/slack/interact`
(once, ever). Prefer Discord? Same file: follow its Discord-app steps and set
`APPROVAL_PROVIDER=discord` (its Interactions Endpoint URL must be saved
*after* the sidecar is up — Discord validates it immediately). Whichever you
pick, use a platform your agent doesn't operate — approval is human-in-the-loop,
and a card the agent's own tools can read and click defeats the purpose.

To run a deploy without approvals instead, opt out explicitly with
`MCP_REQUIRE_APPROVAL=0` in the root `.env` — write actions on the gated tools
then run ungated.

## 6. Per-tool secrets

For each tool you enabled:

```bash
cp tools/<tool>/env.example tools/<tool>/.env
```

Fill in the tool's own values (each `env.example` documents them) plus the
auth pair from step 4: `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET`, and
`MCP_ALLOWED_GOOGLE_EMAILS=<your email>` (also a Test user in step 4).
`MCP_AUTH_ENABLED=1` is already the default.

## 7. Bring up the public stack

Only one connector may run for a tunnel — stop any other `cloudflared` for this
tunnel first:

```bash
docker compose -f docker-compose.yml -f docker-compose.tunnel.yml up -d --build
```

This starts the tools + guardrail + egress wall + approval sidecar + the
Cloudflare ingress, with auth **on** (the overlay). Watch it:

```bash
docker compose ps
docker compose logs -f xmcp        # expect: "OAuth enabled (Google) at https://xmcp..."
```

## 8. Verify a public endpoint

```bash
curl -s https://xmcp.example.com/.well-known/oauth-authorization-server | head -c 300; echo
curl -s https://xmcp.example.com/.well-known/oauth-protected-resource/mcp; echo
# 401 MUST carry WWW-Authenticate with resource_metadata=... :
curl -sD - -o /dev/null https://xmcp.example.com/mcp | grep -i www-authenticate
```

The last line must print `WWW-Authenticate: Bearer ... resource_metadata=...`.

## 9. Add the connectors in Claude

Settings → Connectors → Add custom connector → `https://<tool>.example.com/mcp`
→ Connect → Google login. Works on **desktop** and **claude.ai web**;
**mobile** inherits it.

---

## Troubleshooting

- **"Connection issue / server configuration issue" with repeated
  `invalid_token`** — Claude is holding an OAuth token from a *previous*
  instance of this server (the OAuth store is the tool's state volume; a fresh
  volume invalidates old tokens). Fix: **fully quit and restart the Claude
  app**, then re-add the connector so it re-registers.
- **"Authorization failed" on web/mobile before any login** — the
  `WWW-Authenticate` header is missing. Re-run step 8, and keep the hostname
  serving its own OAuth (leave Cloudflare Access off the tool subdomains).
- **Google login succeeds but Claude is rejected** — add your email to
  `MCP_ALLOWED_GOOGLE_EMAILS`. Check `docker compose logs <tool>` for
  "Rejected Google login".
- **"Access blocked" (app unverified)** — add the email as a **Test user** on
  the consent screen (Testing mode allows added Test users).
- **A real host is blocked** (Google login, an API, the HF model pull fail) —
  the egress wall is denying it. Watch
  `docker compose exec egress tail -f /var/log/squid/access.log` (look for
  `TCP_DENIED`), add the host to that service's file in
  `security/egress-proxy/allowlist/`, and `docker compose restart egress`.
- **Guardrail stuck degraded** — `docker compose exec` a tool container and
  `curl http://guardrail:8071/healthz`; `degraded: true` with an `HF_TOKEN`
  set usually means model access is still pending on huggingface.co.
- **Logs:** `docker compose logs -f <tool>` (or `guardrail` / `egress` /
  `approval` / `cloudflared`).
