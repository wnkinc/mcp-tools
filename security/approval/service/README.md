# approval sidecar (:8072)

Single owner of pending approvals + Slack delivery for the whole stack. Exists
because a Slack app delivers every button click to ONE app-level Request URL —
with approval state per-tool, only that one tool's buttons worked; every other
tool's card answered "expired". Centralizing the state makes one-click Approve
work for any number of tools, and moves the Slack bot token out of every tool
container into exactly one place.

Surfaces:

- `POST /gate` (compose-internal): tools create/check an approval for a
  (tool, args) call. Tools can only create and query — the only writers of a
  decision are the two human channels below, so a compromised tool can't
  approve itself.
- `GET/POST /approve/{token}` (public via the tunnel, capability token): the
  approval page linked in chat.
- `POST /slack/interact` (public via the tunnel, Slack-signature verified):
  the card's Approve/Deny buttons.
- `GET /healthz`.

Setup: `cp env.example .env` and follow its Slack-app steps — including pointing
the app's Interactivity Request URL at `https://approval.<MCP_DOMAIN>/slack/interact`
(once, ever). Without Slack values the sidecar still gates via the page link.

Tests: `pytest security` (service exercised in-process via Starlette's TestClient,
middleware end-to-end against the real app over an ASGI transport).
