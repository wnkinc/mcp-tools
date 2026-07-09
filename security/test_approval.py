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
    # Slack off by default: gate still creates approvals but reports notified=False
    # (Slack is the only human channel now -- the chat never gets a link).
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("SLACK_APPROVAL_CHANNEL", raising=False)


@pytest.fixture
def slack_ok(monkeypatch):
    """Pretend the Slack card was delivered (the notified=True path)."""

    async def _posted(token, action, source):
        return True

    monkeypatch.setattr(svc, "_slack_post_approval", _posted)


def _gate(client, source="teltool", action="send_message(m='hi')", key="k1"):
    return client.post("/gate", json={"source": source, "action": action, "call_key": key}).json()


def _token(source="teltool", key="k1"):
    """The pending record's capability token -- server state, never sent to the model."""
    return next(t for t, r in svc._PENDING.items() if r["key"] == f"{source}\x00{key}")


def _sole_token():
    """The only pending approval's token (middleware call_keys are hashes)."""
    (token,) = svc._PENDING
    return token


# --- the gate lifecycle --------------------------------------------------------------


def test_gate_creates_then_reports_pending(slack_ok):
    c = TestClient(svc.app)
    first = _gate(c)
    # No approve_url in the model-facing response: the link lives on the Slack card only.
    assert first == {"decision": "pending", "created": True, "notified": True}
    again = _gate(c)
    assert again == {"decision": "pending", "created": False, "notified": True}
    assert len(svc._PENDING) == 1  # same approval, not a new one per ask


def test_gate_reports_undelivered_slack():
    c = TestClient(svc.app)  # fixture default: Slack unconfigured
    assert _gate(c)["notified"] is False
    assert _gate(c) == {"decision": "pending", "created": False, "notified": False}


def test_unimplemented_provider_fails_closed(slack_ok, monkeypatch):
    # Slack delivery would succeed, but the configured provider isn't slack:
    # dispatch must not fall through to it -- the approval reports undeliverable.
    monkeypatch.setenv("APPROVAL_PROVIDER", "telegram")
    assert _gate(TestClient(svc.app))["notified"] is False


def test_healthz_reports_provider(monkeypatch):
    c = TestClient(svc.app)
    h = c.get("/healthz").json()
    assert h["ok"] is True and h["provider"] == "slack" and h["channel"] == "unconfigured"
    monkeypatch.setenv("APPROVAL_PROVIDER", "telegram")
    assert c.get("/healthz").json()["ok"] is False


def test_notify_dispatches_to_the_configured_provider(monkeypatch):
    delivered = []

    async def _discord(token, action, source):
        delivered.append(source)
        return True

    async def _slack(token, action, source):  # pragma: no cover - must not run
        raise AssertionError("slack called while APPROVAL_PROVIDER=discord")

    monkeypatch.setattr(svc, "_discord_post_approval", _discord)
    monkeypatch.setattr(svc, "_slack_post_approval", _slack)
    monkeypatch.setenv("APPROVAL_PROVIDER", "discord")
    assert _gate(TestClient(svc.app))["notified"] is True
    assert delivered == ["teltool"]


def test_healthz_channel_follows_active_provider(monkeypatch):
    c = TestClient(svc.app)
    monkeypatch.setenv("APPROVAL_PROVIDER", "discord")
    assert c.get("/healthz").json()["channel"] == "unconfigured"
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "x")
    monkeypatch.setenv("DISCORD_PUBLIC_KEY", "x")
    monkeypatch.setenv("DISCORD_APPROVAL_CHANNEL_ID", "1")
    assert c.get("/healthz").json()["channel"] == "configured"


def test_approve_allows_exactly_once():
    c = TestClient(svc.app)
    _gate(c)
    token = _token()
    page = c.get(f"/approve/{token}")
    assert page.status_code == 200 and "send_message" in page.text
    assert "Approved" in c.post(f"/approve/{token}", data={"decision": "approve"}).text
    assert _gate(c)["decision"] == "allow"
    # Consumed: the same call needs a FRESH approval next time.
    fresh = _gate(c)
    assert fresh["decision"] == "pending" and fresh["created"] is True


def test_deny_reported_once_then_recreates():
    c = TestClient(svc.app)
    _gate(c)
    c.post(f"/approve/{_token()}", data={"decision": "deny"})
    assert _gate(c)["decision"] == "denied"
    assert _gate(c)["created"] is True  # consumed; next ask starts a fresh approval


def test_get_of_the_page_never_decides():
    c = TestClient(svc.app)
    _gate(c)
    token = _token()
    c.get(f"/approve/{token}")
    c.get(f"/approve/{token}")
    assert _gate(c)["decision"] == "pending"


def test_approvals_are_scoped_per_tool():
    c = TestClient(svc.app)
    _gate(c, source="xmcp", key="samekey")
    _gate(c, source="telegram", key="samekey")
    c.post(f"/approve/{_token('xmcp', 'samekey')}", data={"decision": "approve"})
    assert _gate(c, source="xmcp", key="samekey")["decision"] == "allow"
    assert _gate(c, source="telegram", key="samekey")["decision"] == "pending"


