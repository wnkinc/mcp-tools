"""SPIKE round 3 (Option A, telegram only): approval widget via ONE shared piece.

Enabled by SPIKE_APPROVAL_WIDGET=1. The design: a single middleware tags tools in
tools/list with the approval widget's _meta, so the host renders the in-chat card
when those tools are called -- no per-tool code. Which tools get the card is just a
LIST (here the render-test set; the real build tags the gated/non-exempt set).

The widget flips the gate by a DIRECT fetch to the sidecar /approve/{token} (proven
session-proof on web+desktop). Because the model can't make HTTP requests and there
is no tool that flips the gate, the token is harmless in the tool result -- so it
rides the result content and needs no forge-proof _meta plumbing.

This module currently runs the RENDER TEST: does a list-injected _meta render the
widget? approval_probe stays exempt (so it runs and returns a token-bearing payload)
and is tagged by the middleware. If the card renders, list-injection works and we
wire the real gated flow (approval_probe becomes gated + the middleware tags the
whole gated set + ApprovalMiddleware carries the token on its pending result).
"""

from __future__ import annotations

import contextlib
import hashlib
import os
from pathlib import Path

from fastmcp.server.middleware import Middleware

from security.approval.gating import fetch_overrides, is_gated

_WIDGETS = Path(__file__).resolve().parent / "widgets"
_html_cache: str | None = None


def _widget_uri(source: str) -> str:
    # Per-SOURCE + content-hashed URI. The source keeps each connector's widget URI
    # distinct (the host associates a ui:// URI with one connector -- a shared URI
    # renders only on the first one, so the gatekeeper's card wouldn't show while
    # telegram's did). The hash busts the connector's per-URI resource cache on any
    # widget change.
    h = hashlib.sha1(_widget_html().encode()).hexdigest()[:10]  # noqa: S324 - cache-bust, not security
    return f"ui://approve.{source}.{h}.html"


def _public_base() -> str:
    return os.getenv("APPROVAL_PUBLIC_URL", "").rstrip("/")


def _widget_html() -> str:
    global _html_cache
    if _html_cache is None:
        html = (_WIDGETS / "approve.html").read_text()
        html = html.replace(
            "/*__EXT_APPS_BUNDLE__*/", (_WIDGETS / "ext-apps-bundle.js").read_text()
        )
        # Bake the fixed sidecar origin into the widget (the token is per-call and comes
        # via the tool result; the base URL is constant, so it need not travel in content).
        html = html.replace("__APPROVAL_PUBLIC_BASE__", _public_base())
        _html_cache = html
    return _html_cache


def _extend_env_csv(name: str, *names: str) -> None:
    have = {p.strip() for p in os.getenv(name, "").split(",") if p.strip()}
    os.environ[name] = ",".join(sorted(have | set(names)))


def _approval_exempt() -> set[str]:
    return {p.strip() for p in os.getenv("MCP_APPROVAL_EXEMPT", "").split(",") if p.strip()}


class WidgetMetaMiddleware(Middleware):
    """Tag the GATED (non-exempt) tools in tools/list with the approval widget's _meta,
    so the host renders the in-chat approval card when they're called. This is the ONE
    place that decides which tools show the card -- driven by the same exempt list that
    already decides which tools need approval. No per-tool code."""

    def __init__(self, uri: str, source: str, approval_url: str) -> None:
        self._uri = uri
        self._source = source
        self._approval_url = approval_url

    async def on_list_tools(self, context, call_next):  # type: ignore[no-untyped-def]
        tools = await call_next(context)
        # Same gated decision the ApprovalMiddleware uses (baseline exempt + live
        # sidecar overrides), so the card and the gate always agree on a tool.
        baseline = _approval_exempt()
        overrides = await fetch_overrides(self._source, self._approval_url)
        meta = {"ui": {"resourceUri": self._uri}, "ui/resourceUri": self._uri}
        out = []
        for t in tools:
            if is_gated(t.name, baseline, overrides):
                merged = {**(getattr(t, "meta", None) or {}), **meta}
                # Best-effort tag; if the Tool isn't a copyable pydantic model, leave it.
                with contextlib.suppress(Exception):
                    t = t.model_copy(update={"meta": merged})
            out.append(t)
        return out


def register_widget_spike(mcp) -> None:  # type: ignore[no-untyped-def]
    # approval_probe is a GATED (non-exempt) tool -- a SAFE stand-in for a real gated
    # action. Its FIRST call is short-circuited by the ApprovalMiddleware to the widget;
    # only after you approve + reply does its body run. Guardrail-exempt so its own
    # (trusted) result isn't wrapped.
    _extend_env_csv("MCP_GUARDRAIL_EXEMPT", "approval_probe")

    uri = _widget_uri(mcp.name)
    _csp = {"connectDomains": [b for b in [_public_base()] if b]}
    mcp.resource(
        uri,
        name="Approval widget",
        mime_type="text/html;profile=mcp-app",
        meta={"csp": _csp, "ui": {"csp": _csp}},
    )(lambda: _widget_html())

    # The harmless gated TEST tool lives only on telegram (where we validate the flow);
    # other widget-mode servers (e.g. gatekeeper) get the widget infra without it.
    if mcp.name == "telegram":

        async def approval_probe(action: str = "demo action") -> str:
            """A harmless gated test action. The first call surfaces the in-chat approval
            card; after you approve it and reply, this re-runs and returns the line below
            (a real gated tool -- e.g. send_message -- would perform its action here)."""
            return f"✅ approval_probe ran — a real gated tool would have run {action!r} here."

        mcp.tool(name="approval_probe")(approval_probe)

    # THE one shared piece: tag the GATED tools so their pending result renders the
    # approval card -- same gated decision (baseline + live overrides) the gate uses.
    approval_url = os.getenv("APPROVAL_URL", "http://127.0.0.1:8072")
    mcp.add_middleware(WidgetMetaMiddleware(uri=uri, source=mcp.name, approval_url=approval_url))
