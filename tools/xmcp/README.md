# xmcp

Upstream https://github.com/xdevplatform/xmcp (X's official MCP server example) with a minimal delta, run through the shared security stack (`security/serve.py`).

- **Surface**: X's live OpenAPI spec filtered by a code-enforced grant. Default = 8 curated read ops; `X_API_TOOL_ALLOWLIST=all` exposes every read; writes additionally need `X_API_ALLOW_WRITES=1`.
- **Auth**: OAuth1 user-context when the four `X_OAUTH_*` values are set — every request is signed and acts as your account (upstream's auth, with tokens pre-minted in the developer portal instead of its browser consent flow, which can't run headless). App-only `X_BEARER_TOKEN` is the read-only fallback.
- **Delta from upstream**: `serve()` wiring (Google OAuth, out-of-band approval, guardrail), write-guard + fail-closed allowlist default, MCP `ToolAnnotations` (`readOnlyHint` drives Claude's read-only vs write/delete permission categories; upstream sets none), and the guardrail outputSchema strip. Which tools require approval is the sidecar's stored per-tool modes (all ship `always_allow`; gate individual writes via the gatekeeper or the manage panel).
- **History**: a custom `grok_x_search` tool (xAI Grok's own X search; never part of upstream) lived here until 2026-07-04 — recover from git history if wanted; its `XAI_*` env keys stay parked in `.env`.
