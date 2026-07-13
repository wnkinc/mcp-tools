"""Approval sidecar: one place that owns pending approvals for EVERY tool.

Slack delivers all button clicks to a single app-level Interactivity URL, but
pending-approval state used to live per-tool (in each server process) -- so the
one tool that URL pointed at could answer clicks and every other tool's buttons
"expired". This service is the fix: tools create/query approvals here over the
internal network, and Slack + the human-facing approval page talk ONLY to this
service, which holds all the state. One-click Approve works for any number of
tools, and the Slack bot token lives in exactly one container (not in tools).

Integrity: a tool can only CREATE a pending approval and ASK its status. The
only writers of "approved"/"denied" are the signature-verified provider
webhooks (Slack HMAC / Discord Ed25519) and the capability-token page -- all
human-driven. A compromised tool cannot flip its own approvals.

Endpoints:
  POST /gate               (internal) create-or-check an approval for a tool call
  GET/POST /approve/{token}     (public via tunnel) the human approval page
  POST /slack/interact     (public via tunnel) Slack button clicks, HMAC-verified
  POST /discord/interact   (public via tunnel) Discord button clicks, Ed25519-verified
  POST /telegram/interact  (public via tunnel) Telegram button clicks, secret-token-verified
  GET /healthz             liveness + config visibility

Single uvicorn process => a plain in-memory dict is fine for pending approvals
(they are 10-minute ephemera; a restart just re-prompts).
"""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import logging
import os
import secrets
import time
import urllib.parse

import httpx
from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey
from starlette.applications import Starlette
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("approval")

_PENDING: dict[str, dict] = {}
_TTL_SECONDS = 600  # approval links expire after 10 minutes

# The out-of-band platform that delivers approval cards.
# Human-in-the-loop only works if the platform is one the agent does NOT operate:
# a card the agent's tools can see and press is a gate that approves itself. This
# is allowed but load-bearing on operator judgment -- most sharply for telegram,
# where running this provider on the same account the telegram TOOL operates lets
# the agent read the card and (if it can send) press its own buttons.
_PROVIDERS_IMPLEMENTED = {"slack", "discord", "telegram"}
_PROVIDERS_PLANNED: set[str] = set()

# Human-readable name of the ACTIVE provider, so the model-facing gate message can
# say "posted to your Telegram approval channel" instead of guessing (or naming the
# wrong platform). The tool process doesn't know APPROVAL_PROVIDER -- only the
# sidecar does -- so /gate hands this back and the middleware surfaces it.
_PROVIDER_LABELS = {"slack": "Slack", "discord": "Discord", "telegram": "Telegram"}


def _channel_label() -> str:
    return _PROVIDER_LABELS.get(_provider(), "")


def _provider() -> str:
    return os.getenv("APPROVAL_PROVIDER", "slack").strip().lower()


def _channel_configured() -> bool:
    """Does the ACTIVE provider have the env it needs to reach a human?"""
    return {
        "slack": _slack_enabled,
        "discord": _discord_enabled,
        "telegram": _telegram_enabled,
    }.get(_provider(), lambda: False)()


async def _notify(token: str, action: str, source: str) -> bool:
    """Deliver the approval card via the configured provider.

    Returns True only when a human was actually notified; an unknown or
    not-yet-implemented provider fails closed (False => the gate reports the
    approval as undeliverable).
    """
    provider = _provider()
    if provider == "slack":
        return await _slack_post_approval(token, action, source)
    if provider == "discord":
        return await _discord_post_approval(token, action, source)
    if provider == "telegram":
        return await _telegram_post_approval(token, action, source)
    log.error(
        "APPROVAL_PROVIDER=%r is not implemented (implemented: %s; planned: %s)",
        provider,
        sorted(_PROVIDERS_IMPLEMENTED),
        sorted(_PROVIDERS_PLANNED),
    )
    return False


def _prune() -> None:
    now = time.time()
    for token in [t for t, r in _PENDING.items() if now - r["created"] > _TTL_SECONDS]:
        _PENDING.pop(token, None)


def _approve_link(token: str) -> str:
    base = os.getenv("APPROVAL_PUBLIC_URL", "").rstrip("/")
    return f"{base}/approve/{token}"


