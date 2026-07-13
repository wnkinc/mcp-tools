"""Tests for serve()'s security composition.

Posture flags, env overrides, middleware order, outputSchema stripping, and
transport selection. ``mcp.run`` is replaced with a recorder -- no server, no
network. Auth wiring is covered in test_auth.py; here only its fail-closed
behavior through serve() is asserted.
"""

import asyncio
from types import SimpleNamespace

import httpx as _httpx
import pytest
from fastmcp import FastMCP
from starlette.testclient import TestClient as _StarletteClient

import security.approval.middleware as _amod
import security.guardrail.middleware as _gmod
from security.approval.middleware import ApprovalMiddleware
from security.guardrail.middleware import GuardrailMiddleware
from security.serve import _env_override, _strip_local_output_schemas, serve

POSTURE_ENV = [
    "MCP_AUTH_ENABLED",
    "MCP_REQUIRE_APPROVAL",
    "MCP_UNTRUSTED_OUTPUT",
    "MCP_KEEP_OUTPUT_SCHEMA",
    "MCP_TRANSPORT",
    "MCP_HOST",
]


@pytest.fixture(autouse=True)
def clean_posture_env(monkeypatch):
    """The dev shell may carry posture env; tests must start from the code's defaults."""
    for name in POSTURE_ENV:
        monkeypatch.delenv(name, raising=False)


def _mcp() -> FastMCP:
    mcp = FastMCP("t")

    @mcp.tool
    def add(a: int, b: int) -> dict:
        return {"total": a + b}

    return mcp


def _serve_captured(mcp, **kwargs) -> dict:
    """Run serve() with mcp.run recording its kwargs instead of serving."""
    calls: list[dict] = []
    mcp.run = lambda **kw: calls.append(kw)
    serve(mcp, port=8000, **kwargs)
    assert len(calls) == 1
    return calls[0]


def _middleware_types(mcp) -> list[type]:
    return [type(m) for m in mcp.middleware]


def _tool_schemas(mcp) -> dict[str, object]:
    comps = mcp._local_provider._components
    return {str(k): v.output_schema for k, v in comps.items() if str(k).startswith("tool:")}


# --- posture composition ---------------------------------------------------------


def test_default_posture_is_bare_http():
    mcp = _mcp()
    run_kwargs = _serve_captured(mcp)
    types = _middleware_types(mcp)
    assert ApprovalMiddleware not in types
    assert GuardrailMiddleware not in types
    assert mcp.auth is None  # MCP_AUTH_ENABLED unset -> loopback mode
    assert all(schema is not None for schema in _tool_schemas(mcp).values())
    assert run_kwargs == {"transport": "http", "host": "127.0.0.1", "port": 8000}


def test_untrusted_output_adds_guardrail_and_strips_schemas():
    mcp = _mcp()
    assert all(schema is not None for schema in _tool_schemas(mcp).values())
    _serve_captured(mcp, untrusted_output=True)
    assert GuardrailMiddleware in _middleware_types(mcp)
    assert all(schema is None for schema in _tool_schemas(mcp).values())


def test_source_names_both_middlewares():
    # source is the tool's ONE short name across the security plumbing (approval
    # scoping + catalog/panel section + guardrail tags); it defaults to mcp.name
    # and an explicit arg overrides it (xmcp's display name isn't its tool name).
    ours = (ApprovalMiddleware, GuardrailMiddleware)
    mcp = _mcp()
    _serve_captured(mcp, untrusted_output=True, require_approval=True)
    assert {m.source for m in mcp.middleware if isinstance(m, ours)} == {"t"}
    mcp = _mcp()
    _serve_captured(mcp, untrusted_output=True, require_approval=True, source="xmcp")
    assert {m.source for m in mcp.middleware if isinstance(m, ours)} == {"xmcp"}


def test_approval_is_outermost_of_the_two():
    # FastMCP wraps reversed(middleware): first-added is outermost. Approval must
    # short-circuit BEFORE the guardrail ever sees a result (see serve()'s docstring).
    mcp = _mcp()
    _serve_captured(mcp, untrusted_output=True, require_approval=True)
    types = _middleware_types(mcp)
    assert types.index(ApprovalMiddleware) < types.index(GuardrailMiddleware)


def test_keep_output_schema_escape_hatch(monkeypatch):
    monkeypatch.setenv("MCP_KEEP_OUTPUT_SCHEMA", "1")
    mcp = _mcp()
    _serve_captured(mcp, untrusted_output=True)
    assert GuardrailMiddleware in _middleware_types(mcp)
    assert all(schema is not None for schema in _tool_schemas(mcp).values())


def test_auth_on_but_unconfigured_refuses_to_start(monkeypatch):
    monkeypatch.setenv("MCP_AUTH_ENABLED", "1")
    with pytest.raises(RuntimeError, match="Refusing"):
        _serve_captured(_mcp())


# --- env overrides of the tool's declared posture --------------------------------


def test_env_flips_posture_on(monkeypatch):
    monkeypatch.setenv("MCP_UNTRUSTED_OUTPUT", "1")
    mcp = _mcp()
    _serve_captured(mcp)  # tool default: trusted
    assert GuardrailMiddleware in _middleware_types(mcp)


def test_env_flips_posture_off(monkeypatch):
    monkeypatch.setenv("MCP_REQUIRE_APPROVAL", "0")
    mcp = _mcp()
    _serve_captured(mcp, require_approval=True)
    assert ApprovalMiddleware not in _middleware_types(mcp)


