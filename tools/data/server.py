"""MCP server: historical market-data tools over OpenBB (yfinance provider).

Bars are fetched through OpenBB and *persisted* to a plain parquet lake (``bars.py``)
so a download is kept and accumulates across calls. Returns trusted, structured market
data (no guardrail).

Tools exposed:
  data-ingest        — fetch bars and merge them into the parquet lake
  data-read          — read stored bars back out of the lake
"""
import os
import sys
from pathlib import Path

from fastmcp import FastMCP

# Make the repo root importable regardless of CWD (systemd runs us from the tool
# dir), then load the shared Google-OAuth provider used by every public server.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from security.serve import serve  # noqa: E402

import bars  # noqa: E402

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


def _summary_line(s: dict) -> str:
    return (
        f"Ingested {s.get('symbol')} {s.get('interval')} bars ({s.get('source')}): "
        f"fetched {s.get('fetched')}, +{s.get('added')} new "
        f"→ {s.get('rows')} stored ({s.get('start')} → {s.get('end')}).\n"
        f"Stored at {s.get('path')}"
    )


@mcp.tool(name="data-ingest")
def data_ingest(
    symbol: str,
    interval: str = "1d",
    start: str | None = None,
    end: str | None = None,
    source: str = "yfinance",
    refresh: bool = False,
) -> str:
    """
    Fetch market data from Yahoo Finance and persist it to the local parquet lake.

    Fetches OHLCV bars for one ``symbol`` (e.g. "AAPL", "BTC-USD") and merges them into
    the symbol's stored parquet file, de-duplicating on timestamp — so the file
    accumulates history across calls (fetch 2024 today, 2023 tomorrow, keep both).
    ``interval`` is OpenBB's bar size (1m, 2m, 5m, 15m, 30m, 60m, 90m, 1h, 1d, 5d, 1W,
    1M, 1Q; default 1d). ``start``/``end`` are ISO dates (YYYY-MM-DD); omit both for the
    provider's default window (yfinance: ~1y). Pass ``refresh=true`` to replace the
    stored file with just this fetch instead of merging.
    """
    return _summary_line(bars.ingest(symbol, interval, start, end, source, refresh))


@mcp.tool(name="data-read")
def data_read(
    symbol: str,
    interval: str = "1d",
    source: str = "yfinance",
    tail: int = 10,
) -> str:
    """
    Read stored bars back out of the parquet lake (ingest them first with data-ingest).

    Returns the last ``tail`` rows (default 10) of stored OHLCV bars for
    ``symbol``/``interval``/``source`` as text, plus the total row count and the
    stored file path. Reads only — never fetches.
    """
    symbol = (symbol or "").strip().upper()
    df = bars.read(symbol, interval, source)
    if df is None or df.empty:
        return (
            f"No stored bars for {symbol} {interval} ({source}). "
            f"Run data-ingest first."
        )
    path = bars.path_for(source, symbol, interval)
    n = max(0, int(tail))
    view = df.tail(n) if n else df
    return (
        f"{len(df)} {interval} bars for {symbol} ({source}); showing last {len(view)}.\n"
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