# ---------------------------------------------------------------------------
# Internal API: the gate tools call (over the compose-internal network).
# ---------------------------------------------------------------------------
async def gate(request):  # type: ignore[no-untyped-def]
    body = await request.json()
    source, action, call_key = body["source"], body["action"], body["call_key"]
    key = f"{source}\x00{call_key}"  # scope per tool: same args on two tools never collide
    _prune()

    token = next((t for t, r in _PENDING.items() if r["key"] == key), None)
    if token is not None:
        rec = _PENDING[token]
        if rec["status"] == "approved":
            _PENDING.pop(token, None)  # one-time use
            return JSONResponse({"decision": "allow"})
        if rec["status"] == "denied":
            _PENDING.pop(token, None)
            return JSONResponse({"decision": "denied"})
        return JSONResponse(
            {
                "decision": "pending",
                "created": False,
                "notified": rec["notified"],
                "channel_label": _channel_label(),
                "token": token,
            }
        )

    token = secrets.token_urlsafe(24)
    rec = {
        "key": key,
        "source": source,
        "action": action,
        "status": "pending",
        "created": time.time(),
    }
    _PENDING[token] = rec
    # The card is the ONLY channel that reaches the human (no URL goes to the chat --
    # links in tool output trip injection screening), so delivery is load-bearing:
    # report it and let the middleware fail loud instead of waiting on a card nobody got.
    rec["notified"] = await _notify(token, action, source)
    return JSONResponse(
        {
            "decision": "pending",
            "created": True,
            "notified": rec["notified"],
            "channel_label": _channel_label(),
            # The capability token, returned ONLY to the trusted server-side caller (a
            # tool/middleware over the internal net -- the model never calls /gate). It
            # lets an in-chat approval widget redeem the decision; it must never reach
            # model-visible output (goes in _meta or the widget HTML, never in content).
            "token": token,
        }
    )


# ---------------------------------------------------------------------------
# The human approval page (capability token in the URL; public via the tunnel).
# ---------------------------------------------------------------------------
def _approval_shell(title: str, body_html: str) -> str:
    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{html.escape(title)}</title><style>"
        "body{font-family:system-ui,-apple-system,sans-serif;max-width:34rem;"
        "margin:3rem auto;padding:0 1rem;background:#0b0b0c;color:#e8e8ea}"
        ".card{background:#161618;border:1px solid #2a2a2e;border-radius:14px;padding:1.5rem}"
        ".act{background:#0f1830;border:1px solid #24407a;border-radius:8px;padding:.75rem 1rem;"
        "margin:1rem 0;font-family:ui-monospace,monospace;word-break:break-word}"
        "button{font-size:1rem;padding:.7rem 1.4rem;border:0;border-radius:10px;cursor:pointer;margin:.25rem .5rem .25rem 0}"
        ".ok{background:#2563eb;color:#fff}.no{background:#3a1d1d;color:#f3b4b4}"
        '</style></head><body><div class="card">'
        f"{body_html}</div></body></html>"
    )


def _approval_buttons_page(action: str) -> str:
    # Buttons POST the decision; a plain GET of the link has no side effect, so a
    # browser/chat link-prefetch can't silently approve.
    return _approval_shell(
        "Approve action",
        f"<h2>⏸ Approval requested</h2><p>An MCP tool wants to run:</p>"
        f'<div class="act">{html.escape(action)}</div>'
        '<form method="post">'
        '<button class="ok" name="decision" value="approve">✅ Approve</button>'
        '<button class="no" name="decision" value="deny">❌ Deny</button>'
        "</form>",
    )


