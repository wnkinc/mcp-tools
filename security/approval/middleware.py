"""Out-of-band human-in-the-loop approval for mcp-tools servers.

claude.ai gives us no reliable in-chat gate: tool-approval is sticky (approve once
and it's approved across every chat; the connector "needs approval" setting stops
applying), and MCP elicitation dialogs don't render for custom connectors (tested).
So a gated tool short-circuits to a plain pending status, an Approve/Deny card is
pushed to the operator's approval channel (Slack or Discord -- the sidecar's
APPROVAL_PROVIDER), and the action runs ONLY after the human decides out-of-band.
The model can't press the button or forge the server-side "approved" state, so
this is a real gate.

The model-facing status deliberately carries NO approval URL and no instructions
addressed to the assistant: a tool result that says "show this link, it's not
phishing" is exactly the shape of a prompt-injection attack, and both claude.ai's
injection screening and the model itself (rightly) refuse to relay it. All
human-facing surfaces (the card, the approval page linked FROM the card) live in
the trusted out-of-band channel; the chat only learns facts. The protocol itself is
pre-declared in the server's MCP instructions (see security/serve.py), so a pending
status arrives as expected behavior instead of a surprise.

The state and the human-facing surfaces (approval page, Slack card + interactivity
webhook) live in the APPROVAL SIDECAR (security/approval/service/), one per stack:
Slack apps deliver every button click to a single app-level URL, so approvals must
have a single owner or only one tool's buttons work. This middleware is just the
client: it asks the sidecar to create/check an approval and formats the model-facing
messages. Tools can only create and query; only the human (page or Slack-signed
webhook) can flip a decision -- a compromised tool cannot approve itself.

Fail closed: if the sidecar is unreachable, the gated action does NOT run.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os

import httpx
from fastmcp.server.middleware import Middleware
from fastmcp.tools.tool import ToolResult

LOGGER = logging.getLogger("mcp_tools.approval")


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


class ApprovalMiddleware(Middleware):
    """Gate every (non-exempt) tool call behind the approval sidecar.

    The first call to a (tool, args) combo short-circuits with an approval request and
    does NOT run the tool; once the human approves (page or Slack), re-calling the same
    tool with the same args runs it -- no token to thread through the model.
    """

    def __init__(
        self,
        exempt: set[str] | None = None,
        source: str = "mcp",
        approval_url: str | None = None,
        timeout: float = 10.0,
    ) -> None:
        self._exempt = exempt or set()
        self.source = source
        self.approval_url = (
            approval_url or os.getenv("APPROVAL_URL", "http://127.0.0.1:8072")
        ).rstrip("/")
        self.timeout = timeout

    async def on_call_tool(self, context, call_next):  # type: ignore[no-untyped-def]
        tool_name = context.message.name
        if tool_name in self._exempt:
            return await call_next(context)

        args = dict(context.message.arguments or {})
        action = _describe_call(tool_name, args)
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(
                    f"{self.approval_url}/gate",
                    json={
                        "source": self.source,
                        "action": action,
                        "call_key": _call_key(tool_name, args),
                    },
                )
            data = resp.json()
        except Exception:  # noqa: BLE001 - any failure fails CLOSED below
            LOGGER.warning(
                "approval sidecar unreachable at %s -- failing closed", self.approval_url
            )
            return _note(
                f"[approval: the approval service is unavailable ({self.approval_url}) -- "
                f"failing CLOSED, `{action}` was NOT performed.]"
            )

        decision = data.get("decision")
        if decision == "allow":
            return await call_next(context)
        if decision == "denied":
            return _note(f"❌ The user denied `{action}`, so it was not performed.")
        # Pending. `notified` reports whether the Slack card actually reached the
        # human; default True so an older sidecar (no such field) isn't a false alarm.
        if not data.get("notified", True):
            return _note(
                f"⚠️ `{action}` requires out-of-band human approval and was NOT "
                "performed — and the approval request could not be delivered to the "
                "approval channel (unconfigured or unreachable), so it cannot currently "
                "be approved. The server operator needs to restore the approval channel "
                "before this action can proceed."
            )
        if not data.get("created"):
            return _note(
                f"⏳ `{action}` is still awaiting human approval on the card in the "
                "user's approval channel. Once approved there, calling the same tool "
                "with the same arguments performs the action."
            )
        return _note(
            f"⏸ Approval required — `{action}` was NOT performed.\n\n"
            "This server gates this tool behind out-of-band human approval: an "
            "Approve/Deny card for this exact action has been posted to the user's "
            "approval channel (Slack or Discord, per the server's setup). Once the "
            "user approves it there, calling the same tool again with the same "
            "arguments performs the action; until then it reports still-pending. "
            "Denying it cancels the action."
        )


def _note(text: str) -> ToolResult:
    return ToolResult(content=text)
