"""Stack-consistency validator: the executable version of "wire a tool everywhere".

Adding a tool touches several files that must agree (compose service, guardrail
profiles, squid listener + allowlist, tunnel route, ports). Each rule here encodes
a mistake class that actually happened -- or one whose symptom (502s, OAuth 401s)
is painful enough to pre-empt -- so a gap fails CI with a message naming the
missing piece instead of surfacing in production. Tools are enumerated from their
deploy.json manifests (each tool's identity record); substrate services
(egress, guardrail, approval, gatekeeper, cloudflared) have no manifest and are
not covered.
"""

import json
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
BASE = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
OVERLAY = yaml.safe_load((ROOT / "docker-compose.tunnel.yml").read_text())
SQUID = (ROOT / "security" / "egress-proxy" / "squid.compose.conf").read_text()

MANIFESTS = {
    m["profile"]: m
    for m in (json.loads(p.read_text()) for p in sorted((ROOT / "tools").glob("*/deploy.json")))
}
TOOLS = sorted(MANIFESTS)

# squid.compose.conf, cross-referenced: listener port -> name, myportname acls,
# dstdomain acls -> allowlist file, and the allow policy pairs.
LISTENERS = {int(p): n for p, n in re.findall(r"^http_port +(\d+) +name=(\S+)", SQUID, re.M)}
PORT_ACLS = dict(re.findall(r"^acl +(\S+) +myportname +(\S+)", SQUID, re.M))
DOM_ACLS = dict(re.findall(r'^acl +(\S+) +dstdomain +"/etc/squid/allowlist/([^"]+)"', SQUID, re.M))
ALLOWS = set(re.findall(r"^http_access allow +(\S+) +CONNECT +(\S+)", SQUID, re.M))

# Tunnel routes: service name -> (subdomain, port). The hostname value embeds
# ${MCP_DOMAIN...}; only the subdomain before the first dot matters here.
ROUTES = {
    svc: (sub, int(port))
    for sub, svc, port in re.findall(
        r"hostname: *([a-z0-9-]+)\.[^\n]*\n *(?:path:[^\n]*\n *)?service: *http://([a-z0-9-]+):(\d+)",
        OVERLAY["configs"]["cloudflared-config"]["content"],
    )
}


def env_of(name: str, compose: dict) -> dict:
    return ((compose.get("services") or {}).get(name) or {}).get("environment") or {}


def merged_env(name: str) -> dict:
    """The public posture: base env with the tunnel overlay's stamped on top."""
    return {**env_of(name, BASE), **env_of(name, OVERLAY)}


def test_manifests_found():
    # A glob/layout change must not green-wash every rule below into vacuity.
    assert len(TOOLS) >= 5, f"expected the shipped tools' manifests, found only {TOOLS}"


# Identities the substrate already claims: subdomains routed by the overlay and
# MCP ports baked into always-on services. A new tool may not collide with them.
_RESERVED_SUBDOMAINS = {"approval", "gatekeeper"}
_RESERVED_MCP_PORTS = {8065, 8071, 8072}  # gatekeeper, guardrail, approval


def test_no_identity_collisions():
    subs = [m["subdomain"] for m in MANIFESTS.values()]
    dupes = {s for s in subs if subs.count(s) > 1} | (set(subs) & _RESERVED_SUBDOMAINS)
    assert not dupes, f"subdomain collision(s): {sorted(dupes)}"
    ports = [m["port"] for m in MANIFESTS.values()]
    port_dupes = {p for p in ports if ports.count(p) > 1} | (set(ports) & _RESERVED_MCP_PORTS)
    assert not port_dupes, f"MCP port collision(s): {sorted(port_dupes)}"
    eports = re.findall(r"^http_port +(\d+)", SQUID, re.M)
    assert len(eports) == len(set(eports)), f"duplicate egress listener ports: {sorted(eports)}"


@pytest.mark.parametrize("name", TOOLS)
def test_compose_service_shape(name):
    svc = BASE["services"].get(name)
    assert svc is not None, (
        f"{name}: tools/{name}/deploy.json exists but docker-compose.yml has no service"
    )
    assert svc.get("profiles") == [name], (
        f"{name}: profiles must be ['{name}'] (opt-in by COMPOSE_PROFILES)"
    )
    env_file = svc.get("env_file") or []
    assert any(
        e.get("path") == f"tools/{name}/.env" and e.get("required") is False for e in env_file
    ), f"{name}: env_file must load tools/{name}/.env with required: false"
    assert any(v.startswith(f"{name}-state:") for v in svc.get("volumes") or []), (
        f"{name}: missing the {name}-state volume mount"
    )
    assert f"{name}-state" in (BASE.get("volumes") or {}), (
        f"{name}: {name}-state not declared under volumes:"
    )