def test_pending_expires_after_ttl():
    c = TestClient(svc.app)
    _gate(c)
    token = _token()
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
    _gate(c)
    assert _signed_interact(c, _token(), "approve", "wrong-secret").status_code == 403
    assert _gate(c)["decision"] == "pending"


def test_interact_approves_with_valid_signature(monkeypatch):
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "real-secret")
    c = TestClient(svc.app)
    _gate(c)
    assert _signed_interact(c, _token(), "approve", "real-secret").status_code == 200
    assert _gate(c)["decision"] == "allow"


# --- the Discord webhook: Ed25519 signature is the gate ------------------------------


@pytest.fixture
def discord_keys(monkeypatch):
    """A real Ed25519 keypair; the app verifies against the public half."""
    from nacl.signing import SigningKey

    sk = SigningKey.generate()
    monkeypatch.setenv("DISCORD_PUBLIC_KEY", sk.verify_key.encode().hex())
    return sk


def _discord_interact(client, payload, signing_key):
    body = json.dumps(payload).encode()
    ts = str(int(time.time()))
    sig = signing_key.sign(ts.encode() + body).signature.hex()
    return client.post(
        "/discord/interact",
        content=body,
        headers={
            "X-Signature-Timestamp": ts,
            "X-Signature-Ed25519": sig,
            "Content-Type": "application/json",
        },
    )


def _discord_click(client, token, action_id, signing_key):
    return _discord_interact(
        client, {"type": 3, "data": {"custom_id": f"{action_id}:{token}"}}, signing_key
    )


def test_discord_ping_pongs(discord_keys):
    resp = _discord_interact(TestClient(svc.app), {"type": 1}, discord_keys)
    assert resp.status_code == 200 and resp.json() == {"type": 1}


def test_discord_rejects_bad_signature(discord_keys):
    from nacl.signing import SigningKey

    c = TestClient(svc.app)
    _gate(c)
    wrong_key = SigningKey.generate()  # not the key the app trusts
    assert _discord_click(c, _token(), "approve", wrong_key).status_code == 401
    assert _gate(c)["decision"] == "pending"


def test_discord_approve_allows(discord_keys):
    c = TestClient(svc.app)
    _gate(c)
    resp = _discord_click(c, _token(), "approve", discord_keys)
    # Type 7 replaces the card in place, removing the buttons.
    assert resp.status_code == 200 and resp.json()["type"] == 7
    assert resp.json()["data"]["components"] == []
    assert _gate(c)["decision"] == "allow"


def test_discord_deny_denies(discord_keys):
    c = TestClient(svc.app)
    _gate(c)
    _discord_click(c, _token(), "deny", discord_keys)
    assert _gate(c)["decision"] == "denied"


def test_discord_expired_token_decides_nothing(discord_keys):
    c = TestClient(svc.app)
    _gate(c)
    resp = _discord_click(c, "not-a-token", "approve", discord_keys)
    assert "expired" in resp.json()["data"]["content"]
    assert _gate(c)["decision"] == "pending"


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


def _pending_text(mw):
    return asyncio.run(mw.on_call_tool(_ctx(), _ran)).content[0].text


def test_middleware_gates_then_allows_after_card_approval(slack_ok, monkeypatch):
    mw = _middleware_against_service(monkeypatch)
    first = _pending_text(mw)
    assert "NOT performed" in first and "approval channel" in first
    still = _pending_text(mw)
    assert "still awaiting" in still
    TestClient(svc.app).post(f"/approve/{_sole_token()}", data={"decision": "approve"})
    assert asyncio.run(mw.on_call_tool(_ctx(), _ran)) == "TOOL-RAN"


def test_middleware_messages_carry_no_link_or_directives(slack_ok, monkeypatch):
    # The injection-shaped patterns that got pending messages flagged and refused:
    # a URL to relay, and instructions addressed to the assistant. Never again.
    mw = _middleware_against_service(monkeypatch)
    for text in (_pending_text(mw), _pending_text(mw)):  # created, then still-pending
        assert "http" not in text and "INSTRUCTIONS" not in text


def test_middleware_fails_loud_when_slack_undelivered(monkeypatch):
    # Slack unconfigured (fixture default): the human was never notified, so the
    # model must be told approval CANNOT arrive -- not left waiting politely.
    mw = _middleware_against_service(monkeypatch)
    for text in (_pending_text(mw), _pending_text(mw)):  # created, then re-asked
        assert "could not be delivered" in text and "NOT" in text


def test_middleware_reports_denial(slack_ok, monkeypatch):
    mw = _middleware_against_service(monkeypatch)
    _pending_text(mw)
    TestClient(svc.app).post(f"/approve/{_sole_token()}", data={"decision": "deny"})
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
