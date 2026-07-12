"""Tests for the telegram proxy wrapper -- no Telegram, no network.

What this wrapper exists to guarantee: the child gets a stdio transport and a
read-only default surface (the two env overrides), proxied tools lose any
outputSchema (the connector contract under the guardrail), and the proxy
actually forwards list/call to a stdio child. The last one uses a dummy child
server -- same shape as the engine (official-SDK FastMCP over stdio) -- because
spawning the real engine needs a session string.
"""

import asyncio
import importlib.util
import sys
from pathlib import Path

# Both tools ship a `server.py`; load this one under a unique module name so the
# suites can't shadow each other in a whole-repo pytest run.
_SPEC = importlib.util.spec_from_file_location(
    "telegram_server", Path(__file__).with_name("server.py")
)
server = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(server)


# --- the child env: the two safety-critical overrides ------------------------------


def test_child_env_forces_stdio(monkeypatch):
    monkeypatch.setenv("MCP_TRANSPORT", "http")  # the parent's transport
    assert server.build_child_env()["MCP_TRANSPORT"] == "stdio"


def test_child_env_defaults_to_read_only(monkeypatch):
    monkeypatch.delenv("TELEGRAM_EXPOSED_TOOLS", raising=False)
    assert server.build_child_env()["TELEGRAM_EXPOSED_TOOLS"] == "read-only"


def test_child_env_respects_explicit_surface(monkeypatch):
    monkeypatch.setenv("TELEGRAM_EXPOSED_TOOLS", "all")
    assert server.build_child_env()["TELEGRAM_EXPOSED_TOOLS"] == "all"


def test_child_env_passes_secrets_through(monkeypatch):
    monkeypatch.setenv("TELEGRAM_SESSION_STRING", "sess-abc")
    assert server.build_child_env()["TELEGRAM_SESSION_STRING"] == "sess-abc"


# --- schema strip: the guardrail/connector contract ---------------------------------


class _FakeTool:
    def __init__(self, schema):
        self.output_schema = schema


def test_strip_middleware_nulls_output_schemas():
    tools = [_FakeTool({"type": "object"}), _FakeTool(None)]

    async def call_next(_ctx):
        return tools

    out = asyncio.run(server.StripOutputSchemas().on_list_tools(None, call_next))
    assert [t.output_schema for t in out] == [None, None]


# --- the proxy: list + call actually reach a stdio child ----------------------------

_CHILD = '''
from mcp.server.fastmcp import FastMCP
m = FastMCP("dummy-engine")

@m.tool()
def greet(name: str) -> str:
    """Say hello."""
    return f"hello {name}"

m.run()
'''


def test_proxy_forwards_to_stdio_child(tmp_path):
    engine = tmp_path / "engine"
    engine.mkdir()
    (engine / "main.py").write_text(_CHILD)
    proxy = server.build_proxy(engine_dir=str(engine))

    from fastmcp import Client

    async def exercise():
        async with Client(proxy) as client:
            tools = await client.list_tools()
            result = await client.call_tool("greet", {"name": "wes"})
            return tools, result

    tools, result = asyncio.run(exercise())
    assert [t.name for t in tools] == ["greet"]
    assert all(t.outputSchema is None for t in tools)  # stripped through the proxy
    assert "hello wes" in result.content[0].text


def test_proxy_child_runs_this_interpreter(tmp_path):
    # The child must run in the SAME locked venv (its deps come from our lock).
    engine = tmp_path / "engine"
    engine.mkdir()
    (engine / "main.py").write_text(
        "import sys\n" + _CHILD.replace('return f"hello {name}"', "return sys.executable")
    )
    proxy = server.build_proxy(engine_dir=str(engine))

    from fastmcp import Client

    async def exercise():
        async with Client(proxy) as client:
            return await client.call_tool("greet", {"name": "x"})

    result = asyncio.run(exercise())
    assert result.content[0].text == sys.executable
