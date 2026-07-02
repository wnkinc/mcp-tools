"""Lake -> QuantConnect Lean data-folder exporter (crypto only, for the lean tool).

Writes stored lake bars into Lean's on-disk format on the shared ``lean-data``
volume, so the lean tool's engine can backtest them. The format is Lean's, not
ours — verified byte-for-byte against the samples bundled in the engine image:

  daily/hour : crypto/<market>/<res>/<symbol>_trade.zip -> <symbol>.csv
               rows "yyyyMMdd HH:mm,O,H,L,C,V" (raw decimal prices)
  minute     : crypto/<market>/minute/<symbol>/<yyyyMMdd>_trade.zip
               -> <yyyyMMdd>_<symbol>_minute_trade.csv
               rows "<ms since midnight>,O,H,L,C,V"

Zips are written atomically (tmp + rename) so the lean engine never reads a
half-written file mid-backtest. Equities are deliberately NOT exported: correct
equity data needs factor/map files (splits/dividends); revisit when needed.
"""
from __future__ import annotations

import os
import tempfile
import zipfile
from pathlib import Path

import pandas as pd

# lake interval -> Lean resolution directory. Only what the lake actually stores.
_RESOLUTIONS = {"1d": "daily", "1h": "hour", "60m": "hour", "1m": "minute"}

_OHLCV = ("open", "high", "low", "close", "volume")


def lean_root() -> Path:
    """The Lean data folder this exporter writes into (the shared volume)."""
    return Path(os.environ.get("LEAN_DATA_ROOT", "/lean-data"))


def _num(x) -> str:
    """Shortest decimal form, full precision, no scientific notation."""
    s = f"{float(x):.10f}".rstrip("0").rstrip(".")
    return s if s else "0"


def _rows(df: pd.DataFrame) -> pd.DataFrame:
    """Normalize a lake frame to naive-UTC-indexed OHLCV columns, sorted."""
    out = df.copy()
    out.columns = [str(c).lower() for c in out.columns]
    missing = [c for c in ("open", "high", "low", "close") if c not in out.columns]
    if missing:
        raise ValueError(f"stored frame lacks columns {missing}; cannot export")
    if "volume" not in out.columns:
        out["volume"] = 0
    idx = pd.DatetimeIndex(out.index)
    if idx.tz is not None:
        idx = idx.tz_convert("UTC").tz_localize(None)
    out.index = idx
    return out[list(_OHLCV)].sort_index()


def _write_zip(dest: Path, entry_name: str, lines: list[str]) -> None:
    """Atomic zip write: the engine may read this folder mid-backtest."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dest.parent, suffix=".tmp")
    os.close(fd)
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr(entry_name, "\n".join(lines) + "\n")
        os.replace(tmp, dest)
    except BaseException:
        os.unlink(tmp)
        raise


def export_crypto(
    df: pd.DataFrame, symbol: str, interval: str, market: str = "coinbase"
) -> dict:
    """Write a lake crypto frame into the Lean data folder; return a summary.

    ``symbol`` is the lake's hyphen-less pair (e.g. BTCUSD); Lean's file naming is
    its lowercase. ``market`` must be one Lean knows (coinbase, binance, bitfinex,
    kraken, bybit) or the engine won't match the files to a security.
    """
    resolution = _RESOLUTIONS.get(interval)
    if resolution is None:
        raise ValueError(
            f"interval {interval!r} has no Lean resolution; exportable: {sorted(_RESOLUTIONS)}"
        )
    bars = _rows(df)
    sym = symbol.lower()
    base = lean_root() / "crypto" / market.lower() / resolution

    if resolution == "minute":
        zips = 0
        for day, chunk in bars.groupby(bars.index.date):
            stamp = day.strftime("%Y%m%d")
            lines = [
                ",".join([
                    str(int((t - t.normalize()).total_seconds() * 1000)),
                    *(_num(r[c]) for c in _OHLCV),
                ])
                for t, r in chunk.iterrows()
            ]
            _write_zip(
                base / sym / f"{stamp}_trade.zip",
                f"{stamp}_{sym}_minute_trade.csv",
                lines,
            )
            zips += 1
        dest = base / sym
    else:
        lines = [
            ",".join([t.strftime("%Y%m%d %H:%M"), *(_num(r[c]) for c in _OHLCV)])
            for t, r in bars.iterrows()
        ]
        _write_zip(base / f"{sym}_trade.zip", f"{sym}.csv", lines)
        zips, dest = 1, base / f"{sym}_trade.zip"

    return {
        "symbol": sym,
        "market": market.lower(),
        "resolution": resolution,
        "rows": int(len(bars)),
        "zips": zips,
        "start": bars.index.min().isoformat(),
        "end": bars.index.max().isoformat(),
        "dest": str(dest),
    }
