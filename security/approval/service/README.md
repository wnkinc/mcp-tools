# approval sidecar (:8072)

Single owner of pending approvals + card delivery for the whole stack. Exists
because a provider app (Slack, Discord, or Telegram) delivers every button click
to ONE app-level webhook URL — with approval state per-tool, only that one tool's
buttons
worked; every other tool's card answered "expired". Centralizing the state
makes one-click Approve work for any number of tools, and moves the provider
bot tokens out of every tool container into exactly one place.

Surfaces:

- `POST /gate` (compose-internal): tools create/check an approval for a
  (tool, args) call. Tools can only create and query — the only writers of a
  decision are the two human channels below, so a compromised tool can't
  approve itself.
- `GET/POST /approve/{token}` (public via the tunnel, capability token): the
  approval page, linked from the card as a fallback if the buttons fail.
- `POST /slack/interact` (public via the tunnel, Slack HMAC verified):
  the Slack card's Approve/Deny buttons.
- `POST /discord/interact` (public via the tunnel, Ed25519 verified):
  the Discord card's buttons, plus Discord's endpoint-validation PING.
- `POST /telegram/interact` (public via the tunnel, secret-token verified):
  the Telegram card's inline buttons (`callback_query` updates). Telegram does
  not sign the body like Slack/Discord — `setWebhook` registers a secret token
  it echoes in a header, and that shared secret (over HTTPS) is the gate.
- `GET /healthz`.

The card platform is selected by `APPROVAL_PROVIDER`: `slack` (default),
`discord`, or `telegram`. An unimplemented provider fails closed: startup
refuses it, and `/gate` reports every approval undeliverable.

Approval is HUMAN-in-the-loop — pick a platform (or at least an account) the
agent does not operate. If the agent's own tools can read the card and press
its buttons, the gate can approve itself. It's allowed, but it's on you to keep
the channel out of the agent's reach — most sharply for Telegram: the approval
bot is a separate identity, but running it in a chat the telegram *tool*'s
account can see means the agent can read the card, so use a chat that account
is not in. Button clicks only arrive via the signature/secret-verified
webhooks, which no tool can forge.

The card is the ONLY channel that reaches the human: the model-facing gate message
is a bare pending status with no URL, because a tool result that asks the model
to relay a link is indistinguishable from prompt injection and gets flagged or
refused (claude.ai screening and the model both). `/gate` reports whether the
card was delivered (`notified`), and the middleware fails loud when it wasn't —
without a working provider config, gated actions cannot be approved at all.

Setup: `cp env.example .env` and follow its Slack-, Discord-, or Telegram-app
steps — including the one-time webhook URL step (Slack's Interactivity Request
URL, Discord's Interactions Endpoint URL, or the Telegram `setWebhook` call, at
`https://approval.<MCP_DOMAIN>/{slack|discord|telegram}/interact`). Discord
validates its endpoint on save, so the sidecar must be live first; Telegram's
`setWebhook` carries the `secret_token` the webhook then verifies.

Tests: `pytest security` (service exercised in-process via Starlette's TestClient,
middleware end-to-end against the real app over an ASGI transport).