def test_blank_env_keeps_tool_default(monkeypatch):
    # Silence never silently weakens a tool: blank != off.
    monkeypatch.setenv("MCP_REQUIRE_APPROVAL", "")
    mcp = _mcp()
    _serve_captured(mcp, require_approval=True)
    assert ApprovalMiddleware in _middleware_types(mcp)


@pytest.mark.parametrize(
    ("raw", "default", "expected"),
    [
        (None, True, True),
        (None, False, False),
        ("  ", True, True),
        ("1", False, True),
        ("true", False, True),
        ("0", True, False),
        ("off", True, False),
    ],
)
def test_env_override_table(monkeypatch, raw, default, expected):
    if raw is None:
        monkeypatch.delenv("X_FLAG", raising=False)
    else:
        monkeypatch.setenv("X_FLAG", raw)
    assert _env_override("X_FLAG", default) is expected


# --- transport selection ----------------------------------------------------------


def test_transport_stdio(monkeypatch):
    monkeypatch.setenv("MCP_TRANSPORT", "stdio")
    assert _serve_captured(_mcp()) == {"transport": "stdio"}


def test_transport_typo_fails_closed(monkeypatch):
    monkeypatch.setenv("MCP_TRANSPORT", "sse")
    with pytest.raises(ValueError, match="MCP_TRANSPORT"):
        _serve_captured(_mcp())


def test_host_comes_from_env(monkeypatch):
    monkeypatch.setenv("MCP_HOST", "0.0.0.0")
    assert _serve_captured(_mcp())["host"] == "0.0.0.0"


# --- schema strip robustness ------------------------------------------------------


def test_strip_noops_when_fastmcp_internals_change():
    # Reaches a FastMCP internal; must degrade to a no-op, never crash the server.
    _strip_local_output_schemas(object())


# --- guardrail middleware: fail-closed messages name the actual cause --------------


def _screened_text(monkeypatch, fake_client) -> str:
    """Run one tool result through GuardrailMiddleware with httpx stubbed."""
    monkeypatch.setattr(_gmod.httpx, "AsyncClient", fake_client)
    mw = GuardrailMiddleware(guardrail_url="http://guard.test", source="t")
    ctx = SimpleNamespace(message=SimpleNamespace(name="fetch"))

    async def _ran(_ctx):
        return SimpleNamespace(content=[SimpleNamespace(text="external content")], is_error=False)

    out = asyncio.run(mw.on_call_tool(ctx, _ran))
    return out.content[0].text


def _client_returning(status_code: int, payload: dict | None = None):
    class _Client:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None):
            return _httpx.Response(
                status_code, json=payload or {}, request=_httpx.Request("POST", url)
            )

    return _Client


def _client_raising(exc: Exception):
    class _Client:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None):
            raise exc

    return _Client


def test_guardrail_unreachable_names_the_container(monkeypatch):
    # Container not deployed / crashed at startup: the withheld message says so and
    # points at the startup-config suspects instead of a generic "unavailable".
    text = _screened_text(monkeypatch, _client_raising(_httpx.ConnectError("refused")))
    assert "failing closed" in text.lower() or "Failing CLOSED" in text
    assert "unreachable" in text and "HF_TOKEN" in text and "BEDROCK_GUARDRAIL_ID" in text


def test_guardrail_warming_up_says_retry(monkeypatch):
    # 503 = service up, provider warming (first start downloads the model).
    text = _screened_text(monkeypatch, _client_returning(503, {"detail": "provider warming up"}))
    assert "starting up" in text and "retry" in text


def test_guardrail_allow_still_wraps(monkeypatch):
    text = _screened_text(monkeypatch, _client_returning(200, {"decision": "allow", "score": 0.0}))
    assert "untrusted_content" in text and "external content" in text


# --- /healthz: liveness + startup catalog registration ------------------------------


def _capture_posts(monkeypatch):
    posted = []

    class _Cap:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None):
            posted.append((url, json))
            return _httpx.Response(200, json={"ok": True}, request=_httpx.Request("POST", url))

    monkeypatch.setattr(_amod.httpx, "AsyncClient", _Cap)
    return posted


def test_healthz_startup_registers_the_catalog(monkeypatch):
    # The container healthcheck target doubles as startup registration: the manage
    # panel shows every DEPLOYED tool without waiting for a client, tagged
    # origin=startup so the sidecar does NOT count it as the source being used.
    posted = _capture_posts(monkeypatch)
    mcp = _mcp()
    _serve_captured(mcp, require_approval=True)
    with _StarletteClient(mcp.http_app()) as client:
        r = client.get("/healthz")
    assert r.status_code == 200 and r.json() == {"ok": True, "server": "t"}
    (url, payload) = posted[0]
    assert url.endswith("/catalog") and payload["origin"] == "startup"
    assert payload["source"] == "t"
    assert [t["name"] for t in payload["tools"]] == ["add"]


def test_healthz_without_approval_is_plain_liveness(monkeypatch):
    posted = _capture_posts(monkeypatch)
    mcp = _mcp()
    _serve_captured(mcp)
    with _StarletteClient(mcp.http_app()) as client:
        assert client.get("/healthz").status_code == 200
    assert posted == []


def test_healthz_answers_even_with_the_sidecar_down(monkeypatch):
    # Registration is best-effort: health must not depend on the approval sidecar.
    class _Boom:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            raise _httpx.ConnectError("refused")

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(_amod.httpx, "AsyncClient", _Boom)
    mcp = _mcp()
    _serve_captured(mcp, require_approval=True)
    with _StarletteClient(mcp.http_app()) as client:
        assert client.get("/healthz").status_code == 200
