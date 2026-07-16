# workspace (:8066)

[taylorwilsdon/google_workspace_mcp](https://github.com/taylorwilsdon/google_workspace_mcp)
— Gmail, Drive, Calendar, Docs, Sheets, Slides, Tasks, Chat and more as MCP
tools, acting as YOUR Google account — vendored (pinned checkout in the
Dockerfile) and imported natively behind this repo's shared stack: `server.py`
registers the engine's tools on a FastMCP server that `serve()` wraps with
Google OAuth, the egress wall, the guardrail, and approvals.

## Posture

- **Untrusted output**: mail bodies, docs, and comments are third-party-authored
  content, so every result screens through the guardrail.
- **Two OAuth layers, one Google client**: the shared MCP-auth identity gates who
  may connect Claude; the ENGINE separately needs
  `GOOGLE_OAUTH_REDIRECT_URI=https://workspace.<your-domain>/oauth2callback` to
  match the Google client's Authorized redirect URIs EXACTLY (the tunnel overlay
  stamps it) for its own user-consent flow.
- **Egress behind the wall**: the engine's HTTP clients need SOCKS-capable
  transports (`pysocks`/`httplib2` are in the lock) — a plain client that
  ignores proxy env has nowhere to dial.

Secrets and prerequisites are declared in [deploy.json](deploy.json), the
tool's manifest; see [env.example](env.example) for setup.