async def approve_route(request):  # type: ignore[no-untyped-def]
    token = request.path_params["token"]
    _prune()
    rec = _PENDING.get(token)
    if rec is None:
        return HTMLResponse(
            _approval_shell(
                "Not found",
                "<h2>Link invalid or expired</h2><p>This approval link is no longer valid.</p>",
            ),
            status_code=404,
        )
    if request.method == "POST":
        # Parse the urlencoded body ourselves rather than request.form(), which needs
        # python-multipart (not a dep) and 500s without it -- this endpoint is POSTed
        # by both the human approval page's form and the in-chat widget's approve tool.
        body = urllib.parse.parse_qs((await request.body()).decode())
        decision = (body.get("decision") or [None])[0]
        if decision == "approve":
            rec["status"] = "approved"
            return HTMLResponse(
                _approval_shell(
                    "Approved",
                    "<h2>✅ Approved</h2>"
                    f'<div class="act">{html.escape(rec["action"])}</div>'
                    "<p>Head back to Claude and tell it to continue.</p>",
                )
            )
        if decision == "deny":
            rec["status"] = "denied"
            return HTMLResponse(
                _approval_shell(
                    "Denied",
                    f'<h2>❌ Denied</h2><div class="act">{html.escape(rec["action"])}</div>',
                )
            )
        return HTMLResponse(_approval_shell("Error", "<p>Unknown decision.</p>"), status_code=400)
    # GET: show buttons only (no side effect) so a link prefetch can't auto-approve.
    if rec["status"] != "pending":
        return HTMLResponse(
            _approval_shell("Already decided", f"<h2>Already {html.escape(rec['status'])}</h2>")
        )
    return HTMLResponse(_approval_buttons_page(rec["action"]))


# ---------------------------------------------------------------------------
# Slack as THE out-of-band channel: the card is how the human learns an approval
# exists (the chat gets a status only, never a link). The approval page is still
# linked from the card as a fallback if the interactivity webhook is down.
# ---------------------------------------------------------------------------
def _slack_enabled() -> bool:
    return bool(os.getenv("SLACK_BOT_TOKEN") and os.getenv("SLACK_APPROVAL_CHANNEL"))


async def _slack_post_approval(token: str, action: str, source: str) -> bool:
    """Post an interactive Approve/Deny message to Slack.

    Returns True only when Slack accepted the message -- i.e. a human was actually
    notified. Never raises: any failure is logged and reported as False so the gate
    can tell the model the request did NOT reach anyone.
    """
    if not _slack_enabled():
        return False
    blocks = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"⏸ *Approval requested* — `{source}`\n>{action}"},
        },
        {
            "type": "actions",
            "block_id": f"approval:{token}",
            "elements": [
                {
                    "type": "button",
                    "style": "primary",
                    "action_id": "approve",
                    "text": {"type": "plain_text", "text": "✅ Approve"},
                    "value": token,
                },
                {
                    "type": "button",
                    "style": "danger",
                    "action_id": "deny",
                    "text": {"type": "plain_text", "text": "❌ Deny"},
                    "value": token,
                },
            ],
        },
    ]
    if os.getenv("APPROVAL_PUBLIC_URL"):
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"<{_approve_link(token)}|Open approval page> if the buttons fail",
                    }
                ],
            }
        )
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                "https://slack.com/api/chat.postMessage",
                headers={"Authorization": f"Bearer {os.environ['SLACK_BOT_TOKEN']}"},
                json={
                    "channel": os.environ["SLACK_APPROVAL_CHANNEL"],
                    "text": f"Approval requested: {action}",  # notification fallback text
                    "blocks": blocks,
                },
            )
        data = resp.json()
        if not data.get("ok"):
            log.error("Slack chat.postMessage failed: %s", data.get("error"))
            return False
        return True
    except Exception:  # noqa: BLE001
        log.exception("Slack approval post failed")
        return False


