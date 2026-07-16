"""Gatekeeper: the operator's control plane over every tool's mode.

The approval sidecar is the sole authority on tool modes (see
security/approval/gating.py): each approval-enabled server registers its full tool
catalog there, every tool defaults to always_allow, and the operator's choices are
stored per (source, tool). Two tools:

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

import os
import sys
from pathlib import Path
from typing import Literal

import httpx
from fastmcp import FastMCP

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from security.approval.manage_widget import register_manage_widget  # noqa: E402
from security.serve import serve  # noqa: E402

mcp = FastMCP(name="gatekeeper")

APPROVAL_URL = os.getenv("APPROVAL_URL", "http://approval:8072").rstrip("/")


@mcp.tool
async def set_gating(
    tool: str,
    mode: Literal["always_allow", "needs_approval", "blocked"],
    source: str,
) -> str:
    """Set a tool's mode on `source` (the connector the tool belongs to, e.g.
    'telegram' -- the manage_tools panel lists them): 'always_allow' runs with no
    approval card, 'needs_approval' needs a human approval per call, 'blocked'
    disables the tool outright (calls refuse immediately, and it disappears from
    Claude's tool list once the connector refreshes).

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
