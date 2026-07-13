"""In-chat secrets staging widget: type a tool's API keys into a form, not the chat.

stage_secrets(name) renders widgets/secrets.html -- one row per secret the tool's
deploy manifest declares (label + where-to-get hint + a password input). The user's
Save POSTs the values DIRECTLY from the browser to the sidecar's /secrets/<token>
(capability token this tool minted internally), so values never enter chat content,
tool results, or the model's context. The sidecar hands them to the HOST reconciler
via the staging file, which writes tools/<name>/.env and blanks the handoff.
Write-only end to end: no endpoint ever returns a stored value -- the widget only
learns staged/missing booleans.
"""

from __future__ import annotations

import json
import os

import httpx

from security.approval.widget_spike import _public_base, widget_html, widget_uri


def register_secrets_widget(mcp, load_manifests) -> None:  # type: ignore[no-untyped-def]
    uri = widget_uri(mcp.name, "secrets.html")
    csp = {"connectDomains": [b for b in [_public_base()] if b]}
    mcp.resource(
        uri,
        name="Stage secrets",
        mime_type="text/html;profile=mcp-app",
        meta={"csp": csp, "ui": {"csp": csp}},
    )(lambda: widget_html("secrets.html"))

    approval_url = os.getenv("APPROVAL_URL", "http://127.0.0.1:8072").rstrip("/")

    @mcp.tool(meta={"ui": {"resourceUri": uri}, "ui/resourceUri": uri})
    async def stage_secrets(name: str) -> str:
        """Open an in-chat form for staging a not-yet-deployed tool's secrets (API keys
        etc. from its deploy manifest). The USER types values into the form and
        saves; they go directly to the server, never through this chat -- neither
        you nor the chat transcript ever sees them. Already-staged keys show as
        staged and can be left blank. After a save, deploy_status shows the
        staging result within ~15s; then deploy_tool(name) can deploy."""
        manifests = load_manifests()
        manifest = manifests.get(name)
        if manifest is None:
            return (
                f"⚠️ `{name}` is not a shipped tool. Available: "
                f"{', '.join(sorted(manifests))}. Nothing was opened."
            )
        fields = [
            {"key": s["key"], "label": s["label"], "hint": s.get("hint", "")}
            for s in manifest.get("secrets", [])
        ]
        if not fields:
            return (
                f"`{name}` needs no tool-specific secrets (only the shared Google "
                "OAuth identity every tool carries) -- nothing to stage. "
                f"deploy_tool('{name}') can proceed."
            )
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    f"{approval_url}/secrets", json={"tool": name, "fields": fields}
                )
            token = resp.json().get("token", "")
        except Exception:  # noqa: BLE001 - sidecar down -> no session, say so plainly
            token = ""
        if not token:
            return (
                "⚠️ The secrets form could not be opened: the approval service is "
                "unavailable. Nothing was changed."
            )
        marker = json.dumps({"token": token, "tool": name})
        return (
            f"A secrets form for `{name}` is shown in the chat ({len(fields)} "
            "field(s)). The user fills it and clicks Save; the values go directly "
            "to the server and are never visible here. When they say it's saved, "
            "check deploy_status -- staging shows within ~15 seconds.\n"
            f"<!--SECRETS {marker}-->"
        )
