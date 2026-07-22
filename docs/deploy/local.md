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

In `.env`, set `COMPOSE_PROFILES` to your tools, e.g. `browser,telegram`. That's the
only deploy-time choice: only listed tools build and run, and the substrate
(egress wall, approval sidecar, gatekeeper, and — whenever an untrusted-output
tool is listed — the guardrail) comes up on its own.

## 2. Guardrail (output screen)

Default provider is `llamafirewall` — a local model, no cloud account involved.

1. On [huggingface.co](https://huggingface.co), request access to the gated
   model `meta-llama/Llama-Prompt-Guard-2-86M` (Meta usually grants in
   minutes–hours) and create a read token.
2. Put the token in `.env` as `HF_TOKEN=hf_...`. First start pulls the model
   through the egress wall into a persistent volume; until access is granted
   the guardrail runs degraded (HiddenASCII-only) and says so on its
   `/healthz`.

Running the untrusted tools **unscreened** instead (not recommended): set
`GUARDRAIL_ENABLED=0`. With screening on and the screen broken, those tools
fail closed — results are withheld with a message naming the cause (container
not running, model still downloading on first start, or a missing
HF_TOKEN/BEDROCK_GUARDRAIL_ID in the container logs).

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
(Cloudflare wildcards cover one label: `telegram.example.com`, never
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
   `https://telegram.example.com/auth/callback`). Copy the **Client ID** and
   **Client secret**.

One OAuth client covers all tools; each new tool just adds another redirect
URI.

## 5. Approvals — needs-approval, always-allow, or blocked

This is the server-side version of Claude's per-tool "always allow / needs
approval / blocked": the desktop toggle is sticky (approve once and it sticks
across every chat) and doesn't reliably apply to custom connectors, so the
always-on approval sidecar owns the gate. Every tool starts **`always_allow`**
(Claude's own permission UI is the first defense line); you gate or block
individual tools at runtime via the gatekeeper's panel or `set_gating`
([docs/GATEKEEPER.md](../GATEKEEPER.md)) — nothing needs a redeploy. A gated
call then reports a plain pending status in chat while an Approve/Deny card
lands in your channel. (No link goes to the chat — a tool result carrying an
approval URL reads as prompt injection and gets flagged or refused.)

- **Configure the channel** (do this unless you opt the layer off):

  ```bash
  cp security/approval/service/env.example security/approval/service/.env
  ```

  Set `APPROVAL_PROVIDER` to `slack`, `discord`, or `telegram` and follow that
  provider's steps inside the file — including the one-time webhook step at
  `https://approval.example.com/{slack|discord|telegram}/interact`. Discord
  validates its endpoint on save, and Telegram's `setWebhook` arms the secret
  the webhook checks, so run those *after* the sidecar is up. Use a platform
  your agent doesn't operate — a card its own tools can read and click defeats
  the gate. Without a channel configured, gated calls report the approval as
  undeliverable — they never silently run.

- **No approval layer at all** — `MCP_REQUIRE_APPROVAL=0` in the root `.env`.

- **Blocked** — a per-tool mode in the panel; or leave the tool out of your
  deploy entirely.

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
docker compose logs -f telegram    # expect: "OAuth enabled (Google) at https://telegram..."
```

**Browser tool only — one-time Drive consent.** The browser's artifacts
(downloads, recordings, PDFs) auto-sync to a `browser-artifacts` folder in your
Google Drive and are deleted locally after upload. That sync needs one consent
click: `docker compose logs browser-sync` prints the exact command to run
(also documented in `tools/browser/env.example`). Until then the sidecar just
idles — nothing breaks.

## 8. Verify a public endpoint

```bash
curl -s https://telegram.example.com/.well-known/oauth-authorization-server | head -c 300; echo
curl -s https://telegram.example.com/.well-known/oauth-protected-resource/mcp; echo
# 401 MUST carry WWW-Authenticate with resource_metadata=... :
curl -sD - -o /dev/null https://telegram.example.com/mcp | grep -i www-authenticate
```

The last line must print `WWW-Authenticate: Bearer ... resource_metadata=...`.

## 9. Add the connectors in Claude

Settings → Connectors → Add custom connector → `https://<tool>.example.com/mcp`
→ Connect → Google login. Works on **desktop** and **claude.ai web**;
**mobile** inherits it.

---

## Adding or removing a tool later

Your initial tool choice isn't privileged — `COMPOSE_PROFILES` is just a line in the
root `.env`, and all the shared infrastructure (tunnel routes for every shipped tool,
the OAuth client, egress listeners) already exists. Adding a tool you skipped:

1. `cp tools/<name>/env.example tools/<name>/.env` and fill its secrets — each tool's
   `tools/<name>/deploy.json` manifest says exactly which and where to get them.
2. Add `https://<name>.example.com/auth/callback` to the shared Google OAuth client's
   authorized redirect URIs (skip if you pre-added all tools' callbacks in step 4).
3. Add `<name>` to `COMPOSE_PROFILES` in the root `.env`, then
   `docker compose -f docker-compose.yml -f docker-compose.tunnel.yml up -d --build <name>`.
4. Add the connector in Claude (step 9). No tunnel or DNS changes — the route
   already exists and stops 502ing the moment the container is healthy.

The tool appears in the gatekeeper's manage panel within ~30 s of starting (its
health probe registers it), pre-gateable before Claude ever connects.

Removing one is the mirror: delete it from `COMPOSE_PROFILES`,
`up -d --remove-orphans` (both `-f` files!), remove the connector in Claude, and
Forget its now-stale section in the manage panel. Its image, secrets, and state
volume stay on disk, so re-adding later is instant — though forgotten permission
modes start back at `always_allow`.

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
