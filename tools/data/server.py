"""MCP server: historical market-data tools over OpenBB, persisted to a parquet lake.

Three layers, each with one job:
  - this file (server.py) — THIN glue to MCP: one tool per capability
  - feeds.py              — THIN glue to OpenBB: one fetch fn per capability
  - lake.py               — OWNED generic parquet persist/merge/read (kind-agnostic)

A capability tool just wires feed → lake → text. Adding one (another OpenBB endpoint)
is a ``feeds`` fn + a tool here; ``lake.py`` never changes. All tools return trusted,
structured market data (no guardrail).

Equity + crypto are fetched from Tiingo (fixed provider — no source choice; see feeds.py).

Tools exposed:
  equity-ingest — fetch equity OHLCV bars and merge them into the lake
  crypto-ingest — fetch crypto OHLCV bars and merge them into the lake
  fx-ingest     — fetch FX (currency pair) OHLC bars and merge them into the lake
  data-catalog  — list what's stored (the inventory: what assets/symbols/intervals exist)
  data-read     — read stored bars for one asset/symbol/interval back out of the lake
  lean-export   — write stored crypto bars into the Lean engine's data folder
                  (the shared volume the lean tool backtests against; see lean_export.py)
"""
import os
import sys
from pathlib import Path

from fastmcp import FastMCP

# Make the repo root importable regardless of CWD (we run from the tool dir), then
# load the shared Google-OAuth provider used by every public server.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from security.serve import serve  # noqa: E402

import feeds  # noqa: E402
import lake  # noqa: E402
import lean_export  # noqa: E402

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


def _fmt(s: dict, partial: str | None = None) -> str:
    """Render a lake.ingest summary as a model-facing line (+ a partial-result note)."""
    line = (
        f"Ingested {s['key']}: fetched {s['fetched']}, +{s['added']} new "
        f"→ {s['rows']} stored ({s['start']} → {s['end']}).\n"
        f"Stored at {s['path']}"
    )
    if partial:
        line += (
            f"\n⚠️ PARTIAL — stopped early ({partial}). Stored only back to {s['start']}; "
            f"re-run the same ingest later (refresh=false) to extend further back. Do NOT "
            f"keep retrying now — it will stay rate-limited until the hourly window resets."
        )
    return line


def _is_rate_limit(exc: Exception) -> bool:
    msg = str(exc).lower()
    return "allocation" in msg or "429" in msg


def _ingest(asset: str, source: str, fetch, symbol, interval, start, end, refresh) -> str:
    """Shared ingest body: fetch via a feed → persist (under ``source``) → format.

    A mid-walk rate limit comes back as a *partial* frame (kept, flagged); a rate limit on
    the very first fetch means nothing was retrieved, so return a clean note instead of
    surfacing a raw provider exception to the model.
    """
    symbol = (symbol or "").strip().upper()
    try:
        df = fetch(symbol, interval, start, end)
        summary = lake.ingest((asset, source, symbol, interval), df, refresh=refresh)
    except Exception as exc:  # noqa: BLE001
        if _is_rate_limit(exc):
            return (
                "Rate limited — the provider's request allocation is exhausted; nothing was "
                "ingested. Wait for the window to reset, then retry. Do NOT keep retrying now."
            )
        if isinstance(exc, ValueError):  # bad interval, missing start/key, empty result
            return f"Cannot ingest {asset} {symbol} from {source}: {exc}"
        raise
    return _fmt(summary, partial=df.attrs.get("partial"))


