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
import re
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

from security.approval import gating as gating_mod  # noqa: E402
from security.approval.middleware import ApprovalMiddleware  # noqa: E402

PUBLIC = "https://approval.example.com"


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    svc._PENDING.clear()
    svc._STATE.clear()
    svc._MANAGE.clear()
    gating_mod._cache.clear()  # the TTL cache must not leak modes across tests
    monkeypatch.delenv("APPROVAL_STATE_FILE", raising=False)  # memory-only in tests
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
    # No approve_url in the model-facing response: the link lives on the card only.
    # channel_label names the active provider so the model-facing message matches it;
    # token is the capability token, returned ONLY to the trusted server-side caller
    # (never model-visible) so an in-chat approval widget can redeem it.
    assert first["decision"] == "pending" and first["created"] is True
    assert first["notified"] is True and first["channel_label"] == "Slack"
    assert first["token"] == _token()
    again = _gate(c)
    assert again == {
        "decision": "pending",
        "created": False,
        "notified": True,
        "channel_label": "Slack",
        "token": first["token"],  # re-ask returns the SAME pending's token (for a re-render)
    }
    assert len(svc._PENDING) == 1  # same approval, not a new one per ask


def test_gate_reports_undelivered_slack():
    c = TestClient(svc.app)  # fixture default: Slack unconfigured
    assert _gate(c)["notified"] is False
    assert _gate(c) == {
        "decision": "pending",
        "created": False,
        "notified": False,
        "channel_label": "Slack",
        "token": _token(),
    }


def test_unimplemented_provider_fails_closed(slack_ok, monkeypatch):
    # Slack delivery would succeed, but the configured provider isn't slack:
    # dispatch must not fall through to it -- the approval reports undeliverable.
    monkeypatch.setenv("APPROVAL_PROVIDER", "whatsapp")
    assert _gate(TestClient(svc.app))["notified"] is False


def test_healthz_reports_provider(monkeypatch):
    c = TestClient(svc.app)
    h = c.get("/healthz").json()
    assert h["ok"] is True and h["provider"] == "slack" and h["channel"] == "unconfigured"
    monkeypatch.setenv("APPROVAL_PROVIDER", "whatsapp")
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


# --- the Telegram webhook: the setWebhook secret token is the gate -------------------


@pytest.fixture
def telegram_env(monkeypatch):
    """Telegram provider configured; the outbound bot API is stubbed (no network)."""
    monkeypatch.setenv("APPROVAL_PROVIDER", "telegram")
    monkeypatch.setenv("TELEGRAM_APPROVAL_BOT_TOKEN", "bot-token")
    monkeypatch.setenv("TELEGRAM_APPROVAL_CHAT_ID", "555")
    monkeypatch.setenv("TELEGRAM_APPROVAL_WEBHOOK_SECRET", "hook-secret")
    calls = []

    async def _rec(client, bot_token, method, payload):
        calls.append((method, payload))
        return SimpleNamespace(status_code=200, json=lambda: {"ok": True})

    monkeypatch.setattr(svc, "_telegram_call", _rec)
    return calls


def _telegram_click(client, token, action_id, secret="hook-secret"):
    payload = {
        "callback_query": {
            "id": "cq1",
            "data": f"{action_id}:{token}",
            "message": {"message_id": 7, "chat": {"id": 555}},
        }
    }
    return client.post(
        "/telegram/interact",
        content=json.dumps(payload).encode(),
        headers={
            "X-Telegram-Bot-Api-Secret-Token": secret,
            "Content-Type": "application/json",
        },
    )


def test_telegram_gate_posts_card(telegram_env):
    # notified=True proves _telegram_post_approval ran its sendMessage stub.
    assert _gate(TestClient(svc.app))["notified"] is True
    assert telegram_env[0][0] == "sendMessage"
    assert "callback_data" in json.dumps(telegram_env[0][1])


def test_telegram_rejects_bad_secret(telegram_env):
    c = TestClient(svc.app)
    _gate(c)
    assert _telegram_click(c, _token(), "approve", secret="wrong").status_code == 403
    assert _gate(c)["decision"] == "pending"


def test_telegram_approve_allows(telegram_env):
    c = TestClient(svc.app)
    _gate(c)
    telegram_env.clear()  # drop the sendMessage from _gate; watch the click's edits
    assert _telegram_click(c, _token(), "approve").status_code == 200
    assert _gate(c)["decision"] == "allow"
    # The click clears the spinner and edits the card to remove the buttons.
    methods = [m for m, _ in telegram_env]
    assert methods == ["answerCallbackQuery", "editMessageText"]
    assert telegram_env[1][1]["reply_markup"] == {"inline_keyboard": []}


