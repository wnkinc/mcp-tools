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

import os

from security.approval.middleware import ApprovalMiddleware, register_approval_routes
from security.auth import build_oauth_provider
from security.guardrail.middleware import GuardrailMiddleware


def _is_truthy(value: str | None) -> bool:
    return bool(value) and value.strip().lower() in {"1", "true", "yes", "on"}


def _csv_set(value: str | None) -> set[str]:
    if not value:
        return set()
    return {item.strip() for item in value.split(",") if item.strip()}


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
    ``from_openapi(mcp_component_fn=...)`` (see tools/x-mcp/server.py). Reaches a
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
    guardrail_source: str | None = None,
    approval_exempt_env: str = "MCP_APPROVAL_EXEMPT",
) -> None:
    """Apply the shared security layers to ``mcp`` and run it over HTTP.

    ORDER MATTERS: FastMCP wraps ``reversed(middleware)``, so the first-added is the
    OUTERMOST. Approval must be outermost — it short-circuits BEFORE the tool runs, so
    a pending-approval message is never screened — with the guardrail INSIDE it,
    screening only results of calls the human already approved.
    """
    host = host or os.getenv("MCP_HOST", "127.0.0.1")

    if require_approval:
        mcp.add_middleware(ApprovalMiddleware(exempt=_csv_set(os.getenv(approval_exempt_env))))
        register_approval_routes(mcp)
    if untrusted_output:
        mcp.add_middleware(GuardrailMiddleware(source=guardrail_source or mcp.name))
        # The guardrail nulls each result's structuredContent (screening replaces it with
        # text). A tool that still advertises an outputSchema then violates the MCP rule
        # "outputSchema => conforming structuredContent", so the Claude connector rejects
        # the call with an output-validation error. Strip schemas from these guardrailed
        # tools. (Trusted tools keep their outputSchema -- they still return matching
        # structuredContent, so it's valid and useful.) Escape hatch: MCP_KEEP_OUTPUT_SCHEMA=1.
        if not _is_truthy(os.getenv("MCP_KEEP_OUTPUT_SCHEMA")):
            _strip_local_output_schemas(mcp)

    auth = build_oauth_provider()
    if auth is not None:
        mcp.auth = auth

    mcp.run(transport="http", host=host, port=port)