def _verify_slack_signature(timestamp: str, body: bytes, signature: str) -> bool:
    """Verify Slack's request signature (HMAC-SHA256 over v0:timestamp:body)."""
    secret = os.getenv("SLACK_SIGNING_SECRET", "")
    if not (secret and timestamp and signature):
        return False
    try:
        if abs(time.time() - int(timestamp)) > 300:  # reject stale/replayed requests
            return False
    except ValueError:
        return False
    basestring = b"v0:" + timestamp.encode() + b":" + body
    expected = "v0=" + hmac.new(secret.encode(), basestring, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


async def slack_interact(request):  # type: ignore[no-untyped-def]
    raw = await request.body()
    if not _verify_slack_signature(
        request.headers.get("X-Slack-Request-Timestamp", ""),
        raw,
        request.headers.get("X-Slack-Signature", ""),
    ):
        return PlainTextResponse("bad signature", status_code=403)

    form = urllib.parse.parse_qs(raw.decode())
    payload = json.loads((form.get("payload") or ["{}"])[0])
    actions = payload.get("actions") or []
    response_url = payload.get("response_url")
    if not actions:
        return Response(status_code=200)
    action_id = actions[0].get("action_id")
    token = actions[0].get("value")

    _prune()
    rec = _PENDING.get(token)
    if rec is None:
        msg = "⚠️ This approval link has expired."
    elif action_id == "approve":
        rec["status"] = "approved"
        msg = f"✅ *Approved*\n>{rec['action']}\n\nReturn to Claude and tell it to continue."
    elif action_id == "deny":
        rec["status"] = "denied"
        msg = f"❌ *Denied*\n>{rec['action']}"
    else:
        msg = "Unknown action."

    # Replace the original message in place (removes the buttons).
    if response_url:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(response_url, json={"replace_original": True, "text": msg})
        except Exception:  # noqa: BLE001
            log.exception("Slack response_url update failed")
    return Response(status_code=200)


# ---------------------------------------------------------------------------
# Discord provider: same shape as Slack -- post a card with buttons, receive
# the click on a signed public webhook. Discord signs interactions with the
# app's Ed25519 key and validates the endpoint (a PING plus a deliberately bad
# signature) the moment its URL is saved, so the sidecar must be live first.
# ---------------------------------------------------------------------------
def _discord_enabled() -> bool:
    return bool(
        os.getenv("DISCORD_BOT_TOKEN")
        and os.getenv("DISCORD_APPROVAL_CHANNEL_ID")
        and os.getenv("DISCORD_PUBLIC_KEY")
    )


async def _discord_post_approval(token: str, action: str, source: str) -> bool:
    """Post an Approve/Deny card to the Discord channel. Same contract as Slack:
    True only when Discord accepted the message; never raises."""
    if not _discord_enabled():
        return False
    content = f"⏸ **Approval requested** — `{source}`\n> {action}"
    if os.getenv("APPROVAL_PUBLIC_URL"):
        # <> suppresses the link preview; the page is the fallback if buttons fail.
        content += f"\n-# <{_approve_link(token)}> — approval page fallback"
    payload = {
        "content": content,
        "components": [
            {
                "type": 1,  # action row
                "components": [
                    {"type": 2, "style": 3, "label": "✅ Approve", "custom_id": f"approve:{token}"},
                    {"type": 2, "style": 4, "label": "❌ Deny", "custom_id": f"deny:{token}"},
                ],
            }
        ],
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                "https://discord.com/api/v10/channels/"
                f"{os.environ['DISCORD_APPROVAL_CHANNEL_ID']}/messages",
                headers={"Authorization": f"Bot {os.environ['DISCORD_BOT_TOKEN']}"},
                json=payload,
            )
        if resp.status_code // 100 != 2:
            log.error("Discord message post failed: %s %s", resp.status_code, resp.text[:300])
            return False
        return True
    except Exception:  # noqa: BLE001
        log.exception("Discord approval post failed")
        return False


def _verify_discord_signature(timestamp: str, body: bytes, signature: str) -> bool:
    """Verify Discord's Ed25519 interaction signature (timestamp + body)."""
    key = os.getenv("DISCORD_PUBLIC_KEY", "")
    if not (key and timestamp and signature):
        return False
    try:
        VerifyKey(bytes.fromhex(key)).verify(timestamp.encode() + body, bytes.fromhex(signature))
        return True
    except (BadSignatureError, ValueError):
        return False


async def discord_interact(request):  # type: ignore[no-untyped-def]
    raw = await request.body()
    if not _verify_discord_signature(
        request.headers.get("X-Signature-Timestamp", ""),
        raw,
        request.headers.get("X-Signature-Ed25519", ""),
    ):
        # 401 is what Discord's endpoint validation expects for its bad-signature probe.
        return PlainTextResponse("bad signature", status_code=401)

    payload = json.loads(raw or "{}")
    if payload.get("type") == 1:  # PING (endpoint validation) -> PONG
        return JSONResponse({"type": 1})
    if payload.get("type") != 3:  # only button clicks (MESSAGE_COMPONENT) decide
        return JSONResponse(
            {"type": 4, "data": {"content": "Unsupported interaction.", "flags": 64}}
        )

    action_id, _, token = (payload.get("data", {}).get("custom_id") or "").partition(":")
    _prune()
    rec = _PENDING.get(token)
    if rec is None:
        msg = "⚠️ This approval has expired."
    elif action_id == "approve":
        rec["status"] = "approved"
        msg = f"✅ **Approved**\n> {rec['action']}\n\nReturn to Claude and tell it to continue."
    elif action_id == "deny":
        rec["status"] = "denied"
        msg = f"❌ **Denied**\n> {rec['action']}"
    else:
        msg = "Unknown action."
    # Type 7 = UPDATE_MESSAGE: replace the card in place (removes the buttons).
    return JSONResponse({"type": 7, "data": {"content": msg, "components": []}})


# ---------------------------------------------------------------------------
# Telegram provider: a BOT posts the card and receives clicks as callback_query
# updates on a webhook. Unlike Slack/Discord, Telegram does not sign the request
# body; setWebhook registers a secret_token it echoes in a header on every update,
# and that shared secret is the gate. The bot's identity is separate from the
# telegram TOOL's user session -- but if the tool's account can see the approval
# chat, the agent can read the card, so keep approvals in a chat that account is
# not in (see env.example).
# ---------------------------------------------------------------------------
def _telegram_enabled() -> bool:
    return bool(
        os.getenv("TELEGRAM_APPROVAL_BOT_TOKEN")
        and os.getenv("TELEGRAM_APPROVAL_CHAT_ID")
        and os.getenv("TELEGRAM_APPROVAL_WEBHOOK_SECRET")
    )


async def _telegram_call(client, bot_token: str, method: str, payload: dict):  # type: ignore[no-untyped-def]
    return await client.post(f"https://api.telegram.org/bot{bot_token}/{method}", json=payload)


async def _telegram_post_approval(token: str, action: str, source: str) -> bool:
    """Post an Approve/Deny card to the Telegram chat. Same contract as the others:
    True only when Telegram accepted the message; never raises."""
    if not _telegram_enabled():
        return False
    # Plain text (no parse_mode): the action is arbitrary tool-call text and would
    # routinely break Telegram's strict Markdown entity parser -> a 400 and an
    # UNDELIVERED card. Delivery is load-bearing here (an undelivered card fails
    # loud), so robustness beats formatting; Telegram auto-links the bare URL.
    text = f"⏸ Approval requested — {source}\n> {action}"
    if os.getenv("APPROVAL_PUBLIC_URL"):
        text += f"\nApproval page (fallback if the buttons fail): {_approve_link(token)}"
    payload = {
        "chat_id": os.environ["TELEGRAM_APPROVAL_CHAT_ID"],
        "text": text,
        "reply_markup": {
            "inline_keyboard": [
                [
                    {"text": "✅ Approve", "callback_data": f"approve:{token}"},
                    {"text": "❌ Deny", "callback_data": f"deny:{token}"},
                ]
            ]
        },
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await _telegram_call(
                client, os.environ["TELEGRAM_APPROVAL_BOT_TOKEN"], "sendMessage", payload
            )
        data = resp.json()
        if not data.get("ok"):
            log.error("Telegram sendMessage failed: %s", data.get("description"))
            return False
        return True
    except Exception:  # noqa: BLE001
        log.exception("Telegram approval post failed")
        return False


def _verify_telegram_secret(secret_header: str) -> bool:
    """Verify Telegram's webhook secret token.

    Telegram doesn't sign the update body (no HMAC/Ed25519 like Slack/Discord):
    setWebhook registers a secret_token that Telegram echoes in the
    X-Telegram-Bot-Api-Secret-Token header on every update. It's a shared secret,
    a weaker guarantee than a per-request signature, but it is the mechanism
    Telegram provides -- and the tunnel serves this endpoint over HTTPS only, so
    the secret stays confidential in transit.
    """
    secret = os.getenv("TELEGRAM_APPROVAL_WEBHOOK_SECRET", "")
    if not (secret and secret_header):
        return False
    return hmac.compare_digest(secret, secret_header)


async def telegram_interact(request):  # type: ignore[no-untyped-def]
    if not _verify_telegram_secret(request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")):
        return PlainTextResponse("bad secret", status_code=403)

    payload = json.loads(await request.body() or "{}")
    cq = payload.get("callback_query")
    if not cq:  # only inline-button clicks decide; ignore any other update type
        return Response(status_code=200)
    action_id, _, token = (cq.get("data") or "").partition(":")

    _prune()
    rec = _PENDING.get(token)
    if rec is None:
        msg = "⚠️ This approval has expired."
    elif action_id == "approve":
        rec["status"] = "approved"
        msg = f"✅ Approved\n> {rec['action']}\n\nReturn to Claude and tell it to continue."
    elif action_id == "deny":
        rec["status"] = "denied"
        msg = f"❌ Denied\n> {rec['action']}"
    else:
        msg = "Unknown action."

    # Separate API calls (not a reply-in-webhook-body): clear the button's spinner,
    # then replace the message text and drop the inline keyboard. Best-effort -- the
    # decision above is already recorded, so a failed edit doesn't lose it.
    bot_token = os.getenv("TELEGRAM_APPROVAL_BOT_TOKEN", "")
    message = cq.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    message_id = message.get("message_id")
    if bot_token:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await _telegram_call(
                    client, bot_token, "answerCallbackQuery", {"callback_query_id": cq.get("id")}
                )
                if chat_id is not None and message_id is not None:
                    await _telegram_call(
                        client,
                        bot_token,
                        "editMessageText",
                        {
                            "chat_id": chat_id,
                            "message_id": message_id,
                            "text": msg,
                            "reply_markup": {"inline_keyboard": []},
                        },
                    )
        except Exception:  # noqa: BLE001
            log.exception("Telegram callback update failed")
    return Response(status_code=200)


# ---------------------------------------------------------------------------
# Tool catalog + modes: the sidecar is the SOLE authority on every tool's mode
# ("always_allow" | "needs_approval" | "blocked"). Each approval-enabled server
# registers its full tool catalog here (its middleware posts it at tools/list time),
# and the operator's explicit per-(source, tool) choices are stored alongside it.
# A tool with no stored mode is always_allow -- the deliberate ship-open default;
# there is NO code-side allowlist anywhere else. "blocked" = the server refuses the
# call outright and filters the tool from Claude's tools/list; the gatekeeper's
# catalog view still shows it. State persists to APPROVAL_STATE_FILE (unset =
# memory-only: choices are lost on restart -- fine for dev, set it in any real
# deploy). Writers are the gatekeeper's set_gating tool and (later) the permissions
# widget; PINNED entries are immutable at runtime -- changing one takes a code
# change right here, by design.
_PINNED = {("gatekeeper", "set_gating"): "needs_approval"}  # the gate-changer stays human-gated
# The gatekeeper's own tools are not runtime-manageable at all: set_gating is pinned
# above, and manage_tools is inherently human-in-the-loop (nothing changes without the
# user's click on Save in the panel). The source is omitted from the manage panel,
# every mode write against it is refused, and stored modes for it are ignored --
# changing how the gatekeeper itself behaves takes a code change, by design.
_UNMANAGED_SOURCES = {"gatekeeper"}
_MODES = {"always_allow", "needs_approval", "blocked"}
_DEFAULT_MODE = "always_allow"
_STATE: dict[str, dict] = {}  # source -> {"catalog": {tool: {...}}, "modes": {tool: mode}}


def _state_file() -> str:
    return os.getenv("APPROVAL_STATE_FILE", "")


def _load_state() -> None:
    path = _state_file()
    if path and os.path.exists(path):
        with open(path) as f:
            data = json.load(f)
        _STATE.clear()
        _STATE.update(data)


def _save_state() -> None:
    path = _state_file()
    if not path:
        return
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(_STATE, f)
    os.replace(tmp, path)  # atomic: a crash mid-write never corrupts the state


def _source_state(source: str) -> dict:
    return _STATE.setdefault(source, {"catalog": {}, "modes": {}})


def _effective_modes(source: str) -> dict[str, str]:
    """Stored choices with the code-pinned entries stamped on top (pins always win).
    An unmanaged source takes no stored choices at all -- code is its only authority."""
    if source in _UNMANAGED_SOURCES:
        modes = {}
    else:
        modes = dict((_STATE.get(source) or {}).get("modes") or {})
    for (src, tool), mode in _PINNED.items():
        if src == source:
            modes[tool] = mode
    return modes


async def catalog(request):  # type: ignore[no-untyped-def]
    if request.method == "POST":
        body = await request.json()
        source = body["source"]
        st = _source_state(source)
        st["catalog"] = {
            # read_only follows the MCP spec default Claude's UI applies: only an
            # explicit readOnlyHint=true is read-only; false, absent, or a legacy
            # null all mean write/delete.
            t["name"]: {
                "description": t.get("description", ""),
                "read_only": t.get("read_only") is True,
            }
            for t in body.get("tools") or []
        }
        # Two origins (see middleware.register_catalog): "startup" = the deployed
        # server announcing itself; "list" = a real authenticated client asked, which
        # is the ONLY thing that counts as the source being USED (the panel's
        # "last used" label). Absent origin (older middleware) counts as list.
        st["registered"] = time.time()
        if body.get("origin", "list") == "list":
            st["seen"] = time.time()
        _save_state()
        n = len(st["catalog"])
        log.info("catalog registered: %s (%d tools, origin=%s)", source, n, body.get("origin"))
        return JSONResponse({"ok": True, "source": source, "count": n})
    source = request.query_params.get("source", "")
    modes = _effective_modes(source)
    tools = {
        name: {**info, "mode": modes.get(name, _DEFAULT_MODE)}
        for name, info in ((_STATE.get(source) or {}).get("catalog") or {}).items()
    }
    return JSONResponse({"source": source, "tools": tools})


async def gating(request):  # type: ignore[no-untyped-def]
    if request.method == "GET":
        source = request.query_params.get("source", "")
        return JSONResponse({"source": source, "modes": _effective_modes(source)})
    body = await request.json()
    source, tool, mode = body["source"], body["tool"], body.get("mode")
    if source in _UNMANAGED_SOURCES:
        return JSONResponse(
            {"ok": False, "error": f"{source} manages the gates; its own tools are fixed in code"},
            status_code=403,
        )
    if (source, tool) in _PINNED:
        return JSONResponse(
            {"ok": False, "error": f"{tool} is pinned to {_PINNED[(source, tool)]} in code"},
            status_code=403,
        )
    if mode not in _MODES:
        return JSONResponse(
            {"ok": False, "error": f"mode must be one of {sorted(_MODES)}"},
            status_code=400,
        )
    _source_state(source)["modes"][tool] = mode
    _save_state()
    log.info("mode set: %s/%s = %s", source, tool, mode)
    return JSONResponse({"ok": True, "source": source, "modes": _effective_modes(source)})


# ---------------------------------------------------------------------------
# Manage sessions: capability tokens for the in-chat tool-permissions widget.
# Minting (bare POST /manage) is INTERNAL-ONLY -- the tunnel path-routes only
# /manage/<token> to the public internet -- so only the gatekeeper's manage_tools
# tool can start a session; the widget in the user's browser redeems it. The
# human's click on Save IS the authorization (the model can't make HTTP requests),
# which is why a save needs no approval card. Pins still hold: a change to a
# _PINNED entry is refused here too. Tokens share the approval TTL and are ONE-SHOT:
# a successful save consumes the session, because the panel's snapshot is stale the
# moment modes change -- further edits go through a fresh manage_tools call (and a
# fresh view of the current state).
_MANAGE: dict[str, dict] = {}  # token -> {"source": str, "created": float}

# The token in the URL is the credential, so any browser origin may call these
# two routes (the widget iframe's origin varies by host).
_MANAGE_CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "content-type",
}


def _prune_manage() -> None:
    now = time.time()
    for token in [t for t, r in _MANAGE.items() if now - r["created"] > _TTL_SECONDS]:
        _MANAGE.pop(token, None)


async def manage_mint(request):  # type: ignore[no-untyped-def]
    _prune_manage()
    token = secrets.token_urlsafe(24)
    # A session spans EVERY registered connector (the widget shows one section per
    # source), so the record carries no source.
    _MANAGE[token] = {"created": time.time()}
    return JSONResponse({"ok": True, "token": token})


async def manage_session(request):  # type: ignore[no-untyped-def]
    if request.method == "OPTIONS":
        return Response(status_code=204, headers=_MANAGE_CORS)
    _prune_manage()
    if _MANAGE.get(request.path_params["token"]) is None:
        return JSONResponse(
            {"ok": False, "error": "expired"}, status_code=404, headers=_MANAGE_CORS
        )
    if request.method == "POST":
        # {"changes": {source: {tool: mode}}, "forget": [source, ...]} -- one save can
        # span connectors, and can drop a connector's stored state entirely (its
        # catalog, modes, and timestamps; a still-deployed server simply re-registers
        # on its next healthz probe, a removed one stays gone).
        body = await request.json()
        changes = body.get("changes") or {}
        forget = body.get("forget") or []
        bad = [
            f"{src}/{tool}={mode}"
            for src, tools in changes.items()
            for tool, mode in tools.items()
            if mode not in _MODES
        ]
        if bad:
            return JSONResponse(
                {"ok": False, "error": f"invalid modes: {bad}"},
                status_code=400,
                headers=_MANAGE_CORS,
            )
        refused: dict[str, list[str]] = {}
        applied = 0
        for src, tools in changes.items():
            if src in forget:  # forgetting wins; mode edits to it are moot
                continue
            modes = _source_state(src)["modes"]
            for tool, mode in tools.items():
                if src in _UNMANAGED_SOURCES or (src, tool) in _PINNED:
                    refused.setdefault(src, []).append(tool)
                else:
                    modes[tool] = mode
                    applied += 1
        forgotten = []
        for src in forget:
            if src in _UNMANAGED_SOURCES:
                refused.setdefault(src, []).append("*forget*")
            elif _STATE.pop(src, None) is not None:
                forgotten.append(src)
                applied += 1
        _save_state()
        _MANAGE.pop(request.path_params["token"], None)  # one-shot: the view is stale now
        log.info("manage save: %d applied, forgot=%s, refused=%s", applied, forgotten, refused)
        return JSONResponse(
            {"ok": True, "applied": applied, "forgotten": forgotten, "refused": refused},
            headers=_MANAGE_CORS,
        )
    # GET: the widget's data source -- every connector with a registered catalog,
    # each with the same catalog+modes view the gatekeeper reads. The gatekeeper
    # itself is absent by design: its tools aren't manageable.
    sources = {}
    for src, st in _STATE.items():
        catalog = st.get("catalog") or {}
        if src in _UNMANAGED_SOURCES or not catalog:
            continue
        modes = _effective_modes(src)
        sources[src] = {
            "tools": {
                name: {**info, "mode": modes.get(name, _DEFAULT_MODE)}
                for name, info in catalog.items()
            },
            "pinned": [tool for (s, tool) in _PINNED if s == src],
            # Epoch seconds of the last CLIENT tools/list (null = never): the panel's
            # "last used" label. Startup re-registration deliberately doesn't count.
            "last_seen": st.get("seen"),
        }
    return JSONResponse({"ok": True, "sources": sources}, headers=_MANAGE_CORS)


async def healthz(request):  # type: ignore[no-untyped-def]
    provider = _provider()
    return JSONResponse(
        {
            "ok": provider in _PROVIDERS_IMPLEMENTED,
            "provider": provider,
            "channel": "configured" if _channel_configured() else "unconfigured",
            "public_url_set": bool(os.getenv("APPROVAL_PUBLIC_URL")),
            "pending": len(_PENDING),
        }
    )


app = Starlette(
    routes=[
        Route("/gate", gate, methods=["POST"]),
        Route("/approve/{token}", approve_route, methods=["GET", "POST"]),
        Route("/slack/interact", slack_interact, methods=["POST"]),
        Route("/discord/interact", discord_interact, methods=["POST"]),
        Route("/telegram/interact", telegram_interact, methods=["POST"]),
        Route("/gating", gating, methods=["GET", "POST"]),
        Route("/catalog", catalog, methods=["GET", "POST"]),
        Route("/manage", manage_mint, methods=["POST"]),
        Route("/manage/{token}", manage_session, methods=["GET", "POST", "OPTIONS"]),
        Route("/healthz", healthz, methods=["GET"]),
    ]
)


if __name__ == "__main__":
    import uvicorn

    # Fail fast on a typo'd/unimplemented provider rather than booting a sidecar
    # that silently reports every approval undeliverable.
    if _provider() not in _PROVIDERS_IMPLEMENTED:
        raise SystemExit(
            f"APPROVAL_PROVIDER={_provider()!r} is not implemented "
            f"(implemented: {sorted(_PROVIDERS_IMPLEMENTED)}; "
            f"planned: {sorted(_PROVIDERS_PLANNED)})"
        )
    _load_state()
    if not _state_file():
        log.warning("APPROVAL_STATE_FILE unset -- tool modes are memory-only, lost on restart")
    uvicorn.run(
        app,
        host=os.getenv("APPROVAL_HOST", "127.0.0.1"),
        port=int(os.getenv("APPROVAL_PORT", "8072")),
    )
