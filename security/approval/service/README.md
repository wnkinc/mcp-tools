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
  approval page, linked from the Slack card as a fallback if the buttons fail.
- `POST /slack/interact` (public via the tunnel, Slack-signature verified):
  the card's Approve/Deny buttons.
- `GET /healthz`.

The card platform is selected by `APPROVAL_PROVIDER` (default `slack`; `discord`
and `telegram` are planned — telegram must never deliver approvals while the
telegram tool is deployed, since that tool operates the user's own account and
could press Approve itself). An unimplemented provider fails closed: startup
refuses it, and `/gate` reports every approval undeliverable.

The card is the ONLY channel that reaches the human: the model-facing gate message
is a bare pending status with no URL, because a tool result that asks the model
to relay a link is indistinguishable from prompt injection and gets flagged or
refused (claude.ai screening and the model both). `/gate` reports whether the
card was delivered (`notified`), and the middleware fails loud when it wasn't —
without working Slack values, gated actions cannot be approved at all.

Setup: `cp env.example .env` and follow its Slack-app steps — including pointing
the app's Interactivity Request URL at `https://approval.<MCP_DOMAIN>/slack/interact`
(once, ever).

Tests: `pytest security` (service exercised in-process via Starlette's TestClient,
middleware end-to-end against the real app over an ASGI transport).
