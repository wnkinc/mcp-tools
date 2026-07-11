"""Telegram MCP: chigwell/telegram-mcp behind the shared security stack.

The engine is a pinned source checkout of an MCP server built on the official
SDK's FastMCP (see the Dockerfile's vendor stage), so the shared fastmcp
middleware can't attach to it directly. Instead it runs as a stdio CHILD
PROCESS and a fastmcp proxy re-exposes its tools through serve() -- auth and
the guardrail apply at the proxy exactly as they would for a native tool. The
child inherits this container's env (session string, proxy settings) with two
enforced overrides, below.

Posture: Telegram message content is untrusted external input (prompt-injection
vector), so output is guardrail-screened. The exposed surface defaults to the
engine's read-only tool set; widening to TELEGRAM_EXPOSED_TOOLS=all is a
deliberate .env change, and every non-read tool then blocks on the out-of-band
approval gate (approval-exempt.txt carries the vetted read-only names).
"""

import os
import sys
from pathlib import Path

from fastmcp import Client
from fastmcp.client.transports import StdioTransport
from fastmcp.server import create_proxy
from fastmcp.server.middleware import Middleware

# Make the repo root importable regardless of CWD, then load the shared serve()
# helper (applies OAuth + optional guardrail/approval, then runs).
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from security.serve import serve  # noqa: E402

# The sha256-verified checkout the Dockerfile stages; overridable for dev/tests.
ENGINE_DIR = os.getenv("TELEGRAM_ENGINE_DIR", "/app/vendor/telegram-mcp")

# The engine's read-only tools (minus upstream mislabels -- see the file header):
# these skip the approval gate so reads flow freely while writes wait for a human.
APPROVAL_EXEMPT_FILE = Path(__file__).with_name("approval-exempt.txt")


def load_approval_exemptions(path: Path = APPROVAL_EXEMPT_FILE) -> set[str]:
    return {
        line.strip()
        for line in path.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }


def apply_approval_exemptions() -> None:
    """Default MCP_APPROVAL_EXEMPT from the committed list (env wins if set)."""
    os.environ.setdefault("MCP_APPROVAL_EXEMPT", ",".join(sorted(load_approval_exemptions())))


class StripOutputSchemas(Middleware):
    """Null proxied tools' outputSchema at list time.

    Same contract as serve()'s local-registry strip, which can't reach proxied
    tools (they're fetched live from the child): the guardrail nulls each
    result's structuredContent, and an advertised outputSchema without
    conforming structuredContent fails the connector's output validation.
    Today every engine tool returns plain text (no schema), so this is
    defensive -- it keeps an engine upgrade from breaking the connector.
    """

    async def on_list_tools(self, context, call_next):  # type: ignore[no-untyped-def]
        tools = await call_next(context)
        for tool in tools:
            if getattr(tool, "output_schema", None) is not None:
                tool.output_schema = None
        return tools


def build_child_env() -> dict[str, str]:
    """The child's env: this container's, with the two safety-critical overrides.

    - MCP_TRANSPORT must be stdio for the child regardless of the parent's
      (the parent serves http; the child speaks stdio to the proxy).
    - The exposed tool surface defaults to read-only; a deploy widens it
      EXPLICITLY by setting TELEGRAM_EXPOSED_TOOLS (env beats the default).
    """
    env = dict(os.environ)
    env["MCP_TRANSPORT"] = "stdio"
    env.setdefault("TELEGRAM_EXPOSED_TOOLS", "read-only")
    return env


def build_proxy(engine_dir: str = ENGINE_DIR):  # type: ignore[no-untyped-def]
    """Front the stdio child with a fastmcp proxy serve() can wrap."""
    transport = StdioTransport(
        sys.executable,
        [str(Path(engine_dir) / "main.py")],
        env=build_child_env(),
    )
    proxy = create_proxy(Client(transport), name="telegram")
    proxy.add_middleware(StripOutputSchemas())
    return proxy


def main() -> None:
    port = int(os.getenv("MCP_PORT", "8063"))
    apply_approval_exemptions()
    # require_approval declares this tool's intent (its writes act as YOU on
    # Telegram); the base compose flips it off for local dev, the overlay on.
    # With the read-only surface every exposed tool is exempt, so the gate only
    # bites once TELEGRAM_EXPOSED_TOOLS=all exposes the write tools.
    # stateless_http: a dropped claude.ai session once wedged the shared stdio pipe
    # and hung every later connect at tools/list; with no server-side HTTP session
    # there's nothing to wedge. Telegram state lives in the long-running child, not
    # the session.
    serve(
        build_proxy(),
        port=port,
        untrusted_output=True,
        require_approval=True,
        stateless_http=True,
    )


if __name__ == "__main__":
    main()
