"""Approval sidecar + middleware tests -- no Slack, no network.

The service is exercised in-process (Starlette TestClient); the middleware is
exercised END-TO-END against that same app over an ASGI transport, pinning the
contract the two sides share: gate semantics (pending -> approved -> one-time
allow), per-tool scoping, human-only decisions, and fail-closed behavior.
"""

import asyncio
import hashlib
import hmac
import importlib.util
import json
import time
import urllib.parse
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from starlette.testclient import TestClient

_SPEC = importlib.util.spec_from_file_location(
    "approval_service", Path(__file__).parent / "approval" / "service" / "service.py"
)
svc = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(svc)

from security.approval.middleware import ApprovalMiddleware  # noqa: E402

PUBLIC = "https://approval.example.com"


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    svc._PENDING.clear()
    monkeypatch.setenv("APPROVAL_PUBLIC_URL", PUBLIC)
    # Slack off: gate must work with the page link alone.
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("SLACK_APPROVAL_CHANNEL", raising=False)


def _gate(client, source="teltool", action="send_message(m='hi')", key="k1"):
    return client.post("/gate", json={"source": source, "action": action, "call_key": key}).json()


def _token(data):
    return data["approve_url"].rsplit("/", 1)[1]


# --- the gate lifecycle --------------------------------------------------------------


def test_gate_creates_then_reports_pending():
    c = TestClient(svc.app)
    first = _gate(c)
    assert first == {
        "decision": "pending",
        "created": True,
        "approve_url": f"{PUBLIC}/approve/{_token(first)}",
    }
    again = _gate(c)
    assert again["created"] is False and _token(again) == _token(first)


def test_approve_allows_exactly_once():
    c = TestClient(svc.app)
    token = _token(_gate(c))
    page = c.get(f"/approve/{token}")
    assert page.status_code == 200 and "send_message" in page.text
    assert "Approved" in c.post(f"/approve/{token}", data={"decision": "approve"}).text
    assert _gate(c)["decision"] == "allow"
    # Consumed: the same call needs a FRESH approval next time.
    fresh = _gate(c)
    assert fresh["decision"] == "pending" and fresh["created"] is True


def test_deny_reported_once_then_recreates():
    c = TestClient(svc.app)
    token = _token(_gate(c))
    c.post(f"/approve/{token}", data={"decision": "deny"})
    assert _gate(c)["decision"] == "denied"
    assert _gate(c)["created"] is True  # consumed; next ask starts a fresh approval


def test_get_of_the_page_never_decides():
    c = TestClient(svc.app)
    token = _token(_gate(c))
    c.get(f"/approve/{token}")
    c.get(f"/approve/{token}")
    assert _gate(c)["decision"] == "pending"


def test_approvals_are_scoped_per_tool():
    c = TestClient(svc.app)
    a = _gate(c, source="xmcp", key="samekey")
    _gate(c, source="telegram", key="samekey")
    c.post(f"/approve/{_token(a)}", data={"decision": "approve"})
    assert _gate(c, source="xmcp", key="samekey")["decision"] == "allow"
    assert _gate(c, source="telegram", key="samekey")["decision"] == "pending"


def test_pending_expires_after_ttl():
    c = TestClient(svc.app)
    token = _token(_gate(c))
    svc._PENDING[token]["created"] -= svc._TTL_SECONDS + 1
    assert _gate(c)["created"] is True  # old record pruned, new one minted
    assert c.get(f"/approve/{token}").status_code == 404


def test_unknown_token_is_404():
    assert TestClient(svc.app).get("/approve/nope").status_code == 404


# --- the Slack webhook: signature is the gate ----------------------------------------


def _signed_interact(client, token, action_id, secret):
    payload = json.dumps({"actions": [{"action_id": action_id, "value": token}]})
    body = urllib.parse.urlencode({"payload": payload}).encode()
    ts = str(int(time.time()))
    sig = (
        "v0="
        + hmac.new(secret.encode(), b"v0:" + ts.encode() + b":" + body, hashlib.sha256).hexdigest()
    )
    return client.post(
        "/slack/interact",
        content=body,
        headers={
            "X-Slack-Request-Timestamp": ts,
            "X-Slack-Signature": sig,
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )


def test_interact_rejects_bad_signature(monkeypatch):
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "real-secret")
    c = TestClient(svc.app)
    token = _token(_gate(c))
    assert _signed_interact(c, token, "approve", "wrong-secret").status_code == 403
    assert _gate(c)["decision"] == "pending"


def test_interact_approves_with_valid_signature(monkeypatch):
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "real-secret")
    c = TestClient(svc.app)
    token = _token(_gate(c))
    assert _signed_interact(c, token, "approve", "real-secret").status_code == 200
    assert _gate(c)["decision"] == "allow"


# --- the middleware, end-to-end against the real service app -------------------------


def _middleware_against_service(monkeypatch, **kwargs):
    mw = ApprovalMiddleware(approval_url="http://approval.test", source="teltool", **kwargs)
    import security.approval.middleware as mwmod

    real_client = httpx.AsyncClient

    def asgi_client(**kw):
        kw.pop("timeout", None)
        return real_client(transport=httpx.ASGITransport(app=svc.app), **kw)

    monkeypatch.setattr(mwmod.httpx, "AsyncClient", asgi_client)
    return mw


def _ctx(tool="send_message", args=None):
    return SimpleNamespace(message=SimpleNamespace(name=tool, arguments=args or {"m": "hi"}))


async def _ran(_ctx):
    return "TOOL-RAN"


def test_middleware_gates_then_allows_after_page_approval(monkeypatch):
    mw = _middleware_against_service(monkeypatch)
    first = asyncio.run(mw.on_call_tool(_ctx(), _ran))
    text = first.content[0].text
    assert "APPROVAL REQUIRED" in text and f"{PUBLIC}/approve/" in text
    token = text.split(f"{PUBLIC}/approve/")[1].split()[0]
    TestClient(svc.app).post(f"/approve/{token}", data={"decision": "approve"})
    assert asyncio.run(mw.on_call_tool(_ctx(), _ran)) == "TOOL-RAN"


def test_middleware_reports_denial(monkeypatch):
    mw = _middleware_against_service(monkeypatch)
    first = asyncio.run(mw.on_call_tool(_ctx(), _ran))
    token = first.content[0].text.split(f"{PUBLIC}/approve/")[1].split()[0]
    TestClient(svc.app).post(f"/approve/{token}", data={"decision": "deny"})
    out = asyncio.run(mw.on_call_tool(_ctx(), _ran))
    assert "denied" in out.content[0].text


def test_middleware_exempt_tools_never_touch_the_service(monkeypatch):
    mw = _middleware_against_service(monkeypatch, exempt={"get_me"})

    def boom(**kw):  # any HTTP attempt = failure
        raise AssertionError("exempt tool contacted the approval service")

    import security.approval.middleware as mwmod

    monkeypatch.setattr(mwmod.httpx, "AsyncClient", boom)
    assert asyncio.run(mw.on_call_tool(_ctx(tool="get_me"), _ran)) == "TOOL-RAN"


def test_middleware_fails_closed_when_service_unreachable():
    mw = ApprovalMiddleware(approval_url="http://127.0.0.1:9", source="t", timeout=0.5)
    out = asyncio.run(mw.on_call_tool(_ctx(), _ran))
    assert "failing CLOSED" in out.content[0].text and "NOT performed" in out.content[0].text
