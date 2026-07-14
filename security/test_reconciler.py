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
        'if [[ "$*" == *"--format json"* ]]; then\n'
        f'  if [ -f "{tmp_path}/unhealthy.flag" ]; then\n'
        '    echo \'{"State":"restarting","Health":""}\'\n'
        "  else\n"
        '    echo \'{"State":"running","Health":"healthy"}\'\n'
        "  fi\n"
        "  exit 0\n"
        "fi\n"
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


def test_staging_seeds_the_shared_mcp_auth_identity(repo):
    # The staging form only carries per-tool secrets; the shared Claude-auth trio
    # (client id/secret + email allowlist) must arrive from a sibling tool's .env
    # or the fresh tool fails closed at startup.
    (repo / "tools" / "other").mkdir()
    (repo / "tools" / "other" / ".env").write_text(
        "GOOGLE_CLIENT_ID=shared-id\nGOOGLE_CLIENT_SECRET=shared-secret\n"
        "MCP_ALLOWED_GOOGLE_EMAILS=me@example.com\n"
    )
    (repo / "tools" / "weather" / "env.example").write_text(
        "W_KEY=\nMCP_AUTH_ENABLED=1\nGOOGLE_CLIENT_ID=\nGOOGLE_CLIENT_SECRET=\n"
        "MCP_ALLOWED_GOOGLE_EMAILS=\n"
    )
    rec.write_json(
        rec.control_dir(repo) / "staging.json",
        {"id": "seed1", "tool": "weather", "values": {"W_KEY": "k"}},
    )
    rec.run_once(repo)
    text = (repo / "tools" / "weather" / ".env").read_text()
    assert "GOOGLE_CLIENT_ID=shared-id" in text
    assert "GOOGLE_CLIENT_SECRET=shared-secret" in text
    assert "MCP_ALLOWED_GOOGLE_EMAILS=me@example.com" in text
    assert "W_KEY=k" in text


def test_deploy_reports_failed_when_the_container_never_gets_healthy(repo, monkeypatch):
    # `up -d` exiting 0 only creates the container; a crash-looping tool must
    # surface as failed with its logs, never as done.
    monkeypatch.setattr(rec, "HEALTHY_TIMEOUT", 0.2)
    (repo / "unhealthy.flag").touch()
    (repo / "tools" / "weather" / ".env").write_text("W_KEY=abc\n")
    _request(repo, rid="r-unhealthy")
    rec.run_once(repo)
    st = _status(repo)
    assert st["phase"] == "failed" and "did not become healthy" in st["detail"]


def test_deploy_done_requires_running_and_healthy(repo):
    (repo / "tools" / "weather" / ".env").write_text("W_KEY=abc\n")
    _request(repo, rid="r-healthy")
    rec.run_once(repo)
    st = _status(repo)
    assert st["phase"] == "done" and "healthy" in st["detail"]


def _defaultable_manifest(repo):
    # A workspace-shaped manifest: every secret defaults from the shared identity.
    (repo / "tools" / "weather" / "deploy.json").write_text(
        json.dumps(
            {
                "title": "Weather",
                "profile": "weather",
                "subdomain": "weather",
                "port": 8070,
                "summary": "weather",
                "secrets": [
                    {
                        "key": "W_OAUTH_ID",
                        "label": "id",
                        "hint": "h",
                        "default_from": "GOOGLE_CLIENT_ID",
                    },
                    {
                        "key": "W_EMAIL",
                        "label": "email",
                        "hint": "h",
                        "default_from": "MCP_ALLOWED_GOOGLE_EMAILS",
                    },
                ],
                "notes": [],
                "depends": [],
            }
        )
    )
    (repo / "tools" / "other").mkdir()
    (repo / "tools" / "other" / ".env").write_text(
        "GOOGLE_CLIENT_ID=shared-id\nGOOGLE_CLIENT_SECRET=shared-secret\n"
        "MCP_ALLOWED_GOOGLE_EMAILS=me@example.com,alt@example.com\n"
    )


def test_defaultable_secrets_count_as_ready_and_deploy_with_no_staging(repo):
    _defaultable_manifest(repo)
    rec.run_once(repo)
    inv = rec.read_json(rec.control_dir(repo) / "inventory.json")["tools"]["weather"]
    # Nothing staged, but everything derivable -> ready; the user is never asked.
    assert inv == {"deployed": False, "secrets_ready": True, "missing_secrets": []}
    _request(repo, rid="r-defaults")
    rec.run_once(repo)
    assert _status(repo)["phase"] == "done"
    text = (repo / "tools" / "weather" / ".env").read_text()
    assert "W_OAUTH_ID=shared-id" in text
    assert "W_EMAIL=me@example.com" in text  # first allowlist entry, not the list
    assert "GOOGLE_CLIENT_ID=shared-id" in text  # shared identity seeded too


def test_user_staged_value_beats_the_default(repo):
    _defaultable_manifest(repo)
    rec.write_json(
        rec.control_dir(repo) / "staging.json",
        {"id": "s-override", "tool": "weather", "values": {"W_OAUTH_ID": "my-own-client"}},
    )
    rec.run_once(repo)
    text = (repo / "tools" / "weather" / ".env").read_text()
    assert "W_OAUTH_ID=my-own-client" in text and "W_EMAIL=me@example.com" in text