def test_telegram_deny_denies(telegram_env):
    c = TestClient(svc.app)
    _gate(c)
    _telegram_click(c, _token(), "deny")
    assert _gate(c)["decision"] == "denied"


def test_telegram_expired_token_decides_nothing(telegram_env):
    c = TestClient(svc.app)
    _gate(c)
    assert _telegram_click(c, "not-a-token", "approve").status_code == 200
    assert _gate(c)["decision"] == "pending"


def test_healthz_channel_follows_telegram(telegram_env, monkeypatch):
    c = TestClient(svc.app)
    h = c.get("/healthz").json()
    assert h["provider"] == "telegram" and h["ok"] is True and h["channel"] == "configured"
    monkeypatch.delenv("TELEGRAM_APPROVAL_WEBHOOK_SECRET")  # secret is part of "configured"
    assert c.get("/healthz").json()["channel"] == "unconfigured"


# --- the middleware, end-to-end against the real service app -------------------------


def _set_mode(client, tool, mode, source="teltool"):
    return client.post("/gating", json={"source": source, "tool": tool, "mode": mode})


def _middleware_against_service(monkeypatch, gated=("send_message",), **kwargs):
    """Middleware wired to the in-process service; `gated` tools get needs_approval
    set up front (nothing is gated by default anymore)."""
    mw = ApprovalMiddleware(approval_url="http://approval.test", source="teltool", **kwargs)
    import security.approval.middleware as mwmod

    real_client = httpx.AsyncClient

    def asgi_client(**kw):
        kw.pop("timeout", None)
        return real_client(transport=httpx.ASGITransport(app=svc.app), **kw)

    # Both the middleware's own calls (/gate, /catalog) and the gating module's
    # mode fetches (/gating) must reach the in-process service.
    monkeypatch.setattr(mwmod.httpx, "AsyncClient", asgi_client)
    monkeypatch.setattr(gating_mod.httpx, "AsyncClient", asgi_client)
    for tool in gated:
        _set_mode(TestClient(svc.app), tool, "needs_approval")
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


def test_middleware_names_the_active_provider(telegram_env, monkeypatch):
    # The gate hands back channel_label for the ACTIVE provider; the model-facing
    # message must say "Telegram" (the live channel) and never list the others.
    mw = _middleware_against_service(monkeypatch)
    text = _pending_text(mw)
    assert "Telegram approval channel" in text
    assert "Slack" not in text and "Discord" not in text


def test_middleware_widget_mode_prose_for_model_token_for_widget(slack_ok, monkeypatch):
    # Widget mode: the model reads explicit prose (so it re-calls and doesn't claim
    # premature success); the token for the card rides an HTML-comment marker.
    mw = _middleware_against_service(monkeypatch, widget=True)
    text = asyncio.run(mw.on_call_tool(_ctx(), _ran)).content[0].text
    assert "was NOT performed" in text and "call this same tool again" in text
    marker = re.search(r"<!--APPROVAL\s+(\{.*?\})\s*-->", text)
    assert marker, "widget marker missing"
    payload = json.loads(marker.group(1))
    assert payload["token"] == _sole_token() and "send_message" in payload["action"]


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


def test_middleware_default_tool_runs_without_gate(monkeypatch):
    # No stored mode -> always_allow: the tool runs and no approval is ever created.
    mw = _middleware_against_service(monkeypatch)
    assert asyncio.run(mw.on_call_tool(_ctx(tool="get_me"), _ran)) == "TOOL-RAN"
    assert svc._PENDING == {}


def test_middleware_fails_closed_when_service_unreachable():
    # Never-answered sidecar: everything is needs_approval, and the gate itself
    # can't be reached either -- so nothing runs. An outage must not ship-open.
    mw = ApprovalMiddleware(approval_url="http://127.0.0.1:9", source="t", timeout=0.5)
    out = asyncio.run(mw.on_call_tool(_ctx(), _ran))
    assert "failing CLOSED" in out.content[0].text and "NOT performed" in out.content[0].text


# --- tool modes: the sidecar as sole authority ----------------------------------------


def test_gating_stores_and_returns_modes():
    c = TestClient(svc.app)
    for tool, mode in [("a", "always_allow"), ("b", "needs_approval"), ("c", "blocked")]:
        assert _set_mode(c, tool, mode).json()["ok"] is True
    got = c.get("/gating", params={"source": "teltool"}).json()
    assert got["modes"] == {"a": "always_allow", "b": "needs_approval", "c": "blocked"}
    # Scoped per source: another source sees nothing.
    assert c.get("/gating", params={"source": "other"}).json()["modes"] == {}


