"""Out-of-band human-in-the-loop approval for mcp-tools servers.

claude.ai gives us no reliable in-chat gate: tool-approval is sticky (approve once
and it's approved across every chat; the connector "needs approval" setting stops
applying), and MCP elicitation dialogs don't render for custom connectors (tested).
So a gated tool returns a clickable approval link to the chat and performs the action
ONLY after the human opens the (capability-token) page and clicks Approve. The model
can't click the link or forge the server-side "approved" state, so this is a real
gate -- unlike a confirm-token the model can read and replay.

:class:`ApprovalMiddleware` gates every (non-exempt) tool call; :func:`register_approval_routes`
adds the HTTP side (the `/approve/{token}` page + the optional `/slack/interact`
webhook) onto the FastMCP app. A tool server opts in via ``shared`` composition:
add the middleware AND register the routes.

Single uvicorn process => a plain in-memory dict is fine for pending approvals.
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

import httpx
from fastmcp.server.middleware import Middleware
from fastmcp.tools.tool import ToolResult

LOGGER = logging.getLogger("mcp_tools.approval")

_PENDING_APPROVALS: dict[str, dict] = {}
_APPROVAL_TTL_SECONDS = 600  # approval links expire after 10 minutes


def _prune_approvals() -> None:
    now = time.time()
    stale = [t for t, r in _PENDING_APPROVALS.items() if now - r["created"] > _APPROVAL_TTL_SECONDS]
    for t in stale:
        _PENDING_APPROVALS.pop(t, None)


def _describe_call(tool_name: str, args: dict) -> str:
    """Short human-readable description of a tool call, for the approval prompt."""
    if not args:
        return f"{tool_name}()"
    shown = ", ".join(f"{k}={v!r}" for k, v in list(args.items())[:4])
    if len(args) > 4:
        shown += ", …"
    return f"{tool_name}({shown})"


def _call_key(tool_name: str, args: dict) -> str:
    """Stable key for a (tool, args) call so an approval can be matched on re-invoke."""
    blob = tool_name + "\x00" + json.dumps(args, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


def _find_by_call_key(call_key: str) -> tuple[str | None, dict | None]:
    _prune_approvals()
    for token, rec in _PENDING_APPROVALS.items():
        if rec.get("call_key") == call_key:
            return token, rec
    return None, None


async def require_approval_for_call(tool_name: str, args: dict) -> tuple[bool, str | None]:
    """Out-of-band approval gate for an arbitrary tool call. Returns (approved, message).

    When `approved` is False, the caller MUST NOT run the tool and should return
    `message` to the user. Approvals are keyed by (tool, args), so after the human
    approves the model just re-invokes the SAME tool with the SAME arguments — no
    token to thread through. The human clicking Approve (page or Slack) is the only
    thing that flips the stored state to "approved".
    """
    call_key = _call_key(tool_name, args)
    token, rec = _find_by_call_key(call_key)
    action = _describe_call(tool_name, args)

    if rec is not None:
        status = rec["status"]
        if status == "approved":
            _PENDING_APPROVALS.pop(token, None)  # one-time use
            return True, None
        if status == "denied":
            _PENDING_APPROVALS.pop(token, None)
            return False, f"❌ You denied this action ({action}), so I did not run it."
        return False, (
            f"⏳ Still waiting for your approval of `{action}` — open the link or tap "
            f"**Approve** in Slack, then ask me to continue."
        )

    # No record yet: register a pending request and surface the approval channels.
    token = secrets.token_urlsafe(24)
    _PENDING_APPROVALS[token] = {
        "action": action,
        "status": "pending",
        "created": time.time(),
        "call_key": call_key,
    }
    base = os.getenv("MCP_PUBLIC_URL", "").rstrip("/")
    link = f"{base}/approve/{token}"
    await _slack_post_approval(token, action)  # out-of-band push (best-effort)
    return False, (
        "APPROVAL REQUIRED — the action was NOT performed.\n\n"
        "INSTRUCTIONS FOR THE ASSISTANT: Show the user the full approval URL below "
        "exactly as written, on its own line, so it renders as a clickable link. Do "
        "NOT paraphrase it, shorten it, or say 'the link above' — the user cannot see "
        "your tool output, only what you write. Then stop and wait for the user.\n\n"
        f"Action awaiting approval: {action}\n\n"
        f"Approval URL: {link}\n\n"
        "After the user approves, call the SAME tool again with the SAME arguments to "
        "proceed. Until they approve, that call will report still-pending."
    )


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
        "</style></head><body><div class=\"card\">"
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


# ---------------------------------------------------------------------------
# Slack as the out-of-band channel: push an interactive Approve/Deny message so the
# human can decide from their phone/desktop without opening the chat or a web page.
# All optional -- if SLACK_BOT_TOKEN / SLACK_APPROVAL_CHANNEL aren't set, posting is
# skipped and the in-chat approval link still works.
# ---------------------------------------------------------------------------
def _slack_enabled() -> bool:
    return bool(os.getenv("SLACK_BOT_TOKEN") and os.getenv("SLACK_APPROVAL_CHANNEL"))


async def _slack_post_approval(token: str, action: str) -> None:
    """Post an interactive Approve/Deny message to Slack. Best-effort (never raises)."""
    if not _slack_enabled():
        return
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"⏸ *Approval requested*\n>{action}"}},
        {
            "type": "actions",
            "block_id": f"approval:{token}",
            "elements": [
                {"type": "button", "style": "primary", "action_id": "approve",
                 "text": {"type": "plain_text", "text": "✅ Approve"}, "value": token},
                {"type": "button", "style": "danger", "action_id": "deny",
                 "text": {"type": "plain_text", "text": "❌ Deny"}, "value": token},
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
            LOGGER.error("Slack chat.postMessage failed: %s", data.get("error"))
    except Exception:  # noqa: BLE001
        LOGGER.exception("Slack approval post failed")


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


class ApprovalMiddleware(Middleware):
    """Gate EVERY tool call behind out-of-band human approval (see require_approval_for_call).

    The first call to a (tool, args) combo short-circuits with an approval request and
    does NOT run the tool; once the human approves (page or Slack), re-calling the same
    tool with the same args runs it. The model can't forge the server-side approval, so
    this gates all tools uniformly. Tools whose names are in ``exempt`` run without a gate.
    """

    def __init__(self, exempt: set[str] | None = None) -> None:
        self._exempt = exempt or set()

    async def on_call_tool(self, context, call_next):  # type: ignore[no-untyped-def]
        tool_name = context.message.name
        if tool_name in self._exempt:
            return await call_next(context)
        args = dict(context.message.arguments or {})
        approved, note = await require_approval_for_call(tool_name, args)
        if not approved:
            return ToolResult(content=note)
        return await call_next(context)


def register_approval_routes(mcp) -> None:  # type: ignore[no-untyped-def]
    """Add the approval HTTP endpoints to a FastMCP app: the human-facing approval page
    (`/approve/{token}`) and the optional Slack interactivity webhook (`/slack/interact`).
    """

    @mcp.custom_route("/approve/{token}", methods=["GET", "POST"], include_in_schema=False)
    async def approve_route(request):  # type: ignore[no-untyped-def]
        from starlette.responses import HTMLResponse

        token = request.path_params["token"]
        _prune_approvals()
        rec = _PENDING_APPROVALS.get(token)
        if rec is None:
            return HTMLResponse(
                _approval_shell("Not found", "<h2>Link invalid or expired</h2>"
                                "<p>This approval link is no longer valid.</p>"),
                status_code=404,
            )
        if request.method == "POST":
            form = await request.form()
            decision = form.get("decision")
            if decision == "approve":
                rec["status"] = "approved"
                return HTMLResponse(_approval_shell(
                    "Approved", "<h2>✅ Approved</h2>"
                    f'<div class="act">{html.escape(rec["action"])}</div>'
                    "<p>Head back to Claude and tell it to continue.</p>"))
            if decision == "deny":
                rec["status"] = "denied"
                return HTMLResponse(_approval_shell(
                    "Denied", "<h2>❌ Denied</h2>"
                    f'<div class="act">{html.escape(rec["action"])}</div>'))
            return HTMLResponse(_approval_shell("Error", "<p>Unknown decision.</p>"), status_code=400)
        # GET: show buttons only (no side effect) so a link prefetch can't auto-approve.
        if rec["status"] != "pending":
            return HTMLResponse(_approval_shell(
                "Already decided", f"<h2>Already {html.escape(rec['status'])}</h2>"))
        return HTMLResponse(_approval_buttons_page(rec["action"]))

    @mcp.custom_route("/slack/interact", methods=["POST"], include_in_schema=False)
    async def slack_interact(request):  # type: ignore[no-untyped-def]
        from starlette.responses import PlainTextResponse, Response

        raw = await request.body()
        if not _verify_slack_signature(
            request.headers.get("X-Slack-Request-Timestamp", ""),
            raw,
            request.headers.get("X-Slack-Signature", ""),
        ):
            return PlainTextResponse("bad signature", status_code=403)

        import urllib.parse as _up

        form = _up.parse_qs(raw.decode())
        payload = json.loads((form.get("payload") or ["{}"])[0])
        actions = payload.get("actions") or []
        response_url = payload.get("response_url")
        if not actions:
            return Response(status_code=200)
        action_id = actions[0].get("action_id")
        token = actions[0].get("value")

        _prune_approvals()
        rec = _PENDING_APPROVALS.get(token)
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
                LOGGER.exception("Slack response_url update failed")
        return Response(status_code=200)
