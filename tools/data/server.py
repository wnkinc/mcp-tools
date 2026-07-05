"""MCP server: the lean tool's data feeder — crypto bars from OpenBB into a parquet
lake, exported on demand into the Lean engine's data folder.

Four layers, each with one job:
  - this file (server.py) — THIN glue to MCP: one tool per capability
  - feeds.py              — THIN glue to OpenBB: one fetch fn per capability
  - lake.py               — OWNED generic parquet persist/merge/read (kind-agnostic)
  - lean_export.py        — lake -> Lean on-disk format on the shared lean-data volume

Crypto only: lean's exporter is crypto-only today (equities need factor/map files —
deferred). The agent's pipeline is crypto-ingest -> lean-export -> backtest (lean tool).
All tools return trusted, structured market data (no guardrail).

Tools exposed:
  crypto-ingest — fetch crypto OHLCV bars (Tiingo) and merge them into the lake
  data-catalog  — list what's stored (the inventory: what symbols/intervals exist)
  data-read     — read stored bars for one symbol/interval back out of the lake
  lean-export   — write stored crypto bars into the Lean engine's data folder
"""

import os
import sys
from pathlib import Path

from fastmcp import FastMCP
from mcp.types import ToolAnnotations

# Make the repo root importable regardless of CWD (we run from the tool dir), then
# load the shared Google-OAuth provider used by every public server.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import feeds  # noqa: E402
import lake  # noqa: E402
import lean_export  # noqa: E402
from security.serve import serve  # noqa: E402

# Server-level instructions land in the client's system prompt: the cross-tool
# pipeline lives here, per-tool contracts stay in each docstring.
INSTRUCTIONS = (
    "The lake is the staging store for the Lean backtesting pipeline: crypto-ingest "
    "fetches bars into it, lean-export makes a stored series backtestable by the lean "
    "tool. Discover what's stored with data-catalog before reading; what the lean tool "
    "can actually backtest is its own available_data, not the lake."
)

mcp = FastMCP(name="data", instructions=INSTRUCTIONS)


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


# readOnlyHint groups Claude's permission categories (read-only vs write/delete),
# same mechanism as xmcp's build_annotations. Nothing here is destructive: ingest
# merges (refresh re-fetches from the provider) and export overwrites a derived
# artifact rebuilt from the lake.
@mcp.tool(
    name="crypto-ingest",
    annotations=ToolAnnotations(
        title="Ingest crypto bars",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
def crypto_ingest(
    symbol: str,
    interval: str = "1d",
    start: str | None = None,
    end: str | None = None,
    refresh: bool = False,
) -> str:
    """
    Fetch crypto OHLCV bars from Tiingo and persist them to the parquet lake.

    Fetches bars for one crypto pair ``symbol`` — Tiingo's symbol format is hyphen-less
    (e.g. "BTCUSD", "ETHUSD", not "BTC-USD") — and merges them into the stored file,
    de-duplicated on timestamp, so the file accumulates history across calls (fetch 2024
    today, 2023 tomorrow, keep both). ``interval`` is OpenBB's bar size (1m, 5m, 15m, 30m,
    60m, 1h, 1d, ...; default 1d; the lean pipeline exports 1d/1h/1m). ``start``/``end``
    are ISO dates (YYYY-MM-DD). For deep INTRADAY history just pass the full range — the
    tool pages the provider's per-request cap automatically (a deep pull on a free tier
    can return a PARTIAL result; re-run later with refresh=false and the lake merges it to
    extend coverage). Pass ``refresh=true`` to replace instead of merge. After ingesting,
    make the series backtestable with lean-export.
    """
    return _ingest(
        "crypto", feeds.DEFAULT_PROVIDER, feeds.crypto_bars, symbol, interval, start, end, refresh
    )


def _fmt_catalog(entries: list[dict], header: str) -> str:
    """Render a lake.catalog() listing (key · row count · date span) as a model-facing block."""
    lines = [header]
    for e in entries:
        rows = f"{e['rows']:,}" if e["rows"] is not None else "?"
        span = f"{e['start']} → {e['end']}" if e["start"] else "—"
        lines.append(f"  {e['key']:<34} {rows:>10} rows  {span}")
    return "\n".join(lines)


@mcp.tool(
    name="data-catalog",
    annotations=ToolAnnotations(
        title="List stored datasets", readOnlyHint=True, openWorldHint=False
    ),
)
def data_catalog(asset: str = "") -> str:
    """
    List the market data stored in the lake — the inventory of what's available.

    Call this to answer "what data / symbols do I have?" or "what's available?". It returns
    every stored dataset as ``asset/source/symbol/interval`` with its row count and date
    span. Pass an ``asset`` (e.g. "crypto") to narrow to one namespace; omit it for the
    whole lake. This is the ONLY way to discover what's stored — once you see a dataset
    here, read its rows with data-read. NOTE: the lake is the staging store; what the lean
    tool can backtest is its available_data, not this. Read-only.
    """
    asset = (asset or "").strip().lower()
    entries = lake.catalog(asset) if asset else lake.catalog()
    if not entries:
        where = f" under {asset!r}" if asset else ""
        return (
            f"The lake is empty{where} — nothing ingested yet. Ingest with equity/crypto/fx-ingest."
        )
    scope = f"{asset!r} datasets" if asset else "all stored datasets"
    return _fmt_catalog(entries, f"Lake — {scope} ({len(entries)}):")


@mcp.tool(
    name="data-read",
    annotations=ToolAnnotations(title="Read stored bars", readOnlyHint=True, openWorldHint=False),
)
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
    ``asset``/``source``/``symbol``/``interval`` — ``asset`` is the namespace (e.g.
    "crypto"); ``source`` is the provider it was ingested from ("tiingo" default).
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
                have,
                f"No {asset or '?'}/{source}/{symbol}/{interval} stored. "
                f"Stored for {symbol} — re-read with a matching source+interval:",
            )
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


@mcp.tool(
    name="lean-export",
    annotations=ToolAnnotations(
        title="Export bars to Lean",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
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
        f'Backtest with: self.add_crypto("{symbol}", Resolution.{s["resolution"].upper()}, '
        f"Market.{s['market'].upper()})"
    )


def main() -> None:
    load_env()
    port = int(os.getenv("MCP_PORT", "8062"))
    # data returns trusted, structured market data -> no guardrail / approval.
    serve(mcp, port=port)


if __name__ == "__main__":
    main()
