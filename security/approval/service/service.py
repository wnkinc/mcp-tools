"""Approval sidecar: one place that owns pending approvals for EVERY tool.

Slack delivers all button clicks to a single app-level Interactivity URL, but
pending-approval state used to live per-tool (in each server process) -- so the
one tool that URL pointed at could answer clicks and every other tool's buttons
"expired". This service is the fix: tools create/query approvals here over the
internal network, and Slack + the human-facing approval page talk ONLY to this
service, which holds all the state. One-click Approve works for any number of
tools, and the Slack bot token lives in exactly one container (not in tools).

Integrity: a tool can only CREATE a pending approval and ASK its status. The
only writers of "approved"/"denied" are the Slack-signed webhook and the
capability-token page -- both human-driven. A compromised tool cannot flip its
own approvals.

Endpoints:
  POST /gate            (internal) create-or-check an approval for a tool call
  GET/POST /approve/{token}  (public via tunnel) the human approval page
  POST /slack/interact  (public via tunnel) Slack button clicks, signature-verified
  GET /healthz          liveness + config visibility

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
from starlette.applications import Starlette
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("approval")

_PENDING: dict[str, dict] = {}
_TTL_SECONDS = 600  # approval links expire after 10 minutes


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
            {"decision": "pending", "created": False, "approve_url": _approve_link(token)}
        )

    token = secrets.token_urlsafe(24)
    _PENDING[token] = {
        "key": key,
        "source": source,
        "action": action,
        "status": "pending",
        "created": time.time(),
    }
    await _slack_post_approval(token, action, source)  # out-of-band push (best-effort)
    return JSONResponse(
        {"decision": "pending", "created": True, "approve_url": _approve_link(token)}
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
# Slack as the out-of-band channel. All optional -- without SLACK_BOT_TOKEN /
# SLACK_APPROVAL_CHANNEL the page link still works, Slack is just not notified.
# ---------------------------------------------------------------------------
def _slack_enabled() -> bool:
    return bool(os.getenv("SLACK_BOT_TOKEN") and os.getenv("SLACK_APPROVAL_CHANNEL"))


async def _slack_post_approval(token: str, action: str, source: str) -> None:
    """Post an interactive Approve/Deny message to Slack. Best-effort (never raises)."""
    if not _slack_enabled():
        return
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
    except Exception:  # noqa: BLE001
        log.exception("Slack approval post failed")


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


async def healthz(request):  # type: ignore[no-untyped-def]
    return JSONResponse(
        {
            "ok": True,
            "slack": "enabled" if _slack_enabled() else "disabled",
            "public_url_set": bool(os.getenv("APPROVAL_PUBLIC_URL")),
            "pending": len(_PENDING),
        }
    )


app = Starlette(
    routes=[
        Route("/gate", gate, methods=["POST"]),
        Route("/approve/{token}", approve_route, methods=["GET", "POST"]),
        Route("/slack/interact", slack_interact, methods=["POST"]),
        Route("/healthz", healthz, methods=["GET"]),
    ]
)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host=os.getenv("APPROVAL_HOST", "127.0.0.1"),
        port=int(os.getenv("APPROVAL_PORT", "8072")),
    )
