"""deploy/host/reconcile.py -- the host-side deploy executor. Tested with compose
stubbed (a recording fake `docker` on PATH): validation, secrets staging, profile
editing, and the request/status consumption protocol."""

import importlib.util
import json
import os
import stat
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "reconcile", Path(__file__).parents[1] / "deploy" / "host" / "reconcile.py"
)
rec = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(rec)


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    """A minimal fake repo: one shipped tool (weather), control dir, root .env,
    and a fake `docker` that records its argv and reports one running service."""
    (tmp_path / "tools" / "weather").mkdir(parents=True)
    (tmp_path / "tools" / "weather" / "deploy.json").write_text(
        json.dumps(
            {
                "title": "Weather",
                "profile": "weather",
                "subdomain": "weather",
                "port": 8070,
                "summary": "weather",
                "secrets": [{"key": "W_KEY", "label": "Weather key", "hint": "example.com"}],
                "notes": [],
                "depends": [],
            }
        )
    )
    (tmp_path / ".env").write_text("MCP_DOMAIN=example.com\nCOMPOSE_PROFILES=xmcp,data\n")
    (tmp_path / "docker-compose.yml").touch()
    (tmp_path / "docker-compose.tunnel.yml").touch()
    rec.control_dir(tmp_path).mkdir(parents=True)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "docker"
    fake.write_text(
        "#!/bin/bash\n"
        f'echo "$@" >> "{tmp_path}/docker.log"\n'
        'for a in "$@"; do [ "$a" = ps ] && echo xmcp; done\n'
        "exit 0\n"
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    return tmp_path


def _request(repo_path, tool="weather", rid="req1", action="deploy"):
    rec.write_json(
        rec.control_dir(repo_path) / "request.json",
        {"id": rid, "action": action, "tool": tool},
    )


def _status(repo_path):
    return rec.read_json(rec.control_dir(repo_path) / "status.json")


def test_add_profile_is_idempotent_and_preserves_the_rest():
    env = "MCP_DOMAIN=x\nCOMPOSE_PROFILES=xmcp,data\nHF_TOKEN=t\n"
    once = rec.add_profile(env, "weather")
    assert "COMPOSE_PROFILES=xmcp,data,weather" in once and "HF_TOKEN=t" in once
    assert rec.add_profile(once, "weather") == once


def test_inventory_reports_deployment_and_secret_staging(repo):
    rec.run_once(repo)
    inv = rec.read_json(rec.control_dir(repo) / "inventory.json")["tools"]["weather"]
    assert inv == {"deployed": False, "secrets_ready": False, "missing_secrets": ["W_KEY"]}
    (repo / "tools" / "weather" / ".env").write_text("W_KEY=abc\n")
    rec.run_once(repo)
    inv = rec.read_json(rec.control_dir(repo) / "inventory.json")["tools"]["weather"]
    assert inv["secrets_ready"] is True and inv["missing_secrets"] == []


def test_deploy_request_applies_and_is_consumed_once(repo):
    (repo / "tools" / "weather" / ".env").write_text("W_KEY=abc\n")
    _request(repo)
    rec.run_once(repo)
    st = _status(repo)
    assert st["phase"] == "done" and st["last_id"] == "req1" and st["tool"] == "weather"
    assert "COMPOSE_PROFILES=xmcp,data,weather" in (repo / ".env").read_text()
    log = (repo / "docker.log").read_text()
    assert "up -d --build weather" in log
    # Consumed: the same request id never re-applies.
    ups_before = log.count("up -d")
    rec.run_once(repo)
    assert (repo / "docker.log").read_text().count("up -d") == ups_before


def test_unknown_tool_and_bad_action_are_refused_without_compose(repo):
    _request(repo, tool="egress", rid="r-sub")  # substrate has no manifest
    rec.run_once(repo)
    assert _status(repo)["phase"] == "failed" and "not a shipped tool" in _status(repo)["detail"]
    _request(repo, tool="weather", rid="r-act", action="purge")
    rec.run_once(repo)
    assert "unsupported action" in _status(repo)["detail"]
    assert "up -d" not in (repo / "docker.log").read_text()
    assert "COMPOSE_PROFILES=xmcp,data\n" in (repo / ".env").read_text()  # untouched


def test_missing_secrets_refuse_the_deploy(repo):
    _request(repo, rid="r-nosecrets")
    rec.run_once(repo)
    st = _status(repo)
    assert st["phase"] == "failed" and "W_KEY" in st["detail"]
    assert "up -d" not in (repo / "docker.log").read_text()


def test_torn_or_absent_request_is_ignored(repo):
    (rec.control_dir(repo) / "request.json").write_text('{"id": "half')
    rec.run_once(repo)  # no crash, no status
    assert _status(repo) is None or _status(repo) == {}


def test_staging_writes_env_from_template_and_blanks_the_handoff(repo):
    (repo / "tools" / "weather" / "env.example").write_text(
        "# secrets\nW_KEY=\nMCP_AUTH_ENABLED=1\n"
    )
    rec.write_json(
        rec.control_dir(repo) / "staging.json",
        {"id": "s1", "tool": "weather", "values": {"W_KEY": "sk-abc$1\\n"}},
    )
    rec.run_once(repo)
    env = repo / "tools" / "weather" / ".env"
    text = env.read_text()
    # Template preserved, key filled in place, regex-special chars land literally.
    assert "W_KEY=sk-abc$1\\n" in text and "MCP_AUTH_ENABLED=1" in text
    assert oct(env.stat().st_mode & 0o777) == "0o600"
    # Handoff consumed: values no longer at rest in the control dir.
    assert rec.read_json(rec.control_dir(repo) / "staging.json") == {}
    # And inventory (same pass) already reflects readiness.
    inv = rec.read_json(rec.control_dir(repo) / "inventory.json")["tools"]["weather"]
    assert inv["secrets_ready"] is True


def test_staging_merges_into_existing_env(repo):
    (repo / "tools" / "weather" / ".env").write_text("OTHER=keep\nW_KEY=old\n")
    rec.write_json(
        rec.control_dir(repo) / "staging.json",
        {"id": "s2", "tool": "weather", "values": {"W_KEY": "new"}},
    )
    rec.run_once(repo)
    text = (repo / "tools" / "weather" / ".env").read_text()
    assert "OTHER=keep" in text and "W_KEY=new" in text and "old" not in text


def test_staging_refuses_unknown_tool_or_keys_and_discards(repo):
    rec.write_json(
        rec.control_dir(repo) / "staging.json",
        {"id": "s3", "tool": "weather", "values": {"NOT_IN_MANIFEST": "x"}},
    )
    rec.run_once(repo)
    assert rec.read_json(rec.control_dir(repo) / "staging.json") == {}  # discarded, not kept
    assert not (repo / "tools" / "weather" / ".env").exists()
    rec.write_json(
        rec.control_dir(repo) / "staging.json",
        {"id": "s4", "tool": "egress", "values": {"A": "b"}},
    )
    rec.run_once(repo)
    assert rec.read_json(rec.control_dir(repo) / "staging.json") == {}
