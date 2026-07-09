# claude-custom-connector-server

Self-hosted [MCP](https://modelcontextprotocol.io) servers exposed to the Claude
apps (macOS desktop, claude.ai web, mobile) via Cloudflare Tunnel, with each tool
gated by Google OAuth (verified-email allowlist). Runs on your own Linux box or
on an EC2 VM that `pulumi up` provisions — same stack either way
(**[docs/DEPLOY.md](docs/DEPLOY.md)** is the chooser).

## Quick start

#### Terminal
```bash
git clone https://github.com/wnkinc/claude-custom-connector-server.git mcp-tools
cd mcp-tools
claude        # then say: "deploy this"
```

#### Claude Cowork
Claude in the desktop app, no terminal needed. Tell it where this repo lives and what you want:

> Download https://github.com/wnkinc/claude-custom-connector-server and deploy it

Claude takes it from there. (On the AWS path, one prereq installer wants
interactive sudo, which Cowork doesn't have — the
[AWS runbook](docs/deploy/aws.md) prereqs include a no-sudo alternative.)

## FAQs
Each tool is opt-in via a compose profile named after it — only the tools in
`COMPOSE_PROFILES` are built and started. The guardrail (output screen for untrusted
tools) rides the same mechanism, with an env-chosen provider: a local model
(`llamafirewall`) or Amazon Bedrock Guardrails (`bedrock`).

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