def test_gating_rejects_unknown_mode():
    c = TestClient(svc.app)
    assert _set_mode(c, "a", "sideways").status_code == 400
    assert c.get("/gating", params={"source": "teltool"}).json()["modes"] == {}


def test_gating_pins_are_reported_and_immutable():
    # set_gating on the gatekeeper is a code constant: always reported as
    # needs_approval, and no POST can change it.
    c = TestClient(svc.app)
    assert c.get("/gating", params={"source": "gatekeeper"}).json()["modes"] == {
        "set_gating": "needs_approval"
    }
    resp = _set_mode(c, "set_gating", "always_allow", source="gatekeeper")
    assert resp.status_code == 403 and "fixed in code" in resp.json()["error"]
    assert c.get("/gating", params={"source": "gatekeeper"}).json()["modes"] == {
        "set_gating": "needs_approval"
    }


def test_gatekeeper_source_is_wholly_unmanageable():
    # The whole gatekeeper source is off-limits, not just the pinned tool: no mode
    # write lands on ANY of its tools, and stored modes for it (say, a hand-edited
    # state file) are ignored -- code is the only authority on how the gatekeeper
    # itself behaves.
    c = TestClient(svc.app)
    assert _set_mode(c, "manage_tools", "blocked", source="gatekeeper").status_code == 403
    svc._STATE["gatekeeper"] = {"catalog": {}, "modes": {"manage_tools": "blocked"}}
    assert c.get("/gating", params={"source": "gatekeeper"}).json()["modes"] == {
        "set_gating": "needs_approval"
    }


def test_mode_for_defaults_open_but_fails_closed_on_no_data():
    assert gating_mod.mode_for("anything", {}) == "always_allow"  # ship-open default
    assert gating_mod.mode_for("x", {"x": "blocked"}) == "blocked"
    assert gating_mod.mode_for("anything", None) == "needs_approval"  # sidecar never answered


def test_fetch_modes_normalizes_unknown_values(monkeypatch):
    # Corrupt/stale stored values must fail SAFE (needs_approval), never open.
    svc._STATE["teltool"] = {"catalog": {}, "modes": {"a": "blocked", "b": "bogus"}}
    _middleware_against_service(monkeypatch, gated=())
    got = asyncio.run(gating_mod.fetch_modes("teltool", "http://approval.test"))
    assert got == {"a": "blocked", "b": "needs_approval"}


def test_state_persists_across_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("APPROVAL_STATE_FILE", str(tmp_path / "state.json"))
    c = TestClient(svc.app)
    c.post(
        "/catalog",
        json={"source": "teltool", "tools": [{"name": "send_message", "read_only": False}]},
    )
    _set_mode(c, "send_message", "blocked")
    svc._STATE.clear()  # simulate a sidecar restart
    svc._load_state()
    assert c.get("/gating", params={"source": "teltool"}).json()["modes"] == {
        "send_message": "blocked"
    }
    assert "send_message" in c.get("/catalog", params={"source": "teltool"}).json()["tools"]


def test_middleware_blocked_tool_refuses_without_approval_path(monkeypatch):
    # Blocked = disabled outright: a stale client tool list must not be able to run
    # it, and no approval is created (there is nothing to approve).
    mw = _middleware_against_service(monkeypatch, gated=())
    _set_mode(TestClient(svc.app), "send_message", "blocked")
    text = asyncio.run(mw.on_call_tool(_ctx(), _ran)).content[0].text
    assert "disabled" in text and "not performed" in text
    assert "http" not in text  # same no-link/no-directive rule as pending messages
    assert svc._PENDING == {}


# --- the tool list: catalog registration + blocked filtering --------------------------


def _tool(name, read_only=False):
    # read_only=None models a tool with NO annotations (the MCP spec default applies:
    # not read-only, same as Claude's own grouping).
    return SimpleNamespace(
        name=name,
        description=f"{name} does things",
        annotations=None if read_only is None else SimpleNamespace(readOnlyHint=read_only),
    )


async def _list_via(mw, tools):
    async def _inner(_ctx):
        return tools

    return await mw.on_list_tools(None, _inner)


def test_middleware_list_filters_blocked_tools(monkeypatch):
    mw = _middleware_against_service(monkeypatch, gated=())
    _set_mode(TestClient(svc.app), "send_message", "blocked")
    out = asyncio.run(_list_via(mw, [_tool("send_message"), _tool("get_me", read_only=True)]))
    assert [t.name for t in out] == ["get_me"]


