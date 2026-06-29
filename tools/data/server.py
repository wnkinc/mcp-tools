"""MCP server: historical market-data tools over OpenBB, persisted to a parquet lake.

Three layers, each with one job:
  - this file (server.py) — THIN glue to MCP: one tool per capability
  - feeds.py              — THIN glue to OpenBB: one fetch fn per capability
  - lake.py               — OWNED generic parquet persist/merge/read (kind-agnostic)

A capability tool just wires feed → lake → text. Adding one (another OpenBB endpoint)
is a ``feeds`` fn + a tool here; ``lake.py`` never changes. All tools return trusted,
structured market data (no guardrail).

Tools exposed:
  equity-ingest — fetch equity OHLCV bars and merge them into the lake
  crypto-ingest — fetch crypto OHLCV bars and merge them into the lake
  data-read     — read stored bars back out of the lake (any asset)
"""
import os
import sys
from pathlib import Path

from fastmcp import FastMCP

# Make the repo root importable regardless of CWD (systemd runs us from the tool
# dir), then load the shared Google-OAuth provider used by every public server.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from security.serve import serve  # noqa: E402

import feeds  # noqa: E402
import lake  # noqa: E402

mcp = FastMCP(name="data")


def load_env() -> None:
    env_path = Path(__file__).resolve().parent / ".env"
    if not env_path.exists():
        return
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(env_path, override=True)


def _fmt(s: dict) -> str:
    """Render a lake.ingest summary as a model-facing line."""
    return (
        f"Ingested {s['key']}: fetched {s['fetched']}, +{s['added']} new "
        f"→ {s['rows']} stored ({s['start']} → {s['end']}).\n"
        f"Stored at {s['path']}"
    )


@mcp.tool(name="equity-ingest")
def equity_ingest(
    symbol: str,
    interval: str = "1d",
    start: str | None = None,
    end: str | None = None,
    source: str = "yfinance",
    refresh: bool = False,
) -> str:
    """
    Fetch equity OHLCV bars from Yahoo Finance and persist them to the parquet lake.

    Fetches bars for one equity ``symbol`` (e.g. "AAPL") and merges them into the stored
    file, de-duplicated on timestamp — so the file accumulates history across calls
    (fetch 2024 today, 2023 tomorrow, keep both). ``interval`` is OpenBB's bar size (1m,
    2m, 5m, 15m, 30m, 60m, 90m, 1h, 1d, 5d, 1W, 1M, 1Q; default 1d). ``start``/``end`` are
    ISO dates (YYYY-MM-DD); omit both for the provider's default window (yfinance: ~1y).
    ``source`` is the OpenBB provider: "yfinance" (default, no key) or "tiingo" (needs
    TIINGO_API_KEY). Pass ``refresh=true`` to replace the stored file instead of merging.
    """
    symbol = (symbol or "").strip().upper()
    df = feeds.equity_bars(symbol, interval, start, end, source)
    return _fmt(lake.ingest(("equity", source, symbol, interval), df, refresh=refresh))


@mcp.tool(name="crypto-ingest")
def crypto_ingest(
    symbol: str,
    interval: str = "1d",
    start: str | None = None,
    end: str | None = None,
    source: str = "yfinance",
    refresh: bool = False,
) -> str:
    """
    Fetch crypto OHLCV bars from Yahoo Finance and persist them to the parquet lake.

    Same behavior as equity-ingest but for a crypto pair ``symbol`` (e.g. "BTC-USD",
    "ETH-USD"): merges into the stored file de-duplicated on timestamp, accumulating
    history across calls. ``interval``/``start``/``end``/``refresh`` work identically.
    """
    symbol = (symbol or "").strip().upper()
    df = feeds.crypto_bars(symbol, interval, start, end, source)
    return _fmt(lake.ingest(("crypto", source, symbol, interval), df, refresh=refresh))


@mcp.tool(name="data-read")
def data_read(
    asset: str,
    symbol: str,
    interval: str = "1d",
    source: str = "yfinance",
    tail: int = 10,
) -> str:
    """
    Read stored bars back out of the parquet lake (ingest them first).

    ``asset`` is the dataset namespace: "equity" or "crypto". Returns the last ``tail``
    rows (default 10) of stored OHLCV bars for ``asset``/``symbol``/``interval``/``source``
    as text, plus the total row count and the stored file path. Reads only — never fetches.
    """
    asset = (asset or "").strip().lower()
    symbol = (symbol or "").strip().upper()
    df = lake.read(asset, source, symbol, interval)
    if df is None or df.empty:
        return (
            f"No stored {asset} bars for {symbol} {interval} ({source}). "
            f"Run {asset}-ingest first."
        )
    path = lake.path_for(asset, source, symbol, interval)
    n = max(0, int(tail))
    view = df.tail(n) if n else df
    return (
        f"{len(df)} {interval} {asset} bars for {symbol} ({source}); showing last {len(view)}.\n"
        f"Stored at {path}\n\n"
        f"{view.to_string()}"
    )


def main() -> None:
    load_env()
    port = int(os.getenv("MCP_PORT", "8062"))
    # data returns trusted, structured market data -> no guardrail / approval.
    serve(mcp, port=port)


if __name__ == "__main__":
    main()
