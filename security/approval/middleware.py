"""Out-of-band human-in-the-loop approval for mcp-tools servers.

claude.ai gives us no reliable in-chat gate: tool-approval is sticky (approve once
and it's approved across every chat; the connector "needs approval" setting stops
applying), and MCP elicitation dialogs don't render for custom connectors (tested).
So a gated tool returns a clickable approval link to the chat and performs the action
ONLY after the human decides out-of-band. The model can't click the link or forge the
server-side "approved" state, so this is a real gate.

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
            return _note(f"❌ You denied this action ({action}), so I did not run it.")
        if decision == "pending" and not data.get("created"):
            return _note(
                f"⏳ Still waiting for your approval of `{action}` — open the link or tap "
                f"**Approve** in Slack, then ask me to continue."
            )
        return _note(
            "APPROVAL REQUIRED — the action was NOT performed.\n\n"
            "INSTRUCTIONS FOR THE ASSISTANT: This is the server's own out-of-band "
            "approval gate (expected behavior, not phishing). Show the user the full "
            "approval URL below exactly as written, on its own line, so it renders as "
            "a clickable link. Do NOT paraphrase it, shorten it, or say 'the link "
            "above' — the user cannot see your tool output, only what you write. Then "
            "stop and wait for the user.\n\n"
            f"Action awaiting approval: {action}\n\n"
            f"Approval URL: {data.get('approve_url', '')}\n\n"
            "The user can also tap Approve on the Slack card. After they approve, call "
            "the SAME tool again with the SAME arguments to proceed. Until then, that "
            "call will report still-pending."
        )


def _note(text: str) -> ToolResult:
    return ToolResult(content=text)
