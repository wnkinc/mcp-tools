---
name: new-tool
description: Add a new MCP tool server to this stack end-to-end — scaffold, trust posture, egress allowlist, wiring, tests, verification. Use when asked to add/build/wire a new tool or MCP server into this repo, or to research candidate tools and implement one.
---

# Add a new tool to the mcp-tools stack

The stack's shape is fixed; a new tool is a set of decisions plus mechanical
wiring. Make the decisions FIRST (ask the user anything you can't determine),
then scaffold, then wire until `pytest security/test_stack.py` is green — it
names anything still missing, and CI enforces it on the PR.

## The decisions (in order)

**1. Trust posture — the one that matters.** Does ANY tool on this server
return content authored by third parties (messages, posts, emails, docs,
comments, search results, web pages)? Screening is per-SERVER, not per-tool:
one middleware screens every result, so a single untrusted-returning tool makes
the whole server untrusted.

- Yes → `serve(mcp, port=port, untrusted_output=True)`, plus the compose
  untrusted kit (below). When unsure, choose untrusted — a wrongly-trusted
  server passes injection to the model; a wrongly-untrusted one only pays a
  scan per call.
- Only data the user or their own systems produce (market data, backtests,
  control-plane state) → trusted: plain `serve(mcp, port=port)`.

**2. Engine strategy.** Smallest owned wrapper surface wins (the operator's
standing preference):

- An existing MCP server/engine exists → wrap or import it behind `serve()`
  (see tools/workspace for native import, tools/telegram for a vendored stdio
  child behind a fastmcp proxy). VENDOR the source if PyPI trust is unclear —
  the telegram engine's PyPI name was a squat; check the real repo.
- Otherwise implement directly against the upstream API, tools kept thin.
- Either way: pin exact versions in requirements.txt, hash-lock (step 2 of the
  flow). Engines doing their own HTTP behind the egress wall may need SOCKS
  support (pysocks/httplib2 gotcha from the workspace build).

**3. Egress domains.** Enumerate what the server must reach; that list IS
`security/egress-proxy/allowlist/<name>.txt` (leading dot = subdomains). Start
minimal — misses show up as `TCP_DENIED` in
`docker compose exec egress cat /var/log/squid/access.log`, add from there.
Google OAuth hosts are needed only if listed in the stub's comment.

**4. Secrets and prerequisites → the deploy manifest.** For each secret the
tool needs: key, human label, and where-to-get hint into `deploy.json`'s
`secrets` (that drives the in-chat staging form) and `env.example`. Browser
steps the user must do first (enable an API, add an OAuth redirect URI) go in
`prerequisites`; image-size/RAM warnings in `notes`.

**5. Port and name.** Port = next free MCP port (check existing
`tools/*/deploy.json` ports + gatekeeper's 8065). Subdomain defaults to the
name. Lowercase, hyphens ok.

## The flow

```bash
scripts/new-tool.sh <name> <port>      # scaffold + compose service + allowlist stub
```

Then, guided by its printed checklist (squid listener, tunnel route + posture
block, CI matrix + dependabot entries) and the decisions above:

1. Write the real `server.py` (replace the ping stub); apply the trust posture.
   Untrusted also means, in docker-compose.yml: `MCP_UNTRUSTED_OUTPUT: "1"`,
   `GUARDRAIL_URL`, `GUARDRAIL_ENABLED`, `guardrail` in `NO_PROXY`, the profile
   name appended to the guardrail service's `profiles` list, and
   `MCP_UNTRUSTED_OUTPUT: "1"` in the tunnel overlay's posture block.
2. Lock: `uv pip compile tools/<name>/requirements.txt --generate-hashes
   --python-version 3.12 -o tools/<name>/requirements.lock`.
3. Wire until green: `uv run --with pytest --with pyyaml -- pytest
   security/test_stack.py -q` — every failure message names the missing piece.
4. Tests: `tools/<name>/test_<name>.py` (thin server tests; copy a sibling's
   shape) + the CI matrix and dependabot entries from the checklist.
5. Verify live: the `verify` skill (run the server + sidecar locally, drive
   real MCP HTTP). For a first smoke: `docker compose up -d --build <name>`
   and check `docker compose ps` health.
6. PR (branch + auto-merge; never commit secrets — env.example carries keys,
   never values).

## After merge (deployment — the user drives)

Deploys are manual, per docs/deploy/local.md: fill `tools/<name>/.env`, add the
profile to `COMPOSE_PROFILES`, `up -d --build` with both `-f` files. Remind the
user of `prerequisites` browser
steps and to add the connector: `https://<subdomain>.<domain>/mcp` in
claude.ai, plus the OAuth redirect URI on the shared Google client. A changed
squid.compose.conf needs `docker compose restart egress`; a new tunnel route
needs a cloudflared `--force-recreate`.
