"""Out-of-band human-in-the-loop approval for mcp-tools servers.

claude.ai gives us no reliable in-chat gate: tool-approval is sticky (approve once
and it's approved across every chat; the connector "needs approval" setting stops
applying), and MCP elicitation dialogs don't render for custom connectors (tested).
So a gated tool short-circuits to a plain pending status, an Approve/Deny card is
pushed to the operator's approval channel (Slack, Discord, or Telegram -- the
sidecar's APPROVAL_PROVIDER), and the action runs ONLY after the human decides
out-of-band.
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

import contextlib
import hashlib
import json
import logging
import os

import httpx
from fastmcp.server.middleware import Middleware
from fastmcp.tools.tool import ToolResult

from security.approval.gating import fetch_modes, mode_for

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


async def register_catalog(  # type: ignore[no-untyped-def]
    source: str, tools, approval_url: str, *, origin: str, timeout: float = 10.0
) -> None:
    """Publish the FULL tool catalog to the sidecar (the operator's unfiltered view --
    the gatekeeper and the permissions widget read it from there). Best-effort: a down
    sidecar never breaks the caller. Two origins, same payload:

    - "startup": the server announcing itself (serve()'s /healthz probe path), so the
      panel shows every DEPLOYED tool without waiting for a client. Repeats each
      probe -- idempotent, and it refills a wiped sidecar state within a probe cycle.
    - "list": a real authenticated client asked for tools/list; the sidecar also
      stamps the source's last-seen from it (the panel's "last used" label).
    """
    payload = {
        "source": source,
        "origin": origin,
        "tools": [
            {
                "name": t.name,
                "description": t.description or "",
                # Claude's connector UI groups by readOnlyHint with the MCP spec
                # default applied: only an explicit true is read-only; false OR
                # absent means write/delete. Mirror that so the panel groups
                # exactly like Claude's own permissions screen.
                "read_only": bool(t.annotations and getattr(t.annotations, "readOnlyHint", False)),
            }
            for t in tools
        ],
    }
    with contextlib.suppress(Exception):
        async with httpx.AsyncClient(timeout=timeout) as client:
            await client.post(f"{approval_url}/catalog", json=payload)


class ApprovalMiddleware(Middleware):
    """Enforce each tool's sidecar-held mode (always_allow / needs_approval / blocked).

    The sidecar is the sole authority (see security/approval/gating.py): no stored
    choice means always_allow. A needs_approval call short-circuits: the first
    (tool, args) call requests an approval and does NOT run the tool; once the human
    approves (page or card), re-calling the same tool with the same args runs it --
    no token to thread through the model.
    """

    def __init__(
        self,
        source: str = "mcp",
        approval_url: str | None = None,
        timeout: float = 10.0,
        widget: bool = False,
    ) -> None:
        self.source = source
        self.approval_url = (
            approval_url or os.getenv("APPROVAL_URL", "http://127.0.0.1:8072")
        ).rstrip("/")
        self.timeout = timeout
        # Widget mode: the in-chat approval widget IS the channel, so a pending result
        # carries the capability token (harmless -- no tool flips the gate, the model
        # can't fetch) as a JSON payload the widget reads; the tool is tagged elsewhere
        # (WidgetMetaMiddleware) so this result renders the card. No out-of-band card needed.
        self._widget = widget

    async def on_list_tools(self, context, call_next):  # type: ignore[no-untyped-def]
        # This is the one place the full list passes by: register it as the catalog,
        # then filter blocked tools from what Claude sees. The connector caches
        # tools/list until refreshed, so the filter is cosmetic latency-wise -- the
        # call-time refusal below is the actual gate. origin="list" doubles as the
        # liveness beacon: the sidecar stamps the source's last-seen from it (a real,
        # authenticated client asked), which the manage panel shows as "last used".
        tools = await call_next(context)
        await register_catalog(
            self.source, tools, self.approval_url, origin="list", timeout=self.timeout
        )
        modes = await fetch_modes(self.source, self.approval_url)
        if modes is None:  # nothing known -> can't tell what's blocked; calls fail closed
            return tools
        return [t for t in tools if mode_for(t.name, modes) != "blocked"]

    async def on_call_tool(self, context, call_next):  # type: ignore[no-untyped-def]
        tool_name = context.message.name
        # Live sidecar modes (the gatekeeper flips them at runtime). A fetch blip
        # keeps the last-known; a never-answered sidecar means everything below
        # treats the tool as needs_approval and the gate itself fails closed.
        modes = await fetch_modes(self.source, self.approval_url)
        mode = mode_for(tool_name, modes)
        if mode == "always_allow":
            return await call_next(context)
        if mode == "blocked":
            # Disabled outright: no approval path, and a stale client tool list must
            # not be able to run it. Same wording rules as pending: bare facts only.
            return _note(
                f"⛔ `{tool_name}` has been disabled by the server operator, so this "
                "call was not performed. It cannot be approved; the operator would "
                "have to re-enable the tool first."
            )

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

        # Widget mode: the model reads the SAME explicit prose as the non-widget path
        # (that's what makes it reliably re-call after approval, per main), and the token
        # for the in-chat card rides an HTML comment the model ignores and the widget
        # parses. Do NOT swap this for a JSON blob -- terse JSON + a visible widget makes
        # the model assume the card executes the action and claim premature success.
        if self._widget:
            marker = json.dumps({"token": data.get("token", ""), "action": action})
            return _note(
                f"⏸ Approval required — `{action}` was NOT performed and has NOT been sent. "
                "An Approve/Deny card is shown in the chat. This action performs ONLY when "
                "you call this same tool again with the same arguments AFTER the user "
                "approves it in the card and tells you to continue; until then it stays "
                "pending. Do NOT tell the user it was done until that second call succeeds.\n"
                f"<!--APPROVAL {marker}-->"
            )

        # Pending. `notified` reports whether the card actually reached the human;
        # default True so an older sidecar (no such field) isn't a false alarm.
        # `channel_label` names the ACTIVE provider (e.g. "Telegram") so the message
        # matches reality instead of listing platforms or guessing; the sidecar owns
        # APPROVAL_PROVIDER, the tool doesn't, so it comes back on the gate response.
        # Empty (older sidecar) => the generic phrasing, which still reads fine.
        label = data.get("channel_label") or ""
        where = f"{label} " if label else ""
        if not data.get("notified", True):
            return _note(
                f"⚠️ `{action}` requires out-of-band human approval and was NOT "
                f"performed — and the approval request could not be delivered to the "
                f"{where}approval channel (unconfigured or unreachable), so it cannot "
                "currently be approved. The server operator needs to restore the "
                "approval channel before this action can proceed."
            )
        if not data.get("created"):
            return _note(
                f"⏳ `{action}` is still awaiting human approval on the card in the "
                f"user's {where}approval channel. Once approved there, calling the same "
                "tool with the same arguments performs the action."
            )
        return _note(
            f"⏸ Approval required — `{action}` was NOT performed.\n\n"
            "This server gates this tool behind out-of-band human approval: an "
            f"Approve/Deny card for this exact action has been posted to the user's "
            f"{where}approval channel. Once the user approves it there, calling the "
            "same tool again with the same arguments performs the action; until then "
            "it reports still-pending. Denying it cancels the action."
        )


def _note(text: str) -> ToolResult:
    return ToolResult(content=text)
