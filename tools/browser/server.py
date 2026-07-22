"""Browser MCP: @playwright/mcp behind the shared security stack, plus a live view.

The engine is Microsoft's official @playwright/mcp npm package (exact-pinned via
this directory's package.json + package-lock.json; the Dockerfile npm-ci's it
into /app/engine). It's a Node server, so the shared fastmcp middleware can't
attach to it directly. Instead it runs as a stdio CHILD PROCESS and a fastmcp
proxy re-exposes its tools through serve() -- auth, approval, and the guardrail
apply at the proxy exactly as they would for a native tool.

Chromium runs HEADED against the Xvfb display the container's entrypoint starts,
so the same session is watchable -- and takeable-over -- through x11vnc/noVNC
(the live view; see entrypoint.sh and the browser_live_view tool). Egress rides
the squid wall via Chromium's own --proxy-server flag; the browser listener is
the stack's one allow-all-external rule (browsing is the tool's job), with
private/link-local ranges still denied.

Posture: web pages are untrusted external input (prompt-injection vector), so
output is guardrail-screened. Screenshots/PDFs are image bytes the text screen
can't inspect -- accepted, noted in the README. The full engine tool surface is
exposed on purpose; which tools require approval is the sidecar's stored
per-tool modes (everything ships always_allow; the operator gates individual
tools via the gatekeeper or the manage panel).
"""

import os
import socket
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

# The npm-ci'd engine entrypoint; overridable for dev/tests.
ENGINE_CLI = os.getenv("BROWSER_ENGINE_CLI", "/app/engine/node_modules/@playwright/mcp/cli.js")
# Every opt-in capability group the engine ships, on by default (the exposed
# surface is deliberately "all of it"; gating lives in the approval sidecar).
DEFAULT_CAPS = "config,devtools,network,pdf,storage,testing,vision"
# Where the entrypoint serves noVNC inside the container (fixed; the tunnel
# overlay routes browser-view.<domain> here).
VIEW_PORT = 6080


def _is_truthy(value: str | None) -> bool:
    return bool(value) and value.strip().lower() in {"1", "true", "yes", "on"}


def output_dir() -> str:
    """Where engine artifacts land (compose mounts the browser-artifacts volume
    here; the browser-sync sidecar drains it to Drive)."""
    return os.path.join(os.getenv("HOME", "/app/state"), "output")


class StripOutputSchemas(Middleware):
    """Null proxied tools' outputSchema at list time.

    Same contract as serve()'s local-registry strip, which can't reach proxied
    tools (they're fetched live from the child): the guardrail nulls each
    result's structuredContent, and an advertised outputSchema without
    conforming structuredContent fails the connector's output validation.
    """

    async def on_list_tools(self, context, call_next):  # type: ignore[no-untyped-def]
        tools = await call_next(context)
        for tool in tools:
            if getattr(tool, "output_schema", None) is not None:
                tool.output_schema = None
        return tools


def build_child_args(engine_cli: str = ENGINE_CLI) -> list[str]:
    """The engine's argv: headed chromium on the Xvfb display, walled egress.

    - HEADED on purpose (no --headless): the live view watches the same X
      display. BROWSER_HEADLESS=1 flips it for deploys that don't run a view.
    - --no-sandbox: chromium's setuid/userns sandbox isn't available to the
      image's non-root user; same trade the engine's own Dockerfile makes.
    - --proxy-server: chromium ignores HTTPS_PROXY env, so the egress wall must
      be passed as a launch flag or the browser would simply have no route out.
    """
    args = [engine_cli, "--browser", "chromium", "--no-sandbox"]
    caps = os.getenv("BROWSER_CAPS", DEFAULT_CAPS).strip()
    if caps:
        args += ["--caps", caps]
    if _is_truthy(os.getenv("BROWSER_HEADLESS")):
        args.append("--headless")
    proxy = os.getenv("HTTPS_PROXY", "").strip()
    if proxy:
        args += ["--proxy-server", proxy]
    # Artifacts (screenshots, PDFs, traces) land in the artifacts volume, not cwd.
    args += ["--output-dir", output_dir()]
    return args


def build_child_env() -> dict[str, str]:
    """The child's env: this container's, pointed at the entrypoint's display."""
    env = dict(os.environ)
    env.setdefault("DISPLAY", ":99")
    return env


def _view_listening() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", VIEW_PORT), timeout=1):
            return True
    except OSError:
        return False


def build_proxy(engine_cli: str = ENGINE_CLI, command: str = "node"):  # type: ignore[no-untyped-def]
    """Front the stdio child with a fastmcp proxy serve() can wrap."""
    # The child RUNS in the output dir: the engine resolves user-supplied
    # relative paths (browser_pdf_save's filename, trace names) against its cwd,
    # not --output-dir, so any other cwd sends those writes outside the synced
    # volume (EACCES on the read-only image tree).
    artifacts = output_dir()
    os.makedirs(artifacts, exist_ok=True)
    transport = StdioTransport(
        command, build_child_args(engine_cli), env=build_child_env(), cwd=artifacts
    )
    proxy = create_proxy(Client(transport), name="browser")
    proxy.add_middleware(StripOutputSchemas())

    @proxy.tool
    def browser_live_view() -> str:
        """Report where a human can watch -- and take over -- the live browser.

        The view is the same headed Chromium session the other tools drive: a
        noVNC page with full mouse/keyboard, guarded by the deployment's VNC
        password (BROWSER_VIEW_PASSWORD). Use it when a flow needs a human step
        (a login, a brittle widget); the session state stays put afterwards.
        """
        if not _view_listening():
            return (
                "Live view is not running on this deployment. It starts with the "
                "container when BROWSER_VIEW_PASSWORD is set in tools/browser/.env "
                "(headless deploys and BROWSER_HEADLESS=1 have no view)."
            )
        url = os.getenv("BROWSER_VIEW_URL", "").strip()
        if url:
            return (
                f"Live view is up: {url} -- opens the current browser session "
                "(VNC password required; full mouse/keyboard takeover)."
            )
        return (
            "Live view is up on container port 6080 (noVNC). No public URL is "
            "configured on this deployment (BROWSER_VIEW_URL is stamped by the "
            "tunnel overlay); reach it over the compose internal network."
        )

    return proxy


def main() -> None:
    port = int(os.getenv("MCP_PORT", "8067"))
    # require_approval declares intent (the browser acts on the live web, and can
    # act as YOU wherever the persistent profile is signed in); the base compose
    # flips it off for local dev, the overlay on. Which tools actually gate is
    # the approval sidecar's stored modes.
    # stateless_http: same rationale as telegram -- a dropped claude.ai session
    # must not wedge the shared stdio child; browser state lives in the child.
    serve(
        build_proxy(),
        port=port,
        untrusted_output=True,
        require_approval=True,
        stateless_http=True,
    )


if __name__ == "__main__":
    main()
