"""Standard composition + serving for mcp-tools servers.

Every tool builds its own FastMCP ``mcp`` (just its tools), then calls :func:`serve`,
which applies the shared cross-cutting security layers UNIFORMLY and runs the HTTP
server. This is the one place the layering/order lives, so every tool gets it right:

- **Google OAuth** (``security.auth``) — always applied; no-ops to an open loopback
  server when ``MCP_AUTH_ENABLED`` is off.
- **Out-of-band human approval** (``security.approval``) — opt in with
  ``require_approval=True``.
- **Guardrail output screening** (``security.guardrail``) — opt in with
  ``untrusted_output=True`` (for tools that return untrusted external content).
- **outputSchema strip** — applied to local tools ONLY when ``untrusted_output=True``,
  because the guardrail nulls ``structuredContent`` and an advertised ``outputSchema``
  then can't be fulfilled (see :func:`_strip_local_output_schemas`). Trusted tools keep
  their schema. ``from_openapi`` tools must strip at build time instead.

A tool declares its threat posture in one line::

    serve(mcp, port=p, untrusted_output=True, require_approval=True)  # e.g. x-mcp
    serve(mcp, port=p)                                                # trusted internal data
"""

from __future__ import annotations

import contextlib
import os

from starlette.responses import JSONResponse

from security.approval.middleware import ApprovalMiddleware, register_catalog
from security.auth import build_oauth_provider
from security.guardrail.middleware import GuardrailMiddleware


def _is_truthy(value: str | None) -> bool:
    return bool(value) and value.strip().lower() in {"1", "true", "yes", "on"}


def _csv_set(value: str | None) -> set[str]:
    if not value:
        return set()
    return {item.strip() for item in value.split(",") if item.strip()}


def _env_override(name: str, default: bool) -> bool:
    """Substrate override of a per-tool security default.

    The tool's ``serve(...)`` call declares INTENT (e.g. xmcp is ``untrusted_output=True``).
    A substrate may flip it by env: unset/blank -> keep the tool's default (the safe
    baseline travels with the code); set -> explicit on/off for THIS deploy. So the
    desktop/stdio substrate turns the public-only layers off and the tunnel/cloud leaves
    them on -- ONE image, N postures. Ambiguity keeps the default: silence never silently
    weakens a tool.
    """
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return _is_truthy(raw)


def _strip_local_output_schemas(mcp) -> None:  # type: ignore[no-untyped-def]
    """Null the ``output_schema`` of every locally-registered tool (@mcp.tool / add_tool).

    Called only for guardrailed servers: FastMCP 3.x derives an ``outputSchema`` for
    every tool, but the guardrail middleware nulls each result's ``structuredContent``
    (screening replaces it with text). The MCP spec requires an advertised
    ``outputSchema`` to come with conforming ``structuredContent``, so the Claude
    connector rejects such a call with an output-validation error. Nulling the schema
    fixes it -- verified at the HTTP layer: tools/list omits outputSchema after this runs.

    SCOPE: reaches the ``LocalProvider`` registry (decorator + add_tool tools). Tools
    built by ``FastMCP.from_openapi`` live in a separate, dynamically-generated provider
    this can't touch -- those are stripped at BUILD time via
    ``from_openapi(mcp_component_fn=...)`` (see tools/xmcp/server.py). Reaches a
    FastMCP-3.4.2 internal; pinned deps keep it stable, and it no-ops if that internal
    shape changes.
    """
    components = getattr(getattr(mcp, "_local_provider", None), "_components", None)
    if not isinstance(components, dict):
        return
    for key, comp in components.items():
        if str(key).startswith("tool:") and getattr(comp, "output_schema", None) is not None:
            comp.output_schema = None


