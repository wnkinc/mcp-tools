# mcp-tools

Self-hosted [MCP](https://modelcontextprotocol.io) servers exposed to the Claude
apps (macOS desktop, claude.ai web, mobile) via Cloudflare Tunnel, with each tool
gated by Google OAuth (verified-email allowlist). Runs on your own Linux box or
on an EC2 VM that `pulumi up` provisions — same stack either way
(**[docs/DEPLOY.md](docs/DEPLOY.md)** is the chooser).

## The model in one breath

One **portable tool per container**: a FastMCP server that reads its transport and
security posture from **env**, so one image runs locally and in the cloud unchanged.
Each tool sits on an **internal Docker network sealed from the internet** — all egress
goes through a **squid allowlist sidecar**, so a bad dep stays confined to its allowlist.
A **Cloudflare Tunnel** sidecar fronts them, each on its own subdomain (transport only;
the server owns auth). **Auth lives in the MCP server** (FastMCP Google OAuth with a
verified-email allowlist), so it travels with the image and works uniformly across Claude
desktop, web, and mobile.

## Quick start

```bash
git clone https://github.com/wnkinc/claude-custom-connector-server.git mcp-tools
cd mcp-tools
claude        # then say: "deploy this"
```

Claude drives the whole deployment — it reads the runbooks, does every step it
can itself, and stops only where you're needed (accounts, tokens, approving
spend).

**Not a terminal person?** Use
[Claude Cowork](https://support.claude.com/en/articles/13345190-get-started-with-claude-cowork)
(the Claude desktop app's agent workspace) instead: point it at the cloned
folder — or just ask it to clone this repo for you — and say **"deploy this"**.
The repo's deploy skill is picked up there the same as in the terminal.
(Windows: keep the folder under `C:\Users\<you>\`.) With the
[Claude Chrome extension](https://support.claude.com/en/articles/12012173-get-started-with-claude-in-chrome)
connected, Claude can also help you click through the browser-only steps
(Cloudflare dashboard, Google Cloud console).

Driving it yourself instead? **[docs/DEPLOY.md](docs/DEPLOY.md)** is the
chooser; it links the step-by-step runbook for each path
([local](docs/deploy/local.md), [AWS](docs/deploy/aws.md)).

Each tool is opt-in via a compose profile named after it — only the tools in
`COMPOSE_PROFILES` are built and started, so you never pull an image (lean's is
13GB) for a tool you don't want. The guardrail (output screen for untrusted
tools) rides the same mechanism, with an env-chosen provider: a local model
(`llamafirewall`) or Amazon Bedrock Guardrails (`bedrock`).

New tool: `scripts/new-tool.sh`. Deploying:
**[docs/DEPLOY.md](docs/DEPLOY.md)**. How it fits together:
**[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**.
