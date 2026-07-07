# claude-custom-connector-server

Self-hosted [MCP](https://modelcontextprotocol.io) servers exposed to the Claude
apps (macOS desktop, claude.ai web, mobile) via Cloudflare Tunnel, with each tool
gated by Google OAuth (verified-email allowlist). Runs on your own Linux box or
on an EC2 VM that `pulumi up` provisions — same stack either way
(**[docs/DEPLOY.md](docs/DEPLOY.md)** is the chooser).

## Quick start

### Terminal
```bash
git clone https://github.com/wnkinc/claude-custom-connector-server.git mcp-tools
cd mcp-tools
claude        # then say: "deploy this"
```

### Claude Cowork
Claude in the desktop app, no terminal needed. Tell it where this repo lives and what you want:

> Download https://github.com/wnkinc/claude-custom-connector-server and deploy it

Claude takes it from there.

### Driving it yourself instead?
**[docs/DEPLOY.md](docs/DEPLOY.md)** is the chooser; it links the step-by-step runbook for each path
([local](docs/deploy/local.md), [AWS](docs/deploy/aws.md)).

## FAQs
Each tool is opt-in via a compose profile named after it — only the tools in
`COMPOSE_PROFILES` are built and started. The guardrail (output screen for untrusted
tools) rides the same mechanism, with an env-chosen provider: a local model
(`llamafirewall`) or Amazon Bedrock Guardrails (`bedrock`).

New tool: `scripts/new-tool.sh`. Deploying:
**[docs/DEPLOY.md](docs/DEPLOY.md)**. How it fits together:
**[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**.


## The model in one breath

One **portable tool per container**: a FastMCP server that reads its transport and
security posture from **env**, so one image runs locally and in the cloud unchanged.
Each tool sits on an **internal Docker network sealed from the internet** — all egress
goes through a **squid allowlist sidecar**, so a bad dep stays confined to its allowlist.
A **Cloudflare Tunnel** sidecar fronts them, each on its own subdomain (transport only;
the server owns auth). **Auth lives in the MCP server** (FastMCP Google OAuth with a
verified-email allowlist), so it travels with the image and works uniformly across Claude
desktop, web, and mobile.
