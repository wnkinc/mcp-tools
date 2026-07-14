"""Every shipped tool's deploy manifest parses and carries what the deploy flow
needs -- the gatekeeper's deploy_status renders these, and the chat-driven deploy
of a later phase consumes them, so a malformed manifest should fail CI, not chat."""

import json
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1] / "tools"
MANIFESTS = sorted(TOOLS.glob("*/deploy.json"))


def test_every_profile_tool_ships_a_manifest():
    # The compose profiles (= deployable tools) and the manifest set must agree.
    # (Non-vacuity of the manifest glob is test_stack.py's job.)
    compose = (TOOLS.parent / "docker-compose.yml").read_text()
    profiled = {m.parent.name for m in MANIFESTS}
    for tool in profiled:
        assert f'profiles: ["{tool}"]' in compose, f"{tool} has a manifest but no profile"


@pytest.mark.parametrize("path", MANIFESTS, ids=lambda p: p.parent.name)
def test_manifest_shape(path):
    m = json.loads(path.read_text())
    assert m["profile"] == path.parent.name  # keyed by directory = by compose profile
    for field in ("title", "subdomain", "port", "summary", "secrets", "notes", "depends"):
        assert field in m, f"{path}: missing {field}"
    assert isinstance(m["port"], int)
    for secret in m["secrets"]:
        assert set(secret) >= {"key", "label", "hint"}, f"{path}: bad secret entry {secret}"
        if "default_from" in secret:
            assert isinstance(secret["default_from"], str) and secret["default_from"]
    for step in m.get("prerequisites", []):
        assert isinstance(step, str) and step, f"{path}: bad prerequisite {step!r}"
    for dep in m["depends"]:
        assert (TOOLS / dep / "deploy.json").exists(), f"{path}: unknown dependency {dep}"
