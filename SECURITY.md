# Security

This is a personal project, provided as-is with no support or SLA. That said,
it exists to demonstrate a hardened MCP serving posture, so security reports
are genuinely welcome.

## Reporting a vulnerability

Please **do not open a public issue** for anything exploitable. Use GitHub's
private reporting instead: **Security tab → Report a vulnerability** on this
repository. You'll get a response as soon as I see it — typically within a few
days.

## Scope notes

- Secrets are never committed: each tool reads its own gitignored `.env`
  (see `<tool>/env.example`), and the Cloudflare tunnel credential lives in
  gitignored `security/ingress/secrets/`. The committed tunnel routing config
  and hostnames are intentionally public.
- The interesting attack surface is the layering in `security/`: OAuth email
  allowlisting (`auth.py`), the out-of-band approval gate (`approval/`), the
  guardrail output screen (`guardrail/`), and the per-tool egress allowlists
  (`egress-proxy/`). Reports about bypasses of any of these layers are the
  most valuable kind.
