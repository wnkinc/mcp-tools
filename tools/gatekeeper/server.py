"""Gatekeeper: the operator's control plane over every tool's mode.

The approval sidecar is the sole authority on tool modes (see
security/approval/gating.py): each approval-enabled server registers its full tool
catalog there, every tool defaults to always_allow, and the operator's choices are
stored per (source, tool). Five tools:

  - deploy_status() -- read-only deployment inventory: what's running (live startup
        beacons), what's stale, and what else the codebase ships (tools/*/deploy.json
        manifests) with the secrets/notes enabling each would involve, secrets-staged
        state, and in-flight deploy progress. The free companion to deploy_tool.
  - deploy_tool(name) -- request deploying an undeployed tool (PINNED needs_approval:
        a human approves every deploy). Only ever writes a request; the HOST
        reconciler (deploy/host/) validates and applies it. Secrets never pass
        through here -- they're staged on the host, this only checks readiness.
  - stage_secrets(name) -- in-chat secrets form (a widget): the user types the
        tool's API keys into the form, values POST browser->sidecar->reconciler,
        never through chat (see security/approval/secrets_widget.py).

  - manage_tools()  -- the in-chat permissions panel (a widget, one section per
        connector; see security/approval/manage_widget.py). The human review-and-save
        surface; nothing changes until they click Save.
  - set_gating(tool, mode, source) -- the conversational path: set one tool's mode:
        always_allow   -- runs with no approval card
        needs_approval -- each call needs a human approval
        blocked        -- disabled: calls refuse outright AND the tool is filtered
                          from Claude's tools/list (invisible once the connector
                          refreshes its cached list; the refusal is immediate)
The gatekeeper's own tools are NOT manageable -- the sidecar refuses every mode
write against the "gatekeeper" source and omits it from the panel. Their behavior
is fixed in code: set_gating is pinned to needs_approval (changing a safety gate
always takes a human approval), and manage_tools is inherently human-in-the-loop
(nothing changes until the user clicks Save).
"""

import json
import os
import sys
import time
from pathlib import Path
from typing import Literal

import httpx
from fastmcp import FastMCP

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from security.approval.manage_widget import register_manage_widget  # noqa: E402
from security.approval.secrets_widget import register_secrets_widget  # noqa: E402
from security.serve import serve  # noqa: E402

mcp = FastMCP(name="gatekeeper")

APPROVAL_URL = os.getenv("APPROVAL_URL", "http://approval:8072").rstrip("/")

# A source whose startup beacon (~30s cadence) arrived within this window is
# currently deployed; older/absent means its container is gone (stale state).
DEPLOYED_WINDOW_SECONDS = 90

# tools/*/deploy.json -- each tool's deploy manifest (what it is, which secrets it
# needs and where to get them, notes like image size). The gatekeeper image bakes
# the whole tools/ tree, so a rebuild picks up new tools automatically.
TOOLS_DIR = Path(__file__).resolve().parents[1]


def load_manifests(tools_dir: Path = TOOLS_DIR) -> dict[str, dict]:
    """Every shipped tool's deploy manifest, keyed by profile name."""
    manifests = {}
    for path in sorted(tools_dir.glob("*/deploy.json")):
        m = json.loads(path.read_text())
        manifests[m["profile"]] = m
    return manifests


def _ago(epoch: float | None, now: float) -> str:
    if not epoch:
        return "never"
    s = max(0.0, now - epoch)
    if s < 5400:
        return f"{round(s / 60)}m ago"
    if s < 129600:
        return f"{round(s / 3600)}h ago"
    return f"{round(s / 86400)}d ago"


