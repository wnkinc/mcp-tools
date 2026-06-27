"""OpenBB-backed bar fetch — replaces the hand-rolled yfinance download + normalize.

OpenBB's standardized model already returns canonical-named OHLCV, so a fetch yields a
frame with the canonical column *names* (``timestamp, open, high, low, close, volume``)
and ``schema.enforce_canonical`` applies the universal guarantees. The two things OpenBB
does *not* do for us — the canonical interval vocabulary and a real "full history" pull —
live here.
"""
from __future__ import annotations

import pandas as pd

# OpenBB provider name (the data origin). Kept as the store's ``source`` segment.
SOURCE = "yfinance"

# Canonical interval → OpenBB/yfinance interval value. yfinance via OpenBB uses ``1W``/
# ``1M`` for weekly/monthly (NOT ``1wk``/``1mo``); intraday + daily pass through.
_INTERVALS: dict[str, str] = {
    "1m": "1m",
    "5m": "5m",
    "15m": "15m",
    "30m": "30m",
    "1h": "1h",
    "1d": "1d",
    "1wk": "1W",
    "1mo": "1M",
}

# yfinance's no-date default is ~1y, not max. Anchor full-history pulls far enough back
# that the provider clamps to the first available bar (e.g. AAPL → 1980-12-12).
_FULL_HISTORY_START = "1950-01-01"

_CANONICAL_COLS = ("timestamp", "open", "high", "low", "close", "volume")


def obb_interval(interval: str) -> str:
    if interval not in _INTERVALS:
        raise ValueError(
            f"Unsupported interval {interval!r}. Supported: {sorted(_INTERVALS)}"
        )
    return _INTERVALS[interval]


def fetch(symbol: str, interval: str, start: str | None, end: str | None) -> pd.DataFrame:
    """Fetch canonical-named OHLCV bars via OpenBB (yfinance provider).

    With no ``start``/``end`` the start is anchored in the deep past so the provider
    returns full available history.
    """
    from openbb import obb

    iv = obb_interval(interval)
    # start given → use it; only end given → let the provider pick its window up to end;
    # neither → anchor full history.
    start_date = start or (None if end else _FULL_HISTORY_START)
    res = obb.equity.price.historical(
        symbol=symbol,
        interval=iv,
        start_date=start_date,
        end_date=end,
        provider=SOURCE,
    )
    # to_df() indexes by 'date'; lift it to a canonically-named column.
    df = res.to_df().reset_index().rename(columns={"date": "timestamp"})
    keep = [c for c in _CANONICAL_COLS if c in df.columns]
    return df.loc[:, keep]
