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
  POST /gate              (internal) create-or-check an approval for a tool call
  GET/POST /approve/{token}    (public via tunnel) the human approval page
  POST /slack/interact    (public via tunnel) Slack button clicks, HMAC-verified
  POST /discord/interact  (public via tunnel) Discord button clicks, Ed25519-verified
  GET /healthz            liveness + config visibility

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

# The out-of-band platform that delivers approval cards; "telegram" is planned.
# Human-in-the-loop only works if the platform is one the agent does NOT operate:
# a card the agent's tools can see and press is a gate that approves itself
# (e.g. the telegram provider while the telegram TOOL runs on the same account).
_PROVIDERS_IMPLEMENTED = {"slack", "discord"}
_PROVIDERS_PLANNED = {"telegram"}


def _provider() -> str:
    return os.getenv("APPROVAL_PROVIDER", "slack").strip().lower()


def _channel_configured() -> bool:
    """Does the ACTIVE provider have the env it needs to reach a human?"""
    return {"slack": _slack_enabled, "discord": _discord_enabled}.get(_provider(), lambda: False)()


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
        return JSONResponse({"decision": "pending", "created": False, "notified": rec["notified"]})

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
    return JSONResponse({"decision": "pending", "created": True, "notified": rec["notified"]})


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
        form = await request.form()
        decision = form.get("decision")
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
    uvicorn.run(
        app,
        host=os.getenv("APPROVAL_HOST", "127.0.0.1"),
        port=int(os.getenv("APPROVAL_PORT", "8072")),
    )
