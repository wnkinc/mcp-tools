"""Thin-wrapper tests. The engine (taylorwilsdon/google_workspace_mcp) is a
build-time vendor stage, not repo code, so CI covers the wrapper's own logic:
the env posture handed to the engine before its modules import."""

import importlib.util
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

_SPEC = importlib.util.spec_from_file_location(
    "workspace_server", Path(__file__).parent / "server.py"
)
ws = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(ws)


def test_env_defaults_pin_the_engine_posture(monkeypatch):
    for key in (
        "MCP_SINGLE_USER_MODE",
        "WORKSPACE_MCP_TRANSPORT",
        "WORKSPACE_MCP_PORT",
        "WORKSPACE_MCP_CREDENTIALS_DIR",
        "WORKSPACE_MCP_LOG_DIR",
        "WORKSPACE_ATTACHMENT_DIR",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("MCP_PORT", "8066")
    ws._engine_env_defaults()
    import os

    # Single-user + streamable-http: the engine's multi-user OAuth 2.1 MCP auth
    # stays off (serve() owns MCP auth); all writable paths land in the state volume.
    assert os.environ["MCP_SINGLE_USER_MODE"] == "1"
    assert os.environ["WORKSPACE_MCP_TRANSPORT"] == "streamable-http"
    assert os.environ["WORKSPACE_MCP_PORT"] == "8066"
    assert os.environ["WORKSPACE_MCP_CREDENTIALS_DIR"].startswith("/app/state")
    assert os.environ["WORKSPACE_MCP_LOG_DIR"].startswith("/app/state")
    assert os.environ["WORKSPACE_ATTACHMENT_DIR"].startswith("/app/state")


def test_env_defaults_never_override_a_deploy_choice(monkeypatch):
    monkeypatch.setenv("WORKSPACE_MCP_CREDENTIALS_DIR", "/elsewhere/creds")
    monkeypatch.setenv("MCP_SINGLE_USER_MODE", "1")
    ws._engine_env_defaults()
    import os

    assert os.environ["WORKSPACE_MCP_CREDENTIALS_DIR"] == "/elsewhere/creds"


def test_engine_dir_is_env_overridable():
    # Dev/tests point WORKSPACE_ENGINE_DIR at a local checkout; the image default
    # is the Dockerfile's sha256-verified vendor stage.
    assert ws.ENGINE_DIR == "/app/vendor/google_workspace_mcp"