def format_deploy_status(
    manifests: dict, sources: dict, deploy: dict | None = None, now: float | None = None
) -> str:
    """The agent-facing inventory: deployed tools (from live beacons), undeployed
    ones (manifest present, no fresh beacon) with what enabling each would take,
    plus the reconciler's view (secrets staged? an operation in flight?)."""
    now = now or time.time()
    deploy = deploy or {}
    inventory = deploy.get("inventory") or {}
    fresh = {
        s
        for s, st in sources.items()
        if st.get("registered") and now - st["registered"] < DEPLOYED_WINDOW_SECONDS
    }
    lines = ["Deployed (live startup beacon):"]
    for src in sorted(fresh):
        if src == "gatekeeper":
            continue
        st = sources[src]
        lines.append(f"  - {src}: {st['tools']} tools, last used {_ago(st.get('seen'), now)}")
    stale = sorted(s for s in sources if s not in fresh and s != "gatekeeper")
    if stale:
        lines.append("Stale (stored state, no live server):")
        for src in stale:
            lines.append(f"  - {src}: last registered {_ago(sources[src].get('registered'), now)}")
    undeployed = sorted(p for p in manifests if p not in fresh)
    if undeployed:
        lines.append("Available to deploy (in the codebase, not running):")
        for profile in undeployed:
            m = manifests[profile]
            lines.append(f"  - {profile}: {m['summary']}")
            inv = inventory.get(profile)
            if m.get("secrets"):
                needs = "; ".join(f"{s['label']} ({s['hint']})" for s in m["secrets"])
                lines.append(f"      secrets needed: {needs}")
                if inv is not None:
                    staged = (
                        "staged ✓"
                        if inv.get("secrets_ready")
                        else f"missing: {', '.join(inv.get('missing_secrets', []))}"
                    )
                    lines.append(f"      secrets staged: {staged}")
            else:
                lines.append("      secrets needed: none beyond the shared Google OAuth identity")
            for note in m.get("notes", []):
                lines.append(f"      note: {note}")
    # The reconciler's own state: whether chat-driven deploys can execute at all,
    # and what the last / current operation did.
    reconciler = deploy.get("reconciler", "absent")
    if reconciler == "live":
        if deploy.get("in_flight"):
            op = deploy.get("request") or deploy.get("status") or {}
            phase = (deploy.get("status") or {}).get("phase", "queued")
            lines.append(
                f"Deploy in flight: {op.get('tool')} ({phase}) -- re-check deploy_status "
                "for progress; large images take minutes."
            )
        else:
            last = deploy.get("status") or {}
            if last.get("phase"):
                lines.append(
                    f"Last deploy operation: {last.get('tool')} -> {last['phase']}"
                    + (f" ({last.get('detail')})" if last.get("detail") else "")
                )
            if undeployed:
                lines.append(
                    "To deploy: stage the tool's secrets -- stage_secrets(<name>) opens an "
                    "in-chat form (values go directly to the server, never through chat), "
                    "or fill tools/<name>/.env on the host -- make sure "
                    "https://<subdomain>.<your-domain>/auth/callback is on the shared Google "
                    "OAuth client, then call deploy_tool(<name>) -- it needs the user's "
                    "approval, applies via the host reconciler, and finishes with adding "
                    "the connector in claude.ai."
                )
    elif undeployed:
        lines.append(
            "The deploy reconciler is not running on the host (deploy/host/README.md), so "
            "deploy_tool can't execute. Manual path: fill tools/<name>/.env, add "
            "https://<subdomain>.<your-domain>/auth/callback to the shared Google OAuth "
            "client, add the profile to COMPOSE_PROFILES in the root .env, run docker "
            "compose (both -f files) up -d --build <name>, then add the connector in "
            "claude.ai."
        )
    return "\n".join(lines)


@mcp.tool
async def deploy_status() -> str:
    """What this deployment serves and what else it could: deployed tools (with
    last-used), stale leftovers, and available not-yet-deployed tools from the
    codebase with the secrets/notes enabling each would involve -- plus whether
    their secrets are staged and how any in-flight deploy is progressing.
    Read-only; the free companion to deploy_tool."""
    async with httpx.AsyncClient(timeout=10) as client:
        src_resp = await client.get(f"{APPROVAL_URL}/sources")
        dep_resp = await client.get(f"{APPROVAL_URL}/deploy/state")
    return format_deploy_status(load_manifests(), src_resp.json(), dep_resp.json())