def test_middleware_list_registers_the_full_catalog(monkeypatch):
    # The catalog is the operator's UNFILTERED view: blocked tools are registered
    # too, with their read/write classification and effective mode.
    mw = _middleware_against_service(monkeypatch, gated=())
    _set_mode(TestClient(svc.app), "send_message", "blocked")
    asyncio.run(
        _list_via(
            mw,
            [
                _tool("send_message"),
                _tool("get_me", read_only=True),
                _tool("probe", read_only=None),
            ],
        )
    )
    tools = TestClient(svc.app).get("/catalog", params={"source": "teltool"}).json()["tools"]
    assert tools["send_message"] == {
        "description": "send_message does things",
        "read_only": False,
        "mode": "blocked",
    }
    assert tools["get_me"]["read_only"] is True and tools["get_me"]["mode"] == "always_allow"
    # No annotations -> the spec default: NOT read-only, exactly as Claude's UI
    # groups an annotation-less tool under Interactive.
    assert tools["probe"]["read_only"] is False


def test_middleware_list_survives_a_down_sidecar():
    # No sidecar ever answered: the list passes through unfiltered (nothing is
    # known to be blocked) -- the call path is what fails closed.
    mw = ApprovalMiddleware(approval_url="http://127.0.0.1:9", source="t", timeout=0.5)
    out = asyncio.run(_list_via(mw, [_tool("send_message")]))
    assert [t.name for t in out] == ["send_message"]


# --- manage sessions: the permissions widget's capability API -------------------------


def _mint(client):
    return client.post("/manage", json={}).json()["token"]


def test_manage_session_serves_every_registered_connector():
    c = TestClient(svc.app)
    c.post(
        "/catalog",
        json={
            "source": "teltool",
            "tools": [
                {"name": "send_message", "read_only": False, "description": "d"},
                {"name": "get_me", "read_only": True},
            ],
        },
    )
    c.post("/catalog", json={"source": "gatekeeper", "tools": [{"name": "set_gating"}]})
    _set_mode(c, "send_message", "blocked")
    r = c.get(f"/manage/{_mint(c)}")
    # The widget iframe reads this cross-origin: CORS must be open (token = credential).
    assert r.headers["access-control-allow-origin"] == "*"
    sources = r.json()["sources"]
    assert sources["teltool"]["tools"]["send_message"] == {
        "description": "d",
        "read_only": False,
        "mode": "blocked",
    }
    assert sources["teltool"]["tools"]["get_me"]["mode"] == "always_allow"
    assert sources["teltool"]["pinned"] == []
    # The gatekeeper never appears in the panel, even with a registered catalog:
    # its own tools aren't manageable (set_gating pinned, manage_tools human-driven).
    assert "gatekeeper" not in sources


def test_manage_save_applies_persists_and_is_one_shot(tmp_path, monkeypatch):
    monkeypatch.setenv("APPROVAL_STATE_FILE", str(tmp_path / "s.json"))
    c = TestClient(svc.app)
    token = _mint(c)
    r = c.post(
        f"/manage/{token}",
        json={
            "changes": {
                "teltool": {"send_message": "blocked"},
                "othertool": {"get_me": "needs_approval"},
            }
        },
    ).json()
    assert r["applied"] == 2 and r["refused"] == {}
    # A widget save is a first-class choice: enforced via /gating and restart-proof.
    svc._STATE.clear()
    svc._load_state()
    assert c.get("/gating", params={"source": "teltool"}).json()["modes"] == {
        "send_message": "blocked"
    }
    assert c.get("/gating", params={"source": "othertool"}).json()["modes"] == {
        "get_me": "needs_approval"
    }
    # ONE-SHOT: a successful save consumed the session (its snapshot is stale now) --
    # the same token neither saves again nor serves the catalog. Fresh call, fresh view.
    again = c.post(
        f"/manage/{token}", json={"changes": {"teltool": {"send_message": "always_allow"}}}
    )
    assert again.status_code == 404
    assert c.get(f"/manage/{token}").status_code == 404
    assert c.get("/gating", params={"source": "teltool"}).json()["modes"] == {
        "send_message": "blocked"  # the replayed save changed nothing
    }