@pytest.mark.parametrize("name", TOOLS)
def test_egress_chain(name):
    env = env_of(name, BASE)
    proxy = env.get("HTTPS_PROXY", "")
    assert env.get("HTTP_PROXY") == proxy, f"{name}: HTTP_PROXY and HTTPS_PROXY must match"
    m = re.fullmatch(r"http://egress:(\d+)", proxy)
    assert m, f"{name}: HTTPS_PROXY must be http://egress:<port>, got {proxy!r}"
    port = int(m.group(1))
    listener = LISTENERS.get(port)
    assert listener, f"{name}: no `http_port {port} name=...` listener in squid.compose.conf"
    port_acl = next((a for a, p in PORT_ACLS.items() if p == listener), None)
    assert port_acl, f"{name}: no myportname acl for listener {listener!r} in squid.compose.conf"
    dom_acl = next((d for p, d in ALLOWS if p == port_acl and d in DOM_ACLS), None)
    assert dom_acl, f"{name}: no `http_access allow {port_acl} CONNECT <dom_acl>` policy line"
    allowlist = ROOT / "security" / "egress-proxy" / "allowlist" / DOM_ACLS[dom_acl]
    assert allowlist.exists(), f"{name}: {allowlist} referenced by squid but missing from the repo"


@pytest.mark.parametrize("name", TOOLS)
def test_untrusted_kit_is_all_or_nothing(name):
    guardrail_profiles = BASE["services"]["guardrail"].get("profiles") or []
    env = merged_env(name)
    if env.get("MCP_UNTRUSTED_OUTPUT") == "1":
        base = env_of(name, BASE)
        assert base.get("GUARDRAIL_URL"), f"{name}: untrusted but no GUARDRAIL_URL"
        assert "GUARDRAIL_ENABLED" in base, f"{name}: untrusted but no GUARDRAIL_ENABLED"
        assert "guardrail" in base.get("NO_PROXY", "").split(","), (
            f"{name}: untrusted but guardrail missing from NO_PROXY (scans would hit the egress wall)"
        )
        assert name in guardrail_profiles, (
            f"{name}: untrusted but not in the guardrail service's profiles -- "
            "the screener would not start with it"
        )
    else:
        assert name not in guardrail_profiles, (
            f"{name}: trusted, but still listed in the guardrail service's profiles (stale entry)"
        )


@pytest.mark.parametrize("name", TOOLS)
def test_approval_sidecar_wiring(name):
    env = env_of(name, BASE)
    assert env.get("APPROVAL_URL"), (
        f"{name}: no APPROVAL_URL -- the tool would never register in the manage panel"
    )
    assert "approval" in env.get("NO_PROXY", "").split(","), (
        f"{name}: approval missing from NO_PROXY -- registration would hit the egress wall"
    )


def test_env_example_available_list_matches_manifests():
    """env.example's 'Available:' line is the one hand-maintained tool list kept
    on purpose (deployers edit that file with no README at hand) -- so it must
    equal the manifest set exactly."""
    m = re.search(r"Available: ([a-z0-9, -]+)\.", (ROOT / "env.example").read_text())
    assert m, "env.example lost its 'Available: <tools>.' line"
    listed = {t.strip() for t in m.group(1).split(",")}
    assert listed == set(TOOLS), (
        f"env.example 'Available:' list {sorted(listed)} != shipped tools {TOOLS}"
    )


def test_ci_and_dependabot_cover_every_tool():
    """A tool outside the pytest matrix ships untested; one outside dependabot's
    pip directories gets no security-update PRs for its lock. Both gaps happened
    (workspace/gatekeeper were missing from dependabot) -- now they fail CI."""
    ci = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())
    matrix = {e["tests"] for e in ci["jobs"]["pytest"]["strategy"]["matrix"]["include"]}
    bot = yaml.safe_load((ROOT / ".github" / "dependabot.yml").read_text())
    pip_dirs = {
        d
        for u in bot["updates"]
        if u["package-ecosystem"] == "pip"
        for d in u.get("directories", [])
    }
    for name in TOOLS:
        assert f"tools/{name}" in matrix, f"{name}: no pytest matrix entry in ci.yml"
        assert f"/tools/{name}" in pip_dirs, (
            f"{name}: missing from dependabot.yml pip directories -- its lock gets no "
            "security-update PRs"
        )


@pytest.mark.parametrize("name", TOOLS)
def test_one_port_one_subdomain(name):
    manifest = MANIFESTS[name]
    dockerfile = (ROOT / "tools" / name / "Dockerfile").read_text()
    m = re.search(r"MCP_PORT=(\d+)", dockerfile)
    assert m, f"{name}: Dockerfile bakes no MCP_PORT"
    assert int(m.group(1)) == manifest["port"], (
        f"{name}: Dockerfile MCP_PORT {m.group(1)} != deploy.json port {manifest['port']}"
    )
    assert name in ROUTES, f"{name}: no tunnel route in docker-compose.tunnel.yml"
    sub, port = ROUTES[name]
    assert port == manifest["port"], (
        f"{name}: tunnel route port {port} != deploy.json port {manifest['port']}"
    )
    assert sub == manifest["subdomain"], (
        f"{name}: route subdomain {sub!r} != deploy.json {manifest['subdomain']!r}"
    )
    over = env_of(name, OVERLAY)
    assert over.get("MCP_AUTH_ENABLED") == "1", f"{name}: overlay must flip MCP_AUTH_ENABLED on"
    assert over.get("MCP_PUBLIC_URL", "").startswith(f"https://{sub}."), (
        f"{name}: overlay MCP_PUBLIC_URL must be https://{sub}.<MCP_DOMAIN>"
    )
