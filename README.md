# mcp-tools

Self-hosted [MCP](https://modelcontextprotocol.io) servers exposed to the Claude
apps (macOS desktop, claude.ai web, mobile) from a home Linux box, via Cloudflare
Tunnel, with each tool gated by Google OAuth (verified-email allowlist).

## The model in one breath

One **portable tool per container**: a FastMCP server that reads its transport and
security posture from **env**, so the same image runs locally, on the hardened box, or
in the cloud with no code fork. Each tool sits on an **internal Docker network with no
internet of its own** — all egress is forced through a **squid allowlist sidecar** (a
bad dep can't exfiltrate). A **Cloudflare Tunnel** sidecar fronts them, each on its own
subdomain (transport only — no Access policy). **Auth lives in the MCP server** (FastMCP
Google OAuth), not in Cloudflare, because that's the only way the claude.ai web/mobile
connectors work (see [docs/SETUP.md](docs/SETUP.md) for the Cloudflare-Access bug this
avoids).

Three **substrates** run the same tools — the portable **container** stack (what's
deployed), per-tool **systemd** units (further-hardened home box), and desktop
**stdio** — chosen by env. See **[docs/SUBSTRATE.md](docs/SUBSTRATE.md)**.

## Layout

```
mcp-tools/
  docker-compose.yml         # the stack: tools + guardrail + egress sidecars (local, auth off)
  docker-compose.tunnel.yml  # public overlay: adds the Cloudflare ingress + auth-on posture
  security/                  # shared plumbing, imported by every tool
    serve.py                 #   serve(mcp, ...): env-selects transport + security posture, runs it
    auth.py                  #   Google OAuth provider (email allowlist, fail-closed)
    approval/                #   out-of-band human-in-the-loop approval gate
    egress-proxy/            #   squid egress allowlist (host squid.conf + compose variant)
    ingress/                 #   Cloudflare tunnel routing (creds injected, gitignored)
    guardrail/service/       #   standalone LlamaFirewall output-screen service (own sidecar)
    eval/                    #   garak red-team harness
  tools/                     # one tool per dir: server.py + Dockerfile + systemd unit
    xmcp/                    #   X read-only search/lookup + Grok x_search (:8061)
    data/                    #   market data via OpenBB -> parquet lake (:8062)
    quant/                   #   research library (Hamilton DAG) + backtest engines (:8064)
  scripts/                   # install-system.sh (systemd bootstrap), new-tool.sh
  docs/                      # SUBSTRATE.md, SETUP.md, ARCHITECTURE.md
```

## Quick start

Container stack (primary):

```
X_BEARER_TOKEN=... docker compose up --build                                   # local (auth off)
docker compose -f docker-compose.yml -f docker-compose.tunnel.yml up -d        # public (auth on)
```

Hardened home box (systemd): `sudo scripts/install-system.sh`. New tool:
`scripts/new-tool.sh`. Full detail: **[docs/SUBSTRATE.md](docs/SUBSTRATE.md)**.
