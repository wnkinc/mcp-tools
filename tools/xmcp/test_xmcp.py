"""Tests for xmcp's code-enforced read-only grant and build-time schema strip.

The spec filter is the tool's security boundary (default-deny allowlist, the
write-guard, streaming exclusion), so it gets the coverage. create_mcp is tested
with the spec fetch faked -- which also pins the from_openapi outputSchema strip
(the FastMCP+guardrail connector bug serve() can't reach; see server.py).
"""

import asyncio
import importlib.util
from pathlib import Path

import pytest

# Both tools ship a `server.py`; load this one under a unique module name so the
# suites can't shadow each other in a whole-repo pytest run.
_SPEC = importlib.util.spec_from_file_location("xmcp_server", Path(__file__).with_name("server.py"))
server = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(server)

GRANT_ENV = [
    "X_API_TOOL_ALLOWLIST",
    "X_API_TOOL_DENYLIST",
    "X_API_TOOL_TAGS",
    "X_API_ALLOW_WRITES",
    "X_BEARER_TOKEN",
    "MCP_KEEP_OUTPUT_SCHEMA",
]


@pytest.fixture(autouse=True)
def clean_grant_env(monkeypatch):
    for name in GRANT_ENV:
        monkeypatch.delenv(name, raising=False)
    # The real .env must never leak into a test's grant computation.
    monkeypatch.setattr(server, "load_env", lambda: None)


def _op(op_id: str, tags: list[str] | None = None) -> dict:
    return {"operationId": op_id, "tags": tags or [], "responses": {"200": {"description": "ok"}}}


def _spec(paths: dict) -> dict:
    return {"openapi": "3.0.0", "info": {"title": "x", "version": "1"}, "paths": paths}


def _exposed_ops(filtered: dict) -> set[str]:
    return {
        op.get("operationId")
        for item in filtered["paths"].values()
        for method, op in item.items()
        if method in server.HTTP_METHODS
    }


# --- the read-only grant ----------------------------------------------------------


def test_empty_allowlist_falls_back_to_readonly_default_not_everything():
    spec = _spec(
        {
            "/2/tweets/search/recent": {"get": _op("searchPostsRecent")},
            "/2/anything/else": {"get": _op("someOtherReadOp")},
        }
    )
    assert _exposed_ops(server.filter_openapi_spec(spec)) == {"searchPostsRecent"}


def test_writes_blocked_even_when_allowlisted(monkeypatch):
    monkeypatch.setenv("X_API_TOOL_ALLOWLIST", "createPost,searchPostsRecent")
    spec = _spec(
        {
            "/2/tweets": {"post": _op("createPost")},
            "/2/tweets/search/recent": {"get": _op("searchPostsRecent")},
        }
    )
    assert _exposed_ops(server.filter_openapi_spec(spec)) == {"searchPostsRecent"}

    monkeypatch.setenv("X_API_ALLOW_WRITES", "1")  # explicit opt-in is the only door
    assert _exposed_ops(server.filter_openapi_spec(spec)) == {"createPost", "searchPostsRecent"}


def test_denylist_beats_allowlist(monkeypatch):
    monkeypatch.setenv("X_API_TOOL_ALLOWLIST", "searchPostsRecent,getPostsById")
    monkeypatch.setenv("X_API_TOOL_DENYLIST", "getPostsById")
    spec = _spec(
        {
            "/2/tweets/search/recent": {"get": _op("searchPostsRecent")},
            "/2/tweets/{id}": {"get": _op("getPostsById")},
        }
    )
    assert _exposed_ops(server.filter_openapi_spec(spec)) == {"searchPostsRecent"}


def test_streaming_and_webhooks_never_exposed(monkeypatch):
    monkeypatch.setenv("X_API_TOOL_ALLOWLIST", "searchStream,tagStream,hookOp")
    spec = _spec(
        {
            "/2/tweets/search/stream": {"get": _op("searchStream")},
            "/2/tagged": {"get": _op("tagStream", tags=["Stream"])},
            "/2/webhooks/x": {"get": _op("hookOp")},
        }
    )
    assert _exposed_ops(server.filter_openapi_spec(spec)) == set()


# --- comma-joined array params (X's explode:false quirk) ---------------------------


def test_collect_comma_params_finds_explode_false_arrays():
    spec = {
        "components": {
            "parameters": {
                "TweetFields": {
                    "name": "tweet.fields",
                    "in": "query",
                    "explode": False,
                    "schema": {"type": "array"},
                }
            }
        },
        "paths": {
            "/2/tweets": {
                "get": {
                    "parameters": [
                        {
                            "name": "ids",
                            "in": "query",
                            "explode": False,
                            "schema": {"type": "array"},
                        },
                        # explode:true, non-array, and non-query params must not join.
                        {
                            "name": "repeat",
                            "in": "query",
                            "explode": True,
                            "schema": {"type": "array"},
                        },
                        {"name": "max_results", "in": "query", "schema": {"type": "integer"}},
                        {"name": "id", "in": "path", "explode": False, "schema": {"type": "array"}},
                    ]
                }
            }
        },
    }
    assert server.collect_comma_params(spec) == {"tweet.fields", "ids"}


# --- create_mcp: the built server -------------------------------------------------

SEARCH_RESPONSES = {
    "200": {
        "description": "ok",
        "content": {
            "application/json": {
                "schema": {"type": "object", "properties": {"data": {"type": "string"}}}
            }
        },
    }
}


def _fake_spec_fetch(monkeypatch):
    spec = _spec(
        {
            "/2/tweets/search/recent": {
                "get": {**_op("searchPostsRecent"), "responses": SEARCH_RESPONSES}
            }
        }
    )
    monkeypatch.setattr(server, "load_openapi_spec", lambda: spec)


def _tools(mcp) -> dict:
    return {tool.name: tool for tool in asyncio.run(mcp.list_tools())}


def test_create_mcp_requires_bearer_token(monkeypatch):
    _fake_spec_fetch(monkeypatch)
    with pytest.raises(RuntimeError, match="X_BEARER_TOKEN"):
        server.create_mcp()


def test_create_mcp_strips_output_schemas_for_the_guardrail(monkeypatch):
    _fake_spec_fetch(monkeypatch)
    monkeypatch.setenv("X_BEARER_TOKEN", "token")
    tools = _tools(server.create_mcp())
    assert set(tools) == {"searchPostsRecent", "grok_x_search"}
    # The guardrail nulls structuredContent, so an advertised outputSchema would
    # break the connector's output validation. create_mcp owns the from_openapi
    # tools (serve() can't reach their provider); grok is a LocalProvider tool that
    # serve()'s own strip pass handles (covered in security/test_serve.py).
    assert tools["searchPostsRecent"].output_schema is None


def test_keep_output_schema_escape_hatch(monkeypatch):
    _fake_spec_fetch(monkeypatch)
    monkeypatch.setenv("X_BEARER_TOKEN", "token")
    monkeypatch.setenv("MCP_KEEP_OUTPUT_SCHEMA", "1")
    tools = _tools(server.create_mcp())
    assert tools["searchPostsRecent"].output_schema is not None
