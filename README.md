# claude-custom-connector-server

Self-hosted [MCP](https://modelcontextprotocol.io) servers exposed to the Claude
apps (macOS desktop, claude.ai web, mobile) via Cloudflare Tunnel, with each tool
gated by Google OAuth (verified-email allowlist). Runs on your own Linux box or
on an EC2 VM that `pulumi up` provisions — same stack either way
(**[docs/DEPLOY.md](docs/DEPLOY.md)** is the chooser).

## What you'll need

Gather these before you start — the list is the same either way you host, and
every runbook step walks through the setup. Total out-of-pocket: nothing locally,
~$15/mo if you let Pulumi run the EC2 VM.

| | What for |
|---|---|
| **A Linux box with Docker** (compose v2.20+) — or an AWS account instead | runs the stack; the AWS path provisions a t3.small for you |
| **A domain on Cloudflare** (free plan is fine) + an API token (`Cloudflare Tunnel:Edit` + `DNS:Edit`) | public HTTPS ingress — one subdomain per tool, no inbound ports opened |
| **[Pulumi CLI](https://www.pulumi.com/docs/install/)** | creates the tunnel + wildcard DNS (and the VM on AWS); `pulumi login --local` keeps state on your machine |
| **A Google Cloud OAuth client** (free) | the "Sign in with Google" gate on every tool, locked to your email allowlist |
| **A Hugging Face token** + access to `meta-llama/Llama-Prompt-Guard-2-86M` (free, granted in minutes) | the local guardrail model that screens untrusted tool output. Local path only — AWS uses Bedrock Guardrails |
| **An approval channel** — a Slack, Discord, or Telegram bot | where Approve/Deny cards land for gated tool calls. Pick a platform the agent itself does *not* operate |
| **Per-tool secrets** — e.g. Telegram API credentials | only for the tools you turn on; each documents its own `tools/<tool>/env.example` |

The one deploy-time decision is **which tools to run** (`COMPOSE_PROFILES` in
`.env`) — the security substrate comes up on its own, and permissions, approvals,
and adding tools later are all runtime changes. Full detail and the local-vs-AWS
chooser: **[docs/DEPLOY.md](docs/DEPLOY.md)**.

Just hacking on a tool? None of the above — `docker compose up --build` runs the
stack locally with auth and tunnel off.

## Quick start

#### Terminal
```bash
git clone https://github.com/wnkinc/claude-custom-connector-server.git mcp-tools
cd mcp-tools
claude        # then say: "deploy this"
```

#### Claude Code
Claude Code on the desktop app. Tell it where this repo lives and what you want:

> Download https://github.com/wnkinc/claude-custom-connector-server and deploy it

Claude takes it from there.

## The tools

Each tool is its own container and its own connector (`https://<subdomain>.<your-domain>/mcp`),
opt-in via `COMPOSE_PROFILES`. Built on open source wherever one fits — the wrapper
adds the shared security stack (OAuth, egress wall, guardrail, approvals), not a new engine:

| Tool | What it is | Built on |
|---|---|---|
| `telegram` | Your Telegram account as tools (read-only by default; writes are opt-in + gated) | [chigwell/telegram-mcp](https://github.com/chigwell/telegram-mcp), vendored + pinned |
| `workspace` | Google Workspace — Gmail, Drive, Calendar, Docs, Sheets, Slides, Tasks, Chat — as your account | [taylorwilsdon/google_workspace_mcp](https://github.com/taylorwilsdon/google_workspace_mcp), vendored |
| `browser` | A real web browser as tools, plus a live noVNC view for human watch/takeover | [microsoft/playwright-mcp](https://github.com/microsoft/playwright-mcp), npm-pinned |
| `gatekeeper` | The control plane: per-tool permissions via the in-chat panel | native (always on, like the sidecars) |

## FAQs
Each tool is opt-in via a compose profile named after it — only the tools in
`COMPOSE_PROFILES` are built and started, and that list is the only deploy-time
choice. The rest is automatic: the guardrail (output screen) starts alongside any
untrusted tool, with an env-chosen provider — a local model (`llamafirewall`,
default) or Amazon Bedrock Guardrails (`bedrock`, the AWS-deploy pick).

New tool: `scripts/new-tool.sh`. Deploying:
**[docs/DEPLOY.md](docs/DEPLOY.md)**. How it fits together:
**[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**.


## The model in one breath

One **portable tool per container**: a FastMCP server that reads its transport
and security posture from **env**, so the same image runs locally and in the
cloud unchanged. Tools sit on an **internal Docker network with no route to the
internet**; the only way out is the **squid egress sidecar**, and each tool gets
its **own listener and domain allowlist** there — a bad dep can only reach its
own tool's short list. A **Cloudflare Tunnel** sidecar fronts them, one
subdomain per tool (transport only; **auth lives in each MCP server** — Google
OAuth with a verified-email allowlist — so it travels with the image and works
across Claude desktop, web, and mobile). Two more sidecars round out the
substrate: a **guardrail** that screens the untrusted tools' output for prompt
injection before it reaches your model context (provider env-chosen: local
model or Amazon Bedrock Guardrails), and an **approval** service for
human-in-the-loop gating of sensitive tool calls.
