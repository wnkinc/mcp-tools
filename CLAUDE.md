## Layout

```
docker-compose.yml         # the stack: tools + guardrail + egress sidecars (local, auth off)
docker-compose.tunnel.yml  # public overlay: adds the Cloudflare ingress + auth-on posture
security/                  # shared plumbing, imported by every tool
  serve.py                 #   serve(mcp, ...): env-selects transport + security posture, runs it
  auth.py                  #   Google OAuth provider (email allowlist, fail-closed)
  approval/                #   out-of-band human-in-the-loop approval gate
  egress-proxy/            #   squid egress allowlist (per-tool domains, default-deny)
  ingress/                 #   Cloudflare tunnel routing (creds injected, gitignored)
  guardrail/service/       #   standalone LlamaFirewall output-screen service (own sidecar)
  eval/                    #   garak red-team harness
tools/                     # one tool per dir: server.py + Dockerfile + requirements.lock
  xmcp/                    #   X read-only search/lookup + Grok x_search (:8061)
  data/                    #   market data via OpenBB -> parquet lake (:8062)
scripts/new-tool.sh        # stamp a new tool
docs/                      # SETUP.md, ARCHITECTURE.md (how it fits together)
```