@mcp.tool
async def deploy_tool(name: str) -> str:
    """Deploy an available, not-yet-deployed tool from the codebase (one at a
    time). Requires the
    user's approval, then the host reconciler applies it: profile added, image
    built, container up -- progress and results via deploy_status. Prerequisite:
    the tool's secrets staged on the host (deploy_status shows staged/missing).
    This call only REQUESTS the deploy; it never handles secret values."""
    manifests = load_manifests()
    if name not in manifests:
        return (
            f"⚠️ `{name}` is not a shipped tool. Available manifests: "
            f"{', '.join(sorted(manifests))}."
        )
    async with httpx.AsyncClient(timeout=10) as client:
        sources = (await client.get(f"{APPROVAL_URL}/sources")).json()
        deploy = (await client.get(f"{APPROVAL_URL}/deploy/state")).json()
    reg = (sources.get(name) or {}).get("registered")
    if reg and time.time() - reg < DEPLOYED_WINDOW_SECONDS:
        return f"`{name}` is already deployed (live startup beacon). Nothing to do."
    if deploy.get("reconciler") != "live":
        return (
            "⚠️ The deploy reconciler is not running on the host, so this can't "
            "execute. Install it (deploy/host/README.md) or follow the manual steps "
            "in deploy_status. Nothing was changed."
        )
    if deploy.get("in_flight"):
        op = deploy.get("request") or {}
        return (
            f"⚠️ A deploy is already in flight ({op.get('tool')}). One at a time -- "
            "check deploy_status for progress. Nothing was changed."
        )
    inv = (deploy.get("inventory") or {}).get(name) or {}
    if manifests[name].get("secrets") and not inv.get("secrets_ready"):
        missing = ", ".join(
            inv.get("missing_secrets") or [s["key"] for s in manifests[name]["secrets"]]
        )
        return (
            f"⚠️ `{name}`'s secrets aren't staged yet (missing: {missing}). Open the "
            f"in-chat form with stage_secrets('{name}') -- or fill tools/{name}/.env "
            "on the host directly -- then call this again. Nothing was changed."
        )
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(f"{APPROVAL_URL}/deploy/request", json={"tool": name})
    data = resp.json()
    if not data.get("ok"):
        return (
            f"⚠️ Deploy request refused: {data.get('error', 'unknown error')}. Nothing was changed."
        )
    notes = "; ".join(manifests[name].get("notes", [])[:2])
    return (
        f"🚀 Deploy of `{name}` requested (id {data['id']}) -- the host reconciler is "
        f"applying it now. Track progress with deploy_status."
        + (f" Notes: {notes}" if notes else "")
        + " When it's up, add the connector in claude.ai "
        f"(https://{manifests[name]['subdomain']}.<your-domain>/mcp)."
    )


@mcp.tool
async def set_gating(
    tool: str,
    mode: Literal["always_allow", "needs_approval", "blocked"],
    source: str = "telegram",
) -> str:
    """Set a tool's mode on `source`: 'always_allow' runs with no approval card,
    'needs_approval' needs a human approval per call, 'blocked' disables the tool
    outright (calls refuse immediately, and it disappears from Claude's tool list
    once the connector refreshes).

    Changing a gate is itself gated, so this call needs your approval first. After it
    applies, refresh the `source` connector so its cards and tool list update.
    The gatekeeper's own tools can't be targeted; their behavior is fixed in code."""
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"{APPROVAL_URL}/gating",
            json={"source": source, "tool": tool, "mode": mode},
        )
    data = resp.json()
    if not data.get("ok"):
        return (
            f"⚠️ Mode change refused for `{tool}` on {source}: {data.get('error', 'unknown error')}."
        )
    return (
        f"✅ `{tool}` on {source} is now {mode}. Enforcement takes effect within a "
        f"few seconds; refresh the {source} connector to update its in-chat cards "
        "and visible tool list."
    )


def main() -> None:
    port = int(os.getenv("MCP_PORT", "8065"))
    # The in-chat permissions panel (manage_tools + its ui:// resource) and the
    # secrets-staging form (stage_secrets) -- both human-input widgets.
    register_manage_widget(mcp)
    register_secrets_widget(mcp, load_manifests)
    # Tools default to always_allow like everything else -- EXCEPT set_gating,
    # which the sidecar pins to needs_approval (see _PINNED in its service.py).
    serve(mcp, port=port, require_approval=True)


if __name__ == "__main__":
    main()