def test_manage_save_with_invalid_modes_keeps_the_session():
    # A 400 (nothing applied) must not burn the one-shot token: the user fixes the
    # widget state and saves again on the same session.
    c = TestClient(svc.app)
    token = _mint(c)
    assert c.post(f"/manage/{token}", json={"changes": {"t": {"a": "sideways"}}}).status_code == 400
    ok = c.post(f"/manage/{token}", json={"changes": {"t": {"a": "blocked"}}}).json()
    assert ok["ok"] is True and ok["applied"] == 1


def test_manage_save_respects_pins_and_validates_modes():
    c = TestClient(svc.app)
    r = c.post(
        f"/manage/{_mint(c)}",
        json={"changes": {"gatekeeper": {"set_gating": "always_allow", "manage_tools": "blocked"}}},
    ).json()
    # A save cannot touch the gatekeeper's own tools any more than set_gating can.
    assert r["applied"] == 0 and r["refused"] == {"gatekeeper": ["set_gating", "manage_tools"]}
    assert c.get("/gating", params={"source": "gatekeeper"}).json()["modes"] == {
        "set_gating": "needs_approval"
    }
    bad = c.post(f"/manage/{_mint(c)}", json={"changes": {"teltool": {"a": "sideways"}}})
    assert bad.status_code == 400


def test_manage_token_expires_and_unknown_is_404():
    c = TestClient(svc.app)
    token = _mint(c)
    svc._MANAGE[token]["created"] -= svc._TTL_SECONDS + 1
    assert c.get(f"/manage/{token}").status_code == 404
    assert c.post("/manage/nope", json={"changes": {}}).status_code == 404


def test_manage_preflight_is_answered():
    r = TestClient(svc.app).options("/manage/whatever")
    assert r.status_code == 204
    assert r.headers["access-control-allow-methods"] == "GET, POST, OPTIONS"
    assert r.headers["access-control-allow-headers"] == "content-type"


# --- the gatekeeper manage_tools wrapper (server side of the widget) ------------------

import security.approval.manage_widget as mgmt  # noqa: E402


class _FakeMCP:
    """Minimal stand-in exposing only what register_manage_widget touches, so the
    decorated manage_tools closure and the resource registration are captured for
    direct testing -- no FastMCP internals, no browser."""

    name = "gatekeeper"

    def __init__(self):
        self.resources = {}  # uri -> handler
        self.tools = {}  # name -> async fn
        self.tool_meta = None

    def resource(self, uri, **kwargs):
        def deco(fn):
            self.resources[uri] = fn
            return fn

        return deco

    def tool(self, *args, **kwargs):
        self.tool_meta = kwargs.get("meta")

        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn

        return deco


def _register_manage(monkeypatch, working=True):
    monkeypatch.setenv("APPROVAL_URL", "http://approval.test")
    fake = _FakeMCP()
    mgmt.register_manage_widget(fake)
    if working:
        real = httpx.AsyncClient

        def asgi(**kw):
            kw.pop("timeout", None)
            return real(transport=httpx.ASGITransport(app=svc.app), **kw)

        monkeypatch.setattr(mgmt.httpx, "AsyncClient", asgi)
    else:

        def boom(**kw):
            raise RuntimeError("sidecar unreachable")

        monkeypatch.setattr(mgmt.httpx, "AsyncClient", boom)
    return fake


def test_manage_tools_returns_a_redeemable_marker(monkeypatch):
    fake = _register_manage(monkeypatch)
    text = asyncio.run(fake.tools["manage_tools"]())
    marker = re.search(r"<!--MANAGE\s+(\{.*?\})\s*-->", text)
    assert marker, "manage_tools must emit the widget marker"
    token = json.loads(marker.group(1))["token"]
    # The token it minted actually works against the sidecar the widget will call.
    assert TestClient(svc.app).get(f"/manage/{token}").json()["ok"] is True
    # No approval-shaped directives/links leak into the model-facing prose.
    assert "http" not in text


def test_manage_tools_fails_soft_when_sidecar_down(monkeypatch):
    fake = _register_manage(monkeypatch, working=False)
    text = asyncio.run(fake.tools["manage_tools"]())
    assert "could not be opened" in text and "Nothing was changed" in text
    assert "<!--MANAGE" not in text  # no token, no card to render


def test_register_manage_widget_wires_the_ui_resource(monkeypatch):
    fake = _register_manage(monkeypatch)
    # The tool is tagged with the ui:// resource so the host renders the panel,
    # and that exact resource is registered (served HTML, not a dangling URI).
    uri = fake.tool_meta["ui"]["resourceUri"]
    assert uri.startswith("ui://manage.gatekeeper.")
    assert uri in fake.resources
    assert "Tool permissions" in fake.resources[uri]()  # the widget HTML
