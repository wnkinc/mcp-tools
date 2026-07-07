# Architecture

## Request path (the deployed container stack)

```
Claude desktop / claude.ai web / mobile     (custom connector, private to your account)
        │  HTTPS
        ▼
xmcp.example.com (your MCP_DOMAIN)           Cloudflare edge — TLS, hides home IP, WAF
        │  Cloudflare Tunnel (cloudflared sidecar, outbound-only transport)
        ▼
xmcp container :8061                          FastMCP server on an internal Docker network
        │  FastMCP owns OAuth: "Sign in with Google", locked to an email allowlist
        │  sealed from the internet; output screened by the guardrail sidecar (:8071)
        ▼
egress sidecar (squid) :3128                  per-tool domain allowlist, default-deny, audit log
        ▼
api.x.com (read-only bearer, allowlisted ops) + api.x.ai (grok_x_search) + Google OAuth verify
```

The same image runs locally (`docker compose up`) and in the cloud — transport
(`http`/`stdio`) and the security posture (auth / approval / guardrail) are read from
**env** at startup, so one image serves every environment.

## Why each choice

- **Auth in the MCP server.** Each server runs its own Google OAuth (`FastMCP`
  `OAuthProxy`) with a verified-email allowlist. Keeping auth in the server rather than
  at the edge means it travels with the image — the same container authenticates the
  same way locally or in any cloud — and it works uniformly across Claude desktop, web,
  and mobile.

- **Tunnel = transport only.** The Cloudflare Tunnel provides TLS, hides the home IP,
  and dials outbound only, so the box exposes zero inbound ports. The MCP hostname serves
  its own OAuth; an Access layer on top would just double-auth.

- **One tool per container, own subdomain, isolated.** Each tool is its own image on an
  `internal` network, so each tool's credentials and egress stay isolated — a bug or bad
  dep in one stays contained to that tool.

- **Tools are opt-in (compose profiles).** Every tool service carries a profile named
  after itself; `COMPOSE_PROFILES` in the root `.env` picks which ones a deployment
  builds and runs. The guardrail rides the same mechanism (profile `guardrail`, on by
  default). The egress wall and approval sidecar carry no profile — they're the shared
  substrate and always run. This is what keeps 3 tools or 100 tools the same repo: a
  deployer never pulls the image of a tool they didn't ask for.

- **The guardrail is a provider switch, matched to the deployment path.** The sidecar's
  `/scan` contract is fixed; `GUARDRAIL_PROVIDER` picks the engine behind it —
  `llamafirewall` (local model; the local default) or `bedrock` (Amazon Bedrock
  Guardrails ApplyGuardrail; the AWS default, provisioned by `deploy/aws`). Both leave
  through the guardrail's own egress-wall listener, and the tool middleware fails
  closed either way.

- **Deployment is a chooser, one stack.** `docs/DEPLOY.md` routes to two runbooks —
  your own box (`docs/deploy/local.md`) or an EC2 VM provisioned end-to-end by the
  Pulumi program in `deploy/aws/` (`docs/deploy/aws.md`). Ingress is the shared
  `deploy/cloudflare/` Pulumi stack (tunnel + wildcard DNS) on both paths, so a
  deployment can change hosts later while keeping its domain, tunnel, and
  credentials — both paths run these same compose files behind it.

- **Tools never call each other; cooperation is an artifact plane.** When two tools need
  to cooperate (data produces the lake, lean backtests it), they share a named volume
  carrying artifacts in a documented format — exactly one writer, and the format is the
  contract (here Lean's own on-disk data format, not something we invented). The
  dependency stays soft: lean without data simply reports no data, data without lean
  just exports to a volume nobody reads. No tool ever holds another tool's credentials
  or network access.

- **All internet access flows through the egress allowlist (the strongest single
  control).** Each tool sits on an `internal` Docker network whose only route off-box is
  the squid sidecar; squid enforces a per-tool domain allowlist (default-deny) and is the
  central egress audit log. Verified: allowlisted hosts succeed through the proxy, others
  get `TCP_DENIED/403`, and a proxy-bypass attempt is dropped.

- **Approvals live in a sidecar (one owner, one-click for every tool).** Slack
  delivers every button click to a single app-level Request URL, so pending-approval
  state can't live per-tool — the approval sidecar (`security/approval/service/`,
  `http://approval:8072` internally, `approval.<MCP_DOMAIN>` publicly) owns all
  tokens, the approve page, and the Slack webhook. Tools only create/query their own
  approvals; decisions are written solely by the human channels (capability-URL page
  or Slack-signed webhook), so a compromised tool can't approve itself — and the
  Slack bot token lives in exactly one container.

- **Google OAuth with a verified-email allowlist, fail-closed.** `GoogleProvider`
  authenticates *any* Google account; `security/auth.py` wraps its token verifier to
  accept only logins whose verified email is in `MCP_ALLOWED_GOOGLE_EMAILS`, and requires
  an allowlist + credentials before it will start. While the Google consent screen is in
  "Testing", only added test-user emails can complete the login.

## Adding a tool

`scripts/new-tool.sh <name> <port>` stamps `tools/<name>/` — a `server.py` stub wired
to `security/serve.py`, `requirements.txt`, `env.example`, and a `Dockerfile` from
`scripts/templates/` — creates the tool's (empty, default-deny) egress allowlist, and
inserts the compose service (opt-in `profiles:` entry + state volume) into
`docker-compose.yml`. It then prints the follow-up steps, which stay manual on purpose
— each is a security decision:

1. **Lock deps** — `uv pip compile --generate-hashes` → `requirements.lock`
   (CI installs locks in `--require-hashes` mode, so an unlocked dep can't merge).
2. **Tests + CI** — write a thin `test_<name>.py` (copy an existing tool's), then the
   three CI touchpoints in `.github/`: a pytest matrix entry in `workflows/ci.yml`,
   the tool's directory in `dependabot.yml`, and the tool in compose-validate's
   `.env` stub loop.
3. **Egress** — a listener in `security/egress-proxy/squid.compose.conf` + only the
   hosts this tool must reach in its allowlist file.
4. **Ingress** — a hostname route in the cloudflared `configs:` block of
   `docker-compose.tunnel.yml`, plus the service entry flipping its public posture
   (`MCP_AUTH_ENABLED=1`, `MCP_PUBLIC_URL`). DNS is already covered by the wildcard
   record the `deploy/cloudflare/` stack owns. On a live stack, routes apply when
   cloudflared is *recreated* (its config renders at `up`), and squid config changes
   need an egress *restart* (single-file bind mounts keep the pre-pull inode).
5. **Secrets & identity** — `cp env.example .env` and fill it; add the tool's
   `/auth/callback` URL to the shared Google OAuth client's authorized redirect URIs.
6. **Enable** — add the tool to `COMPOSE_PROFILES` (root `.env` + `env.example`'s
   list), `docker compose up -d --build <name>`, then add the custom connector in
   Claude (desktop + web).

The script's own output prints the exact config snippets for each step.
