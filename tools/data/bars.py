"""Historical OHLCV bars via OpenBB (yfinance provider), persisted to a parquet lake.

Bars are *kept*: each ingest fetches the requested window from OpenBB and merges it into
a per-symbol parquet file, so the file accumulates history across calls. OpenBB's
standardized frame is persisted as-is — its own schema,
its own ``date`` index — with no canonical re-clean; the only bookkeeping is de-duplicating
on the timestamp index so overlapping re-fetches don't double a bar.

Layout (the path is the metadata):

    <DATA_ROOT>/bars/<source>/<symbol>/<interval>.parquet
    e.g.  var/data/bars/yfinance/AAPL/1d.parquet

Intervals are OpenBB's own vocabulary (1m, 2m, 5m, 15m, 30m, 60m, 90m, 1h, 1d, 5d,
1W, 1M, 1Q) — passed straight through, no translation table. With no start/end the
provider's default window applies (yfinance: ~1y); pass dates for a wider pull.
"""
from __future__ import annotations

import os
from pathlib import Path

import pandas as pd

DEFAULT_PROVIDER = "yfinance"
DEFAULT_INTERVAL = "1d"

# Tool dir = this file's parent; the repo-local default lake lives under it.
_TOOL_ROOT = Path(__file__).resolve().parent


def _obb():
    from openbb import obb

    return obb


def data_root() -> Path:
    """Parquet lake root; ``DATA_ROOT`` overrides the tool-local default."""
    return Path(os.environ.get("DATA_ROOT") or (_TOOL_ROOT / "var" / "data"))


def _safe(symbol: str) -> str:
    """Filesystem-safe symbol (only '/' is genuinely problematic on Linux)."""
    return symbol.replace("/", "_")


def path_for(source: str, symbol: str, interval: str) -> Path:
    return data_root() / "bars" / source / _safe(symbol) / f"{interval}.parquet"


def _fetch(
    symbol: str, interval: str, start: str | None, end: str | None, provider: str
) -> pd.DataFrame:
    """OpenBB's standardized OHLCV frame (date-indexed), persisted as-is."""
    res = _obb().equity.price.historical(
        symbol=symbol,
        interval=interval,
        start_date=start,
        end_date=end,
        provider=provider,
    )
    return res.to_df()


def _merge(existing: pd.DataFrame, fetched: pd.DataFrame) -> pd.DataFrame:
    """Append ``fetched`` onto ``existing``, dropping duplicate timestamps (fetched wins).

    ``fetched`` is concatenated last so a re-downloaded bar overwrites the stored one
    (corrections, late volume) rather than the other way around.
    """
    combined = pd.concat([existing, fetched])
    combined = combined[~combined.index.duplicated(keep="last")]
    return combined.sort_index()


def _summary(symbol, interval, source, df, path, *, fetched: int, added: int) -> dict:
    idx = df.index
    return {
        "source": source,
        "symbol": symbol,
        "interval": interval,
        "rows": int(len(df)),       # total bars now stored
        "fetched": int(fetched),    # bars OpenBB returned this call
        "added": int(added),        # net-new rows after dedupe/merge
        "start": idx.min().isoformat() if len(df) else None,
        "end": idx.max().isoformat() if len(df) else None,
        "path": str(path),
    }


def ingest(
    symbol: str,
    interval: str = DEFAULT_INTERVAL,
    start: str | None = None,
    end: str | None = None,
    source: str = DEFAULT_PROVIDER,
    refresh: bool = False,
) -> dict:
    """Fetch the requested window and merge it into the symbol's parquet file.

    The fetched bars are appended to whatever is already stored and de-duplicated on
    the timestamp index, so the file accumulates history across calls (request 2024
    today, 2023 tomorrow, and the file holds both). ``refresh=True`` ignores the stored
    file and replaces it with just this fetch. Returns a summary dict.
    """
    symbol = (symbol or "").strip().upper()
    if not symbol:
        raise ValueError("symbol is required")
    interval = (interval or DEFAULT_INTERVAL).strip()

    fetched = _fetch(symbol, interval, start, end, source)
    if fetched.empty:
        raise ValueError(
            f"No data returned for symbol={symbol!r} interval={interval!r} "
            f"start={start!r} end={end!r} from {source}."
        )

    path = path_for(source, symbol, interval)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not refresh and path.exists():
        existing = pd.read_parquet(path)
        prev = len(existing)
        df = _merge(existing, fetched)
    else:
        prev = 0
        df = fetched.sort_index()
    df.to_parquet(path)

    return _summary(symbol, interval, source, df, path, fetched=len(fetched), added=len(df) - prev)


def read(
    symbol: str, interval: str = DEFAULT_INTERVAL, source: str = DEFAULT_PROVIDER
) -> pd.DataFrame | None:
    """Return the stored frame for symbol/interval/source, or None if nothing stored."""
    path = path_for(source, symbol, interval)
    if not path.exists():
        return None
    return pd.read_parquet(path)