@mcp.tool(name="equity-ingest")
def equity_ingest(
    symbol: str,
    interval: str = "1d",
    start: str | None = None,
    end: str | None = None,
    source: str = "tiingo",
    refresh: bool = False,
) -> str:
    """
    Fetch equity OHLCV bars and persist them to the parquet lake.

    Fetches bars for one equity ``symbol`` (e.g. "AAPL") and merges them into the stored
    file, de-duplicated on timestamp — so the file accumulates history across calls
    (fetch 2024 today, 2023 tomorrow, keep both). ``interval`` is OpenBB's bar size (1m,
    2m, 5m, 15m, 30m, 60m, 90m, 1h, 1d, 5d, 1W, 1M, 1Q; default 1d). ``start``/``end`` are
    ISO dates (YYYY-MM-DD). For deep INTRADAY history just pass the full ``start``/``end``
    range you want — the tool pages the provider's per-request cap automatically (a deep
    pull on a free tier can return a PARTIAL result; re-run later with refresh=false and the
    lake merges it to extend coverage).

    ``source`` is the data provider: "tiingo" (DEFAULT) or "databento". Use "databento" ONLY
    when the user explicitly asks for Databento by name — it's a separate paid SDK (needs
    DATABENTO_API_KEY), consolidated US equities (Nasdaq + NYSE), with only 1s/1m/1h/1d bars
    and a required ``start`` (history from 2023-03-28). Otherwise leave the default. Each source is stored in its own namespace, so
    read it back with the same ``source``. Pass ``refresh=true`` to replace instead of merge.
    """
    source = (source or "tiingo").strip().lower()
    if source not in ("tiingo", "databento"):
        return f"Unknown source {source!r}. Use 'tiingo' (default) or 'databento'."
    fetch = feeds.databento_bars if source == "databento" else feeds.equity_bars
    return _ingest("equity", source, fetch, symbol, interval, start, end, refresh)


@mcp.tool(name="crypto-ingest")
def crypto_ingest(
    symbol: str,
    interval: str = "1d",
    start: str | None = None,
    end: str | None = None,
    refresh: bool = False,
) -> str:
    """
    Fetch crypto OHLCV bars from Tiingo and persist them to the parquet lake.

    Same behavior as equity-ingest but for a crypto pair ``symbol`` — note Tiingo's symbol
    format is hyphen-less (e.g. "BTCUSD", "ETHUSD", not "BTC-USD"). Merges into the stored
    file de-duplicated on timestamp, accumulating history across calls.
    ``interval``/``start``/``end``/``refresh`` work identically.
    """
    return _ingest("crypto", feeds.DEFAULT_PROVIDER, feeds.crypto_bars, symbol, interval, start, end, refresh)


@mcp.tool(name="fx-ingest")
def fx_ingest(
    symbol: str,
    interval: str = "1d",
    start: str | None = None,
    end: str | None = None,
    refresh: bool = False,
) -> str:
    """
    Fetch FX (currency pair) OHLC bars from Tiingo and persist them to the parquet lake.

    Same behavior as equity-ingest but for a currency pair ``symbol`` (e.g. "EURUSD",
    "GBPUSD", "USDJPY"). FX frames carry OHLC but no volume. Merges into the stored file
    de-duplicated on timestamp. ``interval``/``start``/``end``/``refresh`` work identically.
    """
    return _ingest("fx", feeds.DEFAULT_PROVIDER, feeds.fx_bars, symbol, interval, start, end, refresh)


def _fmt_catalog(entries: list[dict], header: str) -> str:
    """Render a lake.catalog() listing (key · row count · date span) as a model-facing block."""
    lines = [header]
    for e in entries:
        rows = f"{e['rows']:,}" if e["rows"] is not None else "?"
        span = f"{e['start']} → {e['end']}" if e["start"] else "—"
        lines.append(f"  {e['key']:<34} {rows:>10} rows  {span}")
    return "\n".join(lines)


@mcp.tool(name="data-catalog")
def data_catalog(asset: str = "") -> str:
    """
    List the market data stored in the lake — the inventory of what's available.

    Call this to answer "what data / symbols do I have?" or "what's available?". It returns
    every stored dataset as ``asset/source/symbol/interval`` with its row count and date
    span. Pass an ``asset`` ("equity"/"crypto"/"fx") to narrow to one namespace; omit it for
    the whole lake. This is the ONLY way to discover what's stored — once you see a dataset
    here, read its rows with data-read. Read-only.
    """
    asset = (asset or "").strip().lower()
    entries = lake.catalog(asset) if asset else lake.catalog()
    if not entries:
        where = f" under {asset!r}" if asset else ""
        return f"The lake is empty{where} — nothing ingested yet. Ingest with equity/crypto/fx-ingest."
    scope = f"{asset!r} datasets" if asset else "all stored datasets"
    return _fmt_catalog(entries, f"Lake — {scope} ({len(entries)}):")


