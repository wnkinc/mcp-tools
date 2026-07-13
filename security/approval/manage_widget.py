"""In-chat tool-permissions widget: the gatekeeper's management UI.

manage_tools() renders widgets/manage.html in the chat -- a replica of Claude's
connector permissions screen (grouped Interactive/Read-only tools, per-tool
always_allow / needs_approval / blocked control, group blanket menu) -- and the
user's Save POSTs the batch of changes DIRECTLY to the sidecar with a capability
token this tool minted server-side. The human's click is the authorization, so a
save needs no approval card: the model can't make HTTP requests, minting is
internal-only (the tunnel exposes only /manage/<token>, never the bare mint), and
the sidecar's code pins hold regardless (set_gating can't be freed from here).
"""

from __future__ import annotations

import json
import os

import httpx

from security.approval.widget_spike import _public_base, widget_html, widget_uri


def register_manage_widget(mcp) -> None:  # type: ignore[no-untyped-def]
    uri = widget_uri(mcp.name, "manage.html")
    csp = {"connectDomains": [b for b in [_public_base()] if b]}
    mcp.resource(
        uri,
        name="Tool permissions",
        mime_type="text/html;profile=mcp-app",
        meta={"csp": csp, "ui": {"csp": csp}},
    )(lambda: widget_html("manage.html"))

    approval_url = os.getenv("APPROVAL_URL", "http://127.0.0.1:8072").rstrip("/")

    @mcp.tool(meta={"ui": {"resourceUri": uri}, "ui/resourceUri": uri})
    async def manage_tools() -> str:
        """Open the tool-permissions panel in the chat: one section per DEPLOYED
        connector (telegram, xmcp, ...) with a "last used" label, every tool with
        its mode (always_allow / needs_approval / blocked), and a per-connector
        Forget that drops a stale connector's stored state. The USER reviews and
        saves right in the panel; nothing changes until they click Save, and the
        panel locks after one Save (its snapshot is stale then) -- call
        manage_tools again for another round of changes. To change a mode
        conversationally instead, use set_gating."""
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(f"{approval_url}/manage", json={})
            token = resp.json().get("token", "")
        except Exception:  # noqa: BLE001 - sidecar down -> no session, say so plainly
            token = ""
        if not token:
            return (
                "⚠️ The permissions panel could not be opened: the approval service is "
                "unavailable, so no management session exists. Nothing was changed."
            )
        marker = json.dumps({"token": token})
        return (
            "A tool-permissions panel is shown in the chat, one section per connector. "
            "The user reviews each tool's mode there and clicks Save; nothing is "
            "changed until they do, and the panel locks after one Save. Saved changes "
            "enforce within ~15 seconds, blocked tools drop off a connector's tool "
            "list when it refreshes, and further changes take a fresh manage_tools "
            "call.\n"
            f"<!--MANAGE {marker}-->"
        )
