# Egress proxy — kill the exfiltration leg

An **allowlist proxy** (squid) that every mcp-tool is forced through, so a tool's
process can only reach its expected hosts — the *strongest single control*: even a
hostile/compromised dependency **cannot exfiltrate** if it can only talk where it's
supposed to.

## How it's enforced (two layers)

1. **squid** does domain allowlisting at the `CONNECT` level (no TLS interception),
   **default-deny**, **per-tool** (each tool gets its own listener mapped to its own
   allowlist via `myportname`). Its `access.log` is the central egress audit trail.
2. **The Docker network** makes the proxy the only route off-box: each tool sits on the
   `internal` network (`internal: true`, no gateway), and the squid sidecar is its only
   peer with an `edge` leg. A dep that ignores the proxy env has nowhere to dial.

```
xmcp (internal net, no gateway) ──HTTPS_PROXY──▶ egress:3128 ──allowlist──▶ api.x.com / api.x.ai / Google OAuth
        every other destination ──▶ no route                 (anything else ──▶ 403 + logged)
```

## Files
- `squid.compose.conf` — the sidecar's config: one listener per service (`3128` x-mcp /
  `3129` data / `3130` lean / `3131` telegram / `3132` approval / `3133` guardrail),
  default-deny. Mounted read-only into the `egress` service.
- `allowlist/<service>.txt` — that service's allowed domains, mounted at
  `/etc/squid/allowlist/`.

## Adding a tool
`scripts/new-tool.sh` prints the exact block: one `http_port <port> name=<tool>` + an
`acl`/`http_access` pair in `squid.compose.conf`, plus an `allowlist/<tool>.txt`. Point
the tool's `HTTPS_PROXY` at `http://egress:<port>`, then `docker compose restart egress`.

## Verify (the "test that it blocks" gate)
```bash
# NEGATIVE — a non-allowlisted host through the proxy is blocked:
docker compose exec <tool> python -c "import httpx; print(httpx.get('https://example.com'))"  # -> ProxyError 403
#   ...and proxy-bypassed: no route off the internal network.
# POSITIVE — allowlisted hosts work end to end (X search, Grok, Google login).
docker compose exec egress tail -f /var/log/squid/access.log   # watch TCP_DENIED vs TCP_TUNNEL/200
```

## Scope / limits
- CONNECT-domain level only — not payload inspection. One deliberate exception: the
  guardrail listener carries plain-HTTP to the EC2 instance-metadata address
  (169.254.169.254) so boto3 can fetch instance-role credentials on cloud deploys —
  pinned to that listener + that one link-local IP.
- Allowlist per tool is the union of that tool's needs. The guardrail's own leg
  (`:3133`) covers its two providers: the HuggingFace model pull and the region's
  `bedrock-runtime` endpoint.
