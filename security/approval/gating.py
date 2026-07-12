"""Runtime tool-mode decision: what mode is a (source, tool) in, right now?

Every tool is in one of three MODES, and the approval SIDECAR is the sole
authority on them -- there is no code-side allowlist:

  - "always_allow":   runs with no approval. Also the default for any tool the
                      sidecar has no stored choice for (ship-open by design; the
                      operator curates from there via the gatekeeper).
  - "needs_approval": each call needs an out-of-band human approval.
  - "blocked":        disabled by the operator -- calls refuse outright AND the
                      tool is filtered from Claude's tools/list (invisible once
                      the connector refreshes its cached list; the refusal is
                      the actual gate).

Servers fetch the modes from the sidecar with a short TTL cache, so a change takes
effect within a few seconds on the gate (the in-chat card and the visible tool list
follow on the next tools/list, which the connector caches until refreshed).

Failure semantics: a fetch blip keeps the last-known modes. If NO fetch has ever
succeeded (fresh process + sidecar down), fetch_modes returns None and mode_for
treats everything as needs_approval -- otherwise an outage would silently unblock
every blocked tool. Approval itself needs the sidecar too, so that state fails
closed end-to-end.
"""

from __future__ import annotations

import contextlib
import time

import httpx

MODES = ("always_allow", "needs_approval", "blocked")
DEFAULT_MODE = "always_allow"

_TTL = 15.0
_cache: dict[str, tuple[float, dict[str, str]]] = {}  # source -> (fetched_at, modes)


def _as_mode(value) -> str:  # type: ignore[no-untyped-def]
    """Normalize a wire value; anything unrecognized fails SAFE (needs_approval)."""
    return value if value in MODES else "needs_approval"


async def fetch_modes(
    source: str, approval_url: str, timeout: float = 5.0
) -> dict[str, str] | None:
    """Tool modes for `source` from the sidecar, cached for _TTL seconds. On error
    returns the last-known value; None only if no fetch has EVER succeeded."""
    now = time.monotonic()
    hit = _cache.get(source)
    if hit and now - hit[0] < _TTL:
        return hit[1]
    with contextlib.suppress(Exception):
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(f"{approval_url.rstrip('/')}/gating", params={"source": source})
        data = {k: _as_mode(v) for k, v in (resp.json().get("modes") or {}).items()}
        _cache[source] = (now, data)
        return data
    if hit:  # blip: keep (and re-stamp) last-known so a down sidecar isn't re-polled per call
        _cache[source] = (now, hit[1])
        return hit[1]
    return None  # nothing ever fetched -> callers fail closed


def mode_for(tool: str, modes: dict[str, str] | None) -> str:
    """The mode `tool` is in. No stored choice -> always_allow; modes=None (the
    sidecar has never answered this process) -> needs_approval, failing closed."""
    if modes is None:
        return "needs_approval"
    return modes.get(tool, DEFAULT_MODE)
