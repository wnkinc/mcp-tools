# mcp-tools

Self-hosted [MCP](https://modelcontextprotocol.io) servers exposed to the Claude
apps (macOS desktop, claude.ai web, mobile) from a home Linux box, via Cloudflare
Tunnel, with each tool gated by Google OAuth (verified-email allowlist).

## Layout

```
mcp-tools/
  security/              # shared plumbing + threat-model layers (imported by every tool)
    auth.py              #   Google OAuth provider (email allowlist, fail-closed)
    serve.py             #   serve(mcp, ...): applies the layers below + runs the server
    approval/            #   out-of-band human-in-the-loop approval gate (middleware + routes)
    egress-proxy/        #   L2: loopback squid allowlist proxy every tool is forced through
    guardrail/
      middleware.py      #   L4 detect: FastMCP middleware that screens tool output
      service/           #   standalone LlamaFirewall scan service (loopback :8071)
    eval/                #   garak red-team harness
  tools/                 # one hardened system unit per tool (own loopback port + subdomain)
    xmcp/                # X (Twitter) read-only search/lookup + Grok x_search (:8061)
      server.py          #   our FastMCP server on fastmcp.from_openapi (read-only X) + grok_x_search
      systemd/mcp-xmcp.service
      env.example
    data/                # historical market data via OpenBB, persisted to a parquet lake (:8062)
      server.py          #   FastMCP server (OAuth) + thin MCP tools (one per capability)
      feeds.py           #   thin OpenBB fetch fns (equity_bars, crypto_bars, fx_bars)
      lake.py            #   generic parquet persist/merge/read (kind-agnostic)
      systemd/mcp-data.service
    hamilton/            # research library: catalog of reusable indicators/signals (:8064)
      server.py          #   FastMCP server (OAuth) + catalog
      systemd/mcp-hamilton.service
  scripts/
    install-system.sh    # one-time ROOT bootstrap (squid + system units + sudoers)
    new-tool.sh          # stamp a new tool (dir + server stub + unit)
    add-tunnel-route.sh  # add Cloudflare ingress + DNS for a tool
    system/
      mcp-tools.sudoers  # scoped passwordless sudoers (restart units without a password)
    templates/
      unit.template      # hardened system-unit template (used by new-tool.sh)
  docs/
    SETUP.md             # step-by-step runbook (start here)
    ARCHITECTURE.md      # how it fits together + why it's built this way
```

## The model in one breath

One **hardened process per tool**, bound to **loopback**, each on its **own
subdomain** routed by a single **Cloudflare Tunnel** (transport only — no Access
policy). **Auth lives in the MCP server** (FastMCP Google OAuth), not in
Cloudflare, because that is the only way the claude.ai **web/mobile** custom
connectors work (see [docs/SETUP.md](docs/SETUP.md) for the Cloudflare-Access
bug this avoids). All outbound traffic is forced through a **loopback egress
allowlist proxy** (the kernel drops anything else), so a bad dep can't exfiltrate.
Each tool is added to Claude as a **custom connector** (no directory review).

## Quick start

See **[docs/SETUP.md](docs/SETUP.md)** (one-time bootstrap: `sudo
scripts/install-system.sh`). New tool later: `scripts/new-tool.sh`.
