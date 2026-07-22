"""Tests for the browser proxy wrapper -- no node engine, no Chromium, no network.

What this wrapper exists to guarantee: the child launches HEADED on the
entrypoint's display with the egress wall passed as a Chromium flag (browsers
ignore proxy env vars), proxied tools lose any outputSchema (the connector
contract under the guardrail), the proxy actually forwards list/call to a stdio
child, and browser_live_view reports honestly when no view is running. The
forwarding tests use a dummy python child -- same stdio shape as the engine --
because spawning the real one needs the npm-installed package and a browser.
"""

import asyncio
import importlib.util
import sys
from pathlib import Path

# Both tools ship a `server.py`; load this one under a unique module name so the
# suites can't shadow each other in a whole-repo pytest run.
_SPEC = importlib.util.spec_from_file_location(
    "browser_server", Path(__file__).with_name("server.py")
)
server = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(server)


# --- the child argv: headed, walled, full surface ------------------------------------


def test_child_is_headed_by_default(monkeypatch):
    monkeypatch.delenv("BROWSER_HEADLESS", raising=False)
    assert "--headless" not in server.build_child_args("cli.js")


def test_child_headless_is_an_explicit_flip(monkeypatch):
    monkeypatch.setenv("BROWSER_HEADLESS", "1")
    assert "--headless" in server.build_child_args("cli.js")


def test_child_gets_egress_wall_as_chromium_flag(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://egress:3136")
    args = server.build_child_args("cli.js")
    assert args[args.index("--proxy-server") + 1] == "http://egress:3136"


def test_child_without_proxy_env_gets_no_proxy_flag(monkeypatch):
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    assert "--proxy-server" not in server.build_child_args("cli.js")


def test_child_enables_every_cap_group_by_default(monkeypatch):
    monkeypatch.delenv("BROWSER_CAPS", raising=False)
    args = server.build_child_args("cli.js")
    assert args[args.index("--caps") + 1] == server.DEFAULT_CAPS


def test_child_caps_are_trimmable(monkeypatch):
    monkeypatch.setenv("BROWSER_CAPS", "pdf")
    args = server.build_child_args("cli.js")
    assert args[args.index("--caps") + 1] == "pdf"


def test_child_output_dir_and_cwd_agree(monkeypatch):
    # The engine resolves relative user filenames against cwd, not --output-dir;
    # build_proxy runs the child IN the output dir so both land in the volume.
    monkeypatch.setenv("HOME", "/somewhere")
    args = server.build_child_args("cli.js")
    assert args[args.index("--output-dir") + 1] == "/somewhere/output"
    assert server.output_dir() == "/somewhere/output"


def test_child_env_points_at_the_entrypoint_display(monkeypatch):
    monkeypatch.delenv("DISPLAY", raising=False)
    assert server.build_child_env()["DISPLAY"] == ":99"
    monkeypatch.setenv("DISPLAY", ":7")
    assert server.build_child_env()["DISPLAY"] == ":7"


# --- schema strip: the guardrail/connector contract -----------------------------------


class _FakeTool:
    def __init__(self, schema):
        self.output_schema = schema


def test_strip_middleware_nulls_output_schemas():
    tools = [_FakeTool({"type": "object"}), _FakeTool(None)]

    async def call_next(_ctx):
        return tools

    out = asyncio.run(server.StripOutputSchemas().on_list_tools(None, call_next))
    assert [t.output_schema for t in out] == [None, None]


# --- the proxy: list + call reach a stdio child; live_view rides alongside ------------

_CHILD = '''
from mcp.server.fastmcp import FastMCP
m = FastMCP("dummy-engine")

@m.tool()
def browser_navigate(url: str) -> str:
    """Navigate somewhere."""
    return f"navigated to {url}"

m.run()
'''


def _dummy_proxy(tmp_path, monkeypatch):
    # HOME -> tmp_path: build_proxy creates and spawns the child in HOME/output.
    monkeypatch.setenv("HOME", str(tmp_path))
    child = tmp_path / "cli.py"
    child.write_text(_CHILD)
    # command=python: the dummy child ignores the engine argv it's handed.
    return server.build_proxy(engine_cli=str(child), command=sys.executable)


def test_proxy_forwards_to_stdio_child(tmp_path, monkeypatch):
    proxy = _dummy_proxy(tmp_path, monkeypatch)

    from fastmcp import Client

    async def exercise():
        async with Client(proxy) as client:
            tools = await client.list_tools()
            result = await client.call_tool("browser_navigate", {"url": "https://x.test"})
            return tools, result

    tools, result = asyncio.run(exercise())
    names = {t.name for t in tools}
    assert "browser_navigate" in names  # the child's surface, proxied
    assert "browser_live_view" in names  # our one native addition
    assert all(t.outputSchema is None for t in tools)  # stripped through the proxy
    assert "navigated to https://x.test" in result.content[0].text


def test_live_view_reports_down_when_nothing_listens(tmp_path, monkeypatch):
    # Point the probe at a port nothing binds, whatever the test host runs.
    monkeypatch.setattr(server, "VIEW_PORT", 1)
    proxy = _dummy_proxy(tmp_path, monkeypatch)

    from fastmcp import Client

    async def exercise():
        async with Client(proxy) as client:
            return await client.call_tool("browser_live_view", {})

    result = asyncio.run(exercise())
    assert "not running" in result.content[0].text