@mcp.tool(name="data-read")
def data_read(
    asset: str,
    symbol: str,
    interval: str = "1d",
    source: str = "tiingo",
    tail: int = 10,
) -> str:
    """
    Read a stored market-data series back out of the parquet lake. Read-only.

    Returns the last ``tail`` rows (default 10) of the stored bars for
    ``asset``/``source``/``symbol``/``interval`` — ``asset`` is "equity"/"crypto"/"fx";
    ``source`` is the provider it was ingested from ("tiingo" default, or "databento").
    Bars are keyed by interval AND source, so both must match what was ingested. If that
    exact series isn't stored, you get the list of what IS stored for ``symbol`` so you can
    retry with the right interval/source. To see everything available first, use data-catalog.
    """
    asset = (asset or "").strip().lower()
    symbol = (symbol or "").strip().upper()
    source = (source or "tiingo").strip().lower()

    df = lake.read(asset, source, symbol, interval)
    if df is None or df.empty:  # miss -> self-correcting hint, not a dead end
        have = [e for e in lake.catalog() if symbol in e["key"].split("/")]
        if have:
            return _fmt_catalog(
                have, f"No {asset or '?'}/{source}/{symbol}/{interval} stored. "
                f"Stored for {symbol} — re-read with a matching source+interval:")
        return (
            f"Nothing stored for {symbol}. Ingest it first, or call data-catalog to see "
            f"what's available."
        )
    path = lake.path_for(asset, source, symbol, interval)
    n = max(0, int(tail))
    view = df.tail(n) if n else df
    return (
        f"{len(df)} {interval} {asset} bars for {symbol} ({source}); showing last {len(view)}.\n"
        f"Stored at {path}\n\n"
        f"{view.to_string()}"
    )


@mcp.tool(name="lean-export")
def lean_export_tool(
    symbol: str,
    interval: str = "1d",
    source: str = "tiingo",
    market: str = "coinbase",
) -> str:
    """
    Export stored crypto bars from the lake into the Lean engine's data folder.

    This is the bridge to the lean backtesting tool: after crypto-ingest, call this
    to make the series backtestable. ``symbol``/``interval``/``source`` name the
    stored lake series (same values as data-read; crypto only). ``market`` is the
    Lean market the files are registered under (default "coinbase"; also binance,
    bitfinex, kraken, bybit) — the backtest must then subscribe with the SAME market,
    e.g. self.add_crypto("BTCUSD", Resolution.DAILY, Market.COINBASE). Re-running
    overwrites the exported files with the lake's current contents (atomic).
    """
    symbol = (symbol or "").strip().upper()
    source = (source or "tiingo").strip().lower()
    df = lake.read("crypto", source, symbol, interval)
    if df is None or df.empty:
        return (
            f"Nothing stored for crypto/{source}/{symbol}/{interval} — ingest it first "
            f"with crypto-ingest, or check data-catalog for what's stored."
        )
    try:
        s = lean_export.export_crypto(df, symbol, interval, market=market)
    except ValueError as exc:
        return f"Cannot export crypto {symbol} {interval}: {exc}"
    return (
        f"Exported crypto/{source}/{symbol}/{interval} -> Lean {s['market']}/{s['resolution']}: "
        f"{s['rows']} bars ({s['start']} → {s['end']}) in {s['zips']} zip(s).\n"
        f"Written to {s['dest']}\n"
        f"Backtest with: self.add_crypto(\"{symbol}\", Resolution.{s['resolution'].upper()}, "
        f"Market.{s['market'].upper()})"
    )


def main() -> None:
    load_env()
    port = int(os.getenv("MCP_PORT", "8062"))
    # data returns trusted, structured market data -> no guardrail / approval.
    serve(mcp, port=port)


if __name__ == "__main__":
    main()
