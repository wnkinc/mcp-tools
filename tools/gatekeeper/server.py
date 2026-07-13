"""Gatekeeper: the operator's control plane over every tool's mode.

The approval sidecar is the sole authority on tool modes (see
security/approval/gating.py): each approval-enabled server registers its full tool
catalog there, every tool defaults to always_allow, and the operator's choices are
stored per (source, tool). Three tools:

  - deploy_status() -- read-only deployment inventory: what's running (live startup
        beacons), what's stale, and what else the codebase ships (tools/*/deploy.json
        manifests) with the secrets/notes enabling each would involve.

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


def format_deploy_status(manifests: dict, sources: dict, now: float | None = None) -> str:
    """The agent-facing inventory: deployed tools (from live beacons), undeployed
    ones (manifest present, no fresh beacon) with what enabling each would take."""
    now = now or time.time()
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
        lines.append("Stale (stored state, no live server -- forgettable in the manage panel):")
        for src in stale:
            lines.append(f"  - {src}: last registered {_ago(sources[src].get('registered'), now)}")
    undeployed = sorted(p for p in manifests if p not in fresh)
    if undeployed:
        lines.append("Available to deploy (in the codebase, not running):")
        for profile in undeployed:
            m = manifests[profile]
            lines.append(f"  - {profile}: {m['summary']}")
            if m.get("secrets"):
                needs = "; ".join(f"{s['label']} ({s['hint']})" for s in m["secrets"])
                lines.append(f"      secrets needed: {needs}")
            else:
                lines.append("      secrets needed: none beyond the shared Google OAuth identity")
            for note in m.get("notes", []):
                lines.append(f"      note: {note}")
        lines.append(
            "To deploy one today (chat-driven deploy arrives in a later phase), on the host: "
            "fill tools/<name>/.env from its env.example, add https://<subdomain>.<your-domain>"
            "/auth/callback to the shared Google OAuth client, add the profile to "
            "COMPOSE_PROFILES in the root .env, run docker compose (both -f files) up -d "
            "--build <name>, then add the connector in claude.ai."
        )
    return "\n".join(lines)


@mcp.tool
async def deploy_status() -> str:
    """What this deployment serves and what else it could: deployed tools (with
    last-used), stale leftovers, and undeployed tools from the codebase with the
    secrets/notes enabling each would involve. Read-only."""
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(f"{APPROVAL_URL}/sources")
    return format_deploy_status(load_manifests(), resp.json())


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
    # The in-chat permissions panel (manage_tools + its ui:// resource).
    register_manage_widget(mcp)
    # Tools default to always_allow like everything else -- EXCEPT set_gating,
    # which the sidecar pins to needs_approval (see _PINNED in its service.py).
    serve(mcp, port=port, require_approval=True)


if __name__ == "__main__":
    main()
