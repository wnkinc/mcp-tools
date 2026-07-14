"""Google Workspace MCP: taylorwilsdon/google_workspace_mcp behind the shared stack.

The engine is a pinned source checkout (see the Dockerfile's vendor stage) built on
the SAME fastmcp our stack uses, so unlike telegram (stdio child behind a proxy)
its FastMCP instance is imported and handed to serve() NATIVELY -- our middleware
attaches to it at runtime and no engine code is modified. This wrapper replaces
the engine's launcher (main.py's main()), calling the same importable startup
helpers it does: register every service's tools, wrap them with the engine's
tool-registry filter, and configure the HTTP-mode Google OAuth callback routes.

Two OAuth systems, deliberately separated:
  - MCP auth (who may connect Claude): OURS, from serve() -- the engine's own
    MCP-level auth modes (MCP_ENABLE_OAUTH21 / its GoogleProvider) stay OFF.
  - Workspace auth (acting on YOUR Google account): the ENGINE's, in single-user
    mode -- its /oauth2callback custom route rides this same app (custom routes
    bypass MCP auth exactly like /healthz; Google's browser redirect needs that),
    and refreshable credentials persist in the state volume.

Posture: Gmail/Docs/Drive content is attacker-controllable external input (the
canonical prompt-injection vector), so output is guardrail-screened; approval is
on like every tool (ship-open until the operator gates via the manage panel).
"""

import os
import sys
from importlib import import_module
from pathlib import Path

# Make the repo root importable regardless of CWD, then load the shared serve()
# helper (applies OAuth + optional guardrail/approval, then runs).
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from security.serve import serve  # noqa: E402

# The sha256-verified checkout the Dockerfile stages; overridable for dev/tests
# (point it at the repo's local vendor/ checkout).
ENGINE_DIR = os.getenv("WORKSPACE_ENGINE_DIR", "/app/vendor/google_workspace_mcp")


def _engine_env_defaults() -> None:
    """The engine's posture, set BEFORE its modules import (oauth_config reads env).

    - single-user: this stack is one operator's; the engine's multi-user OAuth 2.1
      MCP-auth is the problem serve() already solves, so it stays off.
    - transport streamable-http: serve() runs the HTTP app; the engine only needs
      to know which mode it's in (redirect-URI construction, callback routes).
    - credentials in /app/state: the compose state volume -- workspace tokens
      survive rebuilds.
    """
    os.environ.setdefault("MCP_SINGLE_USER_MODE", "1")
    os.environ.setdefault("WORKSPACE_MCP_TRANSPORT", "streamable-http")
    os.environ.setdefault("WORKSPACE_MCP_PORT", os.getenv("MCP_PORT", "8066"))
    os.environ.setdefault("WORKSPACE_MCP_CREDENTIALS_DIR", "/app/state/credentials")
    os.environ.setdefault("WORKSPACE_MCP_LOG_DIR", "/app/state/logs")
    os.environ.setdefault("WORKSPACE_ATTACHMENT_DIR", "/app/state/attachments")


def build_engine():  # type: ignore[no-untyped-def]
    """Import the engine and run its startup wiring; returns its FastMCP server.

    Mirrors the default (all services, no tier filter) path of the engine's
    main(): WORKSPACE_MCP_TOOLS / WORKSPACE_MCP_TOOL_TIER env vars still apply
    inside the engine's own tool_registry if a deploy narrows them.
    """
    _engine_env_defaults()
    sys.path.insert(0, ENGINE_DIR)

    from auth.scopes import set_enabled_tools as set_scope_services  # noqa: E402
    from core.server import configure_server_for_http, server, set_transport_mode  # noqa: E402
    from core.tool_registry import (  # noqa: E402
        filter_server_tools,
        wrap_server_tool_method,
    )
    from core.tool_registry import (
        set_enabled_tools as set_enabled_tool_names,
    )
    from main import SERVICE_MODULES  # noqa: E402 - module-level init only, not main()

    set_enabled_tool_names(None)  # all tools; env-driven narrowing happens in-registry
    wrap_server_tool_method(server)
    set_scope_services(list(SERVICE_MODULES))
    for module in SERVICE_MODULES.values():
        import_module(module)  # decorators register each service's tools
    filter_server_tools(server)

    set_transport_mode("streamable-http")
    configure_server_for_http()  # Google OAuth callback routes (engine MCP-auth stays off)
    return server


def main() -> None:
    port = int(os.getenv("MCP_PORT", "8066"))
    # Workspace content (mail bodies, docs, comments) is untrusted external input ->
    # guardrail-screened; every call acts as YOUR Google account -> approval layer on
    # (ship-open default; gate/block via the manage panel). source names this tool
    # across the security plumbing -- the engine's display name is its own.
    serve(
        build_engine(),
        port=port,
        untrusted_output=True,
        require_approval=True,
        source="workspace",
    )


if __name__ == "__main__":
    main()
