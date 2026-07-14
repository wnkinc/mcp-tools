"""The in-chat approval card (approve.html), plus the shared widget plumbing.

Opt-in per tool via APPROVAL_WIDGET=1 (see serve()): one middleware tags every
needs_approval tool in tools/list with the widget's _meta, so the host renders the
in-chat Approve/Deny card when a gated call returns its pending status -- no
per-tool code, and the card always agrees with the gate because both read the same
sidecar modes.

The widget flips the gate by a DIRECT fetch to the sidecar /approve/{token}
(session-proof on web+desktop). Because the model can't make HTTP requests and no
tool flips the gate, the token is harmless in the tool result -- so it rides the
result content and needs no forge-proof _meta plumbing.

widget_html/widget_uri/_public_base are the shared plumbing every in-chat widget
uses (the manage panel and secrets form import them from here). History: built as
the SPIKE_APPROVAL_WIDGET spike, promoted 2026-07-14; its approval_probe render-test
tool was removed 2026-07-13 (recover from git history if a harmless gated stand-in
is ever needed again).
"""

from __future__ import annotations

import contextlib
import hashlib
import os
from pathlib import Path

from fastmcp.server.middleware import Middleware

from security.approval.gating import fetch_modes, mode_for

_WIDGETS = Path(__file__).resolve().parent / "widgets"
_html_cache: dict[str, str] = {}


def widget_uri(source: str, filename: str) -> str:
    # Per-SOURCE + content-hashed URI. The source keeps each connector's widget URI
    # distinct (the host associates a ui:// URI with one connector -- a shared URI
    # renders only on the first one, so the gatekeeper's card wouldn't show while
    # telegram's did). The hash busts the connector's per-URI resource cache on any
    # widget change.
    h = hashlib.sha1(widget_html(filename).encode()).hexdigest()[:10]  # noqa: S324 - cache-bust, not security
    return f"ui://{Path(filename).stem}.{source}.{h}.html"


def _public_base() -> str:
    return os.getenv("APPROVAL_PUBLIC_URL", "").rstrip("/")


def widget_html(filename: str) -> str:
    if filename not in _html_cache:
        html = (_WIDGETS / filename).read_text()
        html = html.replace(
            "/*__EXT_APPS_BUNDLE__*/", (_WIDGETS / "ext-apps-bundle.js").read_text()
        )
        # Bake the fixed sidecar origin into the widget (per-call data like tokens comes
        # via the tool result; the base URL is constant, so it need not travel in content).
        html = html.replace("__APPROVAL_PUBLIC_BASE__", _public_base())
        _html_cache[filename] = html
    return _html_cache[filename]


class WidgetMetaMiddleware(Middleware):
    """Tag the needs_approval tools in tools/list with the approval widget's _meta,
    so the host renders the in-chat approval card when they're called. This is the ONE
    place that decides which tools show the card -- driven by the same sidecar modes
    that decide which tools need approval. No per-tool code."""

    def __init__(self, uri: str, source: str, approval_url: str) -> None:
        self._uri = uri
        self._source = source
        self._approval_url = approval_url

    async def on_list_tools(self, context, call_next):  # type: ignore[no-untyped-def]
        tools = await call_next(context)
        # Same mode decision the ApprovalMiddleware uses (live sidecar modes), so the
        # card and the gate always agree on a tool.
        modes = await fetch_modes(self._source, self._approval_url)
        meta = {"ui": {"resourceUri": self._uri}, "ui/resourceUri": self._uri}
        out = []
        for t in tools:
            # Only needs_approval gets the card; blocked tools were already filtered
            # out by the (inner) ApprovalMiddleware's on_list_tools before this runs.
            # modes=None (sidecar never answered) tags nothing -- cosmetic only; the
            # gate itself fails closed.
            if modes is not None and mode_for(t.name, modes) == "needs_approval":
                merged = {**(getattr(t, "meta", None) or {}), **meta}
                # Best-effort tag; if the Tool isn't a copyable pydantic model, leave it.
                with contextlib.suppress(Exception):
                    t = t.model_copy(update={"meta": merged})
            out.append(t)
        return out


def register_approve_widget(mcp) -> None:  # type: ignore[no-untyped-def]
    uri = widget_uri(mcp.name, "approve.html")
    _csp = {"connectDomains": [b for b in [_public_base()] if b]}
    mcp.resource(
        uri,
        name="Approval widget",
        mime_type="text/html;profile=mcp-app",
        meta={"csp": _csp, "ui": {"csp": _csp}},
    )(lambda: widget_html("approve.html"))

    # THE one shared piece: tag the GATED tools so their pending result renders the
    # approval card -- same gated decision (baseline + live overrides) the gate uses.
    approval_url = os.getenv("APPROVAL_URL", "http://127.0.0.1:8072")
    mcp.add_middleware(WidgetMetaMiddleware(uri=uri, source=mcp.name, approval_url=approval_url))