def serve(
    mcp,  # type: ignore[no-untyped-def]
    *,
    port: int,
    host: str | None = None,
    untrusted_output: bool = False,
    require_approval: bool = False,
    stateless_http: bool = False,
    source: str | None = None,
) -> None:
    """Apply the shared security layers to ``mcp`` and run it (transport via env).

    ``source`` is the tool's short name across the security plumbing: it scopes
    approvals, names the sidecar catalog entry (and so the manage panel's section),
    and tags guardrail-wrapped content. Defaults to ``mcp.name`` -- pass it
    explicitly when the server's display name isn't the tool's name (e.g. xmcp's
    server is called "X API MCP").

    ORDER MATTERS: FastMCP wraps ``reversed(middleware)``, so the first-added is the
    OUTERMOST. Approval must be outermost — it short-circuits BEFORE the tool runs, so
    a pending-approval message is never screened — with the guardrail INSIDE it,
    screening only results of calls the human already approved.
    """
    host = host or os.getenv("MCP_HOST", "127.0.0.1")
    source = source or mcp.name

    # SECURITY POSTURE: each layer's default is this tool's serve(...) arg; a deploy
    # may flip it by env. Auth is already env-gated one layer down (build_oauth_provider
    # reads MCP_AUTH_ENABLED); these bring approval + guardrail to the same maturity, so
    # the whole posture is one env-readable table. Flipping MCP_UNTRUSTED_OUTPUT on is a
    # promise the deploy must honor: GUARDRAIL_URL has to
    # resolve to a running screener or every call fails closed at the middleware.
    require_approval = _env_override("MCP_REQUIRE_APPROVAL", require_approval)
    untrusted_output = _env_override("MCP_UNTRUSTED_OUTPUT", untrusted_output)
    stateless_http = _env_override("MCP_STATELESS_HTTP", stateless_http)

    # SPIKE (throwaway, SPIKE_APPROVAL_WIDGET=1): register the in-chat approval-widget
    # probe. Runs BEFORE the middleware blocks so it can extend the guardrail exempt
    # allowlist its helper tool relies on (MCP_GUARDRAIL_EXEMPT).
    if _is_truthy(os.getenv("SPIKE_APPROVAL_WIDGET")):
        from security.approval.widget_spike import register_widget_spike

        register_widget_spike(mcp)

    if require_approval:
        # State + the human-facing pages live in the approval sidecar (APPROVAL_URL) --
        # including every tool's mode; this middleware is only the per-tool client
        # (source scopes its approvals and its catalog registration).
        mcp.add_middleware(
            ApprovalMiddleware(
                source=source,
                widget=_is_truthy(os.getenv("SPIKE_APPROVAL_WIDGET")),
            )
        )
        # Pre-declare the approval protocol in the server-level instructions (a
        # list-time, trusted channel) so a pending status arrives as expected
        # behavior. Runtime tool output that explains itself reads as prompt
        # injection -- to the model and to claude.ai's screening -- which is why the
        # pending message itself is a bare status (see approval/middleware.py).
        # Provider-neutral on purpose: this note is baked in at server startup, but the
        # active approval channel is the sidecar's APPROVAL_PROVIDER -- unknown here. The
        # per-call pending message names the live channel (via the gate response), so this
        # stays generic instead of naming a platform that might not be the one in use.
        note = (
            "Some tools on this server are gated behind out-of-band human approval: "
            "instead of running, a gated call reports a pending status and an "
            "Approve/Deny card for that exact action is posted to the user's approval "
            "channel. After the user approves it there, calling the same tool again with "
            "the same arguments performs the action; if they deny it, that call reports "
            "the denial. Undecided requests expire after 10 minutes, and a later call "
            "posts a fresh card."
        )
        mcp.instructions = f"{mcp.instructions}\n\n{note}" if mcp.instructions else note

    # /healthz: the container healthcheck target (unauthenticated liveness -- custom
    # routes sit outside the MCP OAuth guard). It runs in the server's OWN event loop,
    # so on approval-enabled servers it doubles as STARTUP catalog registration: the
    # manage panel lists every DEPLOYED tool without waiting for a client, and each
    # probe re-beacons, so a wiped sidecar state refills within one probe cycle.
    # origin="startup" -- the sidecar does not stamp "last used" from this; only real
    # client tools/list traffic does (see ApprovalMiddleware.on_list_tools).
    approval_url = os.getenv("APPROVAL_URL", "http://127.0.0.1:8072").rstrip("/")

    @mcp.custom_route("/healthz", methods=["GET"])
    async def _healthz(request):  # type: ignore[no-untyped-def]
        if require_approval:
            with contextlib.suppress(Exception):
                # run_middleware=False: the probe must not traverse ApprovalMiddleware,
                # whose on_list_tools registers origin="list" -- that stamp means "a
                # real client asked" and a health probe is not one.
                tools = await mcp.list_tools(run_middleware=False)
                await register_catalog(source, tools, approval_url, origin="startup")
        return JSONResponse({"ok": True, "server": source})

    if untrusted_output:
        mcp.add_middleware(
            GuardrailMiddleware(
                source=source,
                exempt=_csv_set(os.getenv("MCP_GUARDRAIL_EXEMPT")),
            )
        )

    # A tool that advertises an outputSchema must return conforming structuredContent on
    # EVERY result (MCP rule). Two of our layers return a result WITHOUT it: the guardrail
    # nulls structuredContent when screening, and a gated call short-circuits to a plain
    # pending status. Either way an advertised schema makes the Claude connector reject the
    # call with an output-validation error -- so strip local schemas whenever approval or
    # guardrail is on. (Fully-trusted, ungated tools keep their schema.) Escape hatch:
    # MCP_KEEP_OUTPUT_SCHEMA=1.
    if (require_approval or untrusted_output) and not _is_truthy(
        os.getenv("MCP_KEEP_OUTPUT_SCHEMA")
    ):
        _strip_local_output_schemas(mcp)

    auth = build_oauth_provider()
    if auth is not None:
        mcp.auth = auth

    # RUNTIME SELECTOR: the transport is chosen by env at startup, not baked in, so the
    # same image runs anywhere. Default "http" is the container/server deploy; a desktop
    # substrate sets MCP_TRANSPORT=stdio (host/port then irrelevant). Fail closed on typos
    # rather than silently picking a transport the operator didn't ask for.
    transport = os.getenv("MCP_TRANSPORT", "http").strip().lower()
    if transport == "stdio":
        mcp.run(transport="stdio")
    elif transport == "http":
        # stateless_http: every request is self-contained -- no server-side session to
        # lose. Opted into by tools whose sessions churn (reconnects, container
        # recreates) and whose state lives elsewhere anyway (telegram's stdio child).
        # Passed only when on so an off tool runs exactly the stock code path.
        kwargs = {"stateless_http": True} if stateless_http else {}
        mcp.run(transport="http", host=host, port=port, **kwargs)
    else:
        raise ValueError(f"Unsupported MCP_TRANSPORT={transport!r}; expected 'http' or 'stdio'.")
