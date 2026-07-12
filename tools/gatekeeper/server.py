"""Gatekeeper: the operator's control plane over every tool's mode.

The approval sidecar is the sole authority on tool modes (see
security/approval/gating.py): each approval-enabled server registers its full tool
catalog there, every tool defaults to always_allow, and the operator's choices are
stored per (source, tool). Two tools:

  - list_gating(source)          -- read-only: EVERY tool on that server with its
        mode, grouped read-only vs write like Claude's connector UI. Blocked tools
        are listed too -- nothing is invisible to the operator.
  - set_gating(tool, mode, source) -- set a tool's mode:
        always_allow   -- runs with no approval card
        needs_approval -- each call needs a human approval
        blocked        -- disabled: calls refuse outright AND the tool is filtered
                          from Claude's tools/list (invisible once the connector
                          refreshes its cached list; the refusal is immediate)
    set_gating itself is PINNED to needs_approval in the sidecar (a code constant,
    not stored state): changing a safety gate always takes a human approval, and no
    runtime path can lift that.
"""

import os
import sys
from pathlib import Path
from typing import Literal

import httpx
from fastmcp import FastMCP

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from security.serve import serve  # noqa: E402

mcp = FastMCP(name="gatekeeper")

APPROVAL_URL = os.getenv("APPROVAL_URL", "http://approval:8072").rstrip("/")


@mcp.tool
async def list_gating(source: str = "telegram") -> str:
    """Every tool on `source` with its mode (always_allow / needs_approval / blocked),
    grouped read-only vs write. Read-only; blocked tools are shown too."""
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(f"{APPROVAL_URL}/catalog", params={"source": source})
    tools = resp.json().get("tools", {})
    if not tools:
        return (
            f"No catalog registered for `{source}` yet. A server registers its catalog "
            "on its first tools/list -- connect or refresh that connector once."
        )
    groups = {True: [], False: []}
    for name, info in sorted(tools.items()):
        groups[bool(info.get("read_only"))].append(f"  - {name}: {info.get('mode')}")
    sections = []
    if groups[False]:
        sections.append("Write / interactive:\n" + "\n".join(groups[False]))
    if groups[True]:
        sections.append("Read-only:\n" + "\n".join(groups[True]))
    return f"{source} tools ({len(tools)}):\n\n" + "\n\n".join(sections)


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
    applies, refresh the `source` connector so its cards and tool list update."""
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
    # Both tools default to always_allow like everything else -- EXCEPT set_gating,
    # which the sidecar pins to needs_approval (see _PINNED in its service.py).
    serve(mcp, port=port, require_approval=True)


if __name__ == "__main__":
    main()
