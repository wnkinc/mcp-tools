"""The deterministic ingest pipeline — fetch → normalize → validate → store.

This is the single logical act behind the agent's one ``data-ingest`` call. The
agent never sees the individual stages; it asks for data, and a covered request
returns the cached frame while an uncovered one fetches and overwrites.
"""
from __future__ import annotations

import pandas as pd

import store
from schema import KIND_BARS, enforce_canonical
from sources import openbb_source

# source name → fetcher returning a canonical-NAMED frame (OpenBB already normalizes,
# so there is no separate raw→canonical step anymore; enforce_canonical does the rest).
_SOURCES = {
    openbb_source.SOURCE: openbb_source.fetch,
}

DEFAULT_INTERVAL = "1d"
DEFAULT_SOURCE = openbb_source.SOURCE


def _summary(symbol, interval, source, df, path, *, cached) -> dict:
    ts = pd.to_datetime(df["timestamp"], utc=True) if not df.empty else None
    return {
        "kind": KIND_BARS,
        "source": source,
        "symbol": symbol,
        "interval": interval,
        "rows": int(len(df)),
        "start": ts.min().isoformat() if ts is not None else None,
        "end": ts.max().isoformat() if ts is not None else None,
        "path": str(path),
        "cached": cached,
    }


def ingest(
    symbol: str,
    interval: str = DEFAULT_INTERVAL,
    start: str | None = None,
    end: str | None = None,
    source: str = DEFAULT_SOURCE,
    refresh: bool = False,
) -> dict:
    """Ingest bars for one symbol into the canonical store; return a summary dict.

    ``refresh=True`` bypasses the cache and refetches the whole range.
    Raises ``ValueError`` for an unknown source or interval (the latter inside the
    source's own validation).
    """
    symbol = (symbol or "").strip().upper()
    if not symbol:
        raise ValueError("symbol is required")
    interval = (interval or DEFAULT_INTERVAL).strip()
    if source not in _SOURCES:
        raise ValueError(f"Unknown source {source!r}. Supported: {sorted(_SOURCES)}")
    fetch = _SOURCES[source]

    if not refresh and store.covers(KIND_BARS, source, symbol, interval, start, end):
        existing = store.read(KIND_BARS, source, symbol, interval)
        path = store.path_for(KIND_BARS, source, symbol, interval)
        return _summary(symbol, interval, source, existing, path, cached=True)

    df = enforce_canonical(fetch(symbol, interval, start, end))
    if df.empty:
        raise ValueError(
            f"No data returned for symbol={symbol!r} interval={interval!r} "
            f"start={start!r} end={end!r} from {source}."
        )
    path = store.write(KIND_BARS, source, symbol, interval, df, req_start=start, req_end=end)
    return _summary(symbol, interval, source, df, path, cached=False)
