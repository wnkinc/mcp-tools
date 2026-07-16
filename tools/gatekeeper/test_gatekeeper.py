"""set_gating over the real wire shape: the tool posts /gating to the approval
sidecar and relays ok/refused -- so run it against the actual service app (ASGI
transport, no network) and assert both message paths."""

import asyncio
import importlib.util
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gk = _load("gatekeeper_server", ROOT / "tools" / "gatekeeper" / "server.py")
svc = _load("approval_service", ROOT / "security" / "approval" / "service" / "service.py")


@pytest.fixture()
def against_service(monkeypatch):
    """Route the tool's own httpx client at the sidecar app in-process."""

    class _ASGIClient(httpx.AsyncClient):
        def __init__(self, **kw):
            kw["transport"] = httpx.ASGITransport(app=svc.app)
            super().__init__(**kw)

    monkeypatch.setattr(gk.httpx, "AsyncClient", _ASGIClient)
    svc._STATE.clear()


def test_set_gating_applies_and_reports(against_service):
    out = asyncio.run(gk.set_gating(tool="send_message", mode="blocked", source="telegram"))
    assert "✅" in out and "`send_message` on telegram is now blocked" in out
    assert svc._effective_modes("telegram") == {"send_message": "blocked"}


def test_set_gating_relays_a_refusal(against_service):
    # The gatekeeper's own tools are fixed in code; the sidecar refuses and the
    # tool must surface that refusal, not report success.
    out = asyncio.run(gk.set_gating(tool="set_gating", mode="always_allow", source="gatekeeper"))
    assert "⚠️" in out and "refused" in out
    assert svc._effective_modes("gatekeeper") == {"set_gating": "needs_approval"}
