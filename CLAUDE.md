## Layout

```
docker-compose.yml         # the stack: tools + guardrail + egress sidecars (local, auth off)
docker-compose.tunnel.yml  # public overlay: Cloudflare ingress routes + auth-on posture
env.example                # deployment identity (MCP_DOMAIN, TUNNEL_ID) -> cp to .env;
                           #   compose interpolates it into the tunnel overlay
security/                  # shared plumbing, imported by every tool
  serve.py                 #   serve(mcp, ...): env-selects transport + security posture, runs it
  auth.py                  #   Google OAuth provider (email allowlist, fail-closed)
  approval/                #   out-of-band human-in-the-loop approval gate + tool-mode authority
                           #     (per-tool always_allow/needs_approval/blocked; the gatekeeper tool
                           #     + in-chat manage_tools panel edit it — see docs/GATEKEEPER.md)
  egress-proxy/            #   squid egress allowlist (per-tool domains, default-deny)
  ingress/                 #   tunnel creds staging (gitignored; routing lives in the overlay)
  guardrail/service/       #   output-screen sidecar; GUARDRAIL_PROVIDER=llamafirewall|bedrock
  eval/                    #   garak red-team harness
tools/                     # one tool per dir: server.py + Dockerfile + requirements.lock
  xmcp/                    #   X API full surface (reads + OAuth1 user-context writes) (:8061)
  data/                    #   market data via OpenBB -> parquet lake (:8062)
  lean/                    #   QuantConnect Lean backtests of agent-authored algorithms (:8064)
  gatekeeper/              #   control plane over every tool's approval mode (:8065; docs/GATEKEEPER.md)
deploy/                    # IaC: cloudflare/ = shared ingress (tunnel+DNS, both paths);
                           #   aws/ = EC2 VM running this stack (reads the ingress stack)
scripts/new-tool.sh        # stamp a new tool
docs/                      # DEPLOY.md chooser -> deploy/{local,aws}.md runbooks; ARCHITECTURE.md
```
