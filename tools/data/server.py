"""MCP server: market-data ingest into a canonical parquet data lake.

A port of the ``secure-agentic-engineering`` data-ingest pattern into mcp-tools.
There it was a thin httpx MCP bridge fronting a separate FastAPI runner service;
here — where every tool is already its own hardened, single-venv process — the
runner collapses *into* this server (see ``runs.py``). The deterministic pipeline
(``pipeline.py``: fetch → normalize → enforce_canonical → store) and the canonical
contract (``schema.py``) port over unchanged.

Tools exposed:
  data-ingest        — start a bars ingest; returns the summary, or a PENDING run_id
  data-ingest-poll   — retrieve a slow run's summary by run_id
  data-ingest-cancel — best-effort cancel a run
  data-read          — read canonical bars back out of the lake (cached)
  equity-quote       — latest quote (live)
  equity-fundamentals— income/balance/cash/metrics/dividends (live)
  equity-profile     — company profile (live)
  equity-estimates   — analyst price-target consensus (live)
  equity-ownership   — share statistics / float / short interest (live)
  equity-discovery   — market screens (gainers/losers/active/…) (live)

Bars are fetched via OpenBB (yfinance provider) and ingested into a DuckDB-managed
parquet lake (``store.py``) so repeat ranges hit cache. The ``equity-*`` tools are
live read-through passthroughs over OpenBB — structured market data, hence trusted
output (no guardrail).
"""
import os
import sys
from pathlib import Path

from fastmcp import FastMCP

# Make the repo root importable regardless of CWD (systemd runs us from the tool
# dir), then load the shared Google-OAuth provider used by every public server.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from security.serve import serve  # noqa: E402

import equity  # noqa: E402
import pipeline  # noqa: E402
import runs  # noqa: E402
import store  # noqa: E402
from schema import KIND_BARS  # noqa: E402

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
    where = "cache hit (already stored)" if s.get("cached") else "fetched"
    return (
        f"Ingested {s.get('rows')} {s.get('interval')} bars for "
        f"{s.get('symbol')} ({s.get('source')}): "
        f"{s.get('start')} → {s.get('end')} [{where}].\n"
        f"Stored at {s.get('path')}"
    )


def _pending_line(run_id: str) -> str:
    return (
        f"PENDING: data ingest is still running (run_id={run_id}).\n"
        f'Call the data-ingest-poll tool with run_id="{run_id}" to retrieve the summary '
        f"once it is ready. Do NOT call data-ingest again for this task — that starts a "
        f"new ingest. Use data-ingest-cancel with the same run_id to stop it."
    )


def _report(run_id: str) -> str:
    """Turn a run's terminal/pending state into a model-facing message."""
    st = runs.wait(run_id)
    if st == runs.RUNNING:
        return _pending_line(run_id)
    job = runs.result(run_id)
    if job is None:
        return f"ERROR: data ingest run {run_id} was not found (it may have been lost)."
    if st == runs.SUCCESS and job.result:
        return _summary_line(job.result)
    if st == runs.INTERRUPTED:
        return f"Data ingest run {run_id} was cancelled (interrupted)."
    return f"ERROR: data ingest run {run_id}: {job.error or 'unknown error'}"


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
    Fetch market data and store it in the local canonical data lake.

    Runs a deterministic fetch → clean → store pipeline for one ``symbol`` (e.g.
    "AAPL", "BTC-USD") from Yahoo Finance, saving canonical OHLCV bars as parquet.
    ``interval`` is the bar size (1m, 5m, 15m, 30m, 1h, 1d, 1wk, 1mo; default 1d).
    ``start``/``end`` are ISO dates (YYYY-MM-DD); omit both to fetch full history.
    If the requested range is already stored it returns a cache hit without
    re-downloading — pass ``refresh=true`` to force a re-fetch.

    Fast runs return the summary directly. Long runs return a "PENDING" marker with a
    run_id — call the data-ingest-poll tool with that run_id to retrieve the summary.
    """
    run_id = runs.start(symbol, interval, start, end, source, refresh)
    return _report(run_id)


@mcp.tool(name="data-ingest-poll")
def data_ingest_poll(run_id: str) -> str:
    """
    Retrieve the result of a long-running data ingest started by data-ingest.

    Returns the ingest summary when the run has finished, or another "PENDING" marker
    if it is still working — in which case call data-ingest-poll again with the same run_id.
    """
    return _report(run_id)


@mcp.tool(name="data-ingest-cancel")
def data_ingest_cancel(run_id: str) -> str:
    """
    Cancel an in-progress data ingest started by data-ingest.

    Use the run_id from a PENDING marker. No-op-safe if the run has already finished.
    """
    hard, st = runs.cancel(run_id)
    if st is None:
        return f"Run {run_id} not found (it may have already finished)."
    if hard:
        return f"Cancelled data ingest run {run_id}."
    if st in runs.TERMINAL:
        return f"Run {run_id} could not be cancelled (already {st})."
    return f"Cancel requested for run {run_id}; it will stop after the current download returns."


@mcp.tool(name="data-read")
def data_read(
    symbol: str,
    interval: str = "1d",
    source: str = "yfinance",
    tail: int = 10,
) -> str:
    """
    Read canonical bars back out of the data lake (ingest them first with data-ingest).

    Returns the last ``tail`` rows (default 10) of stored OHLCV bars for
    ``symbol``/``interval``/``source`` as text, plus the total row count and the
    stored file path. Reads only — never fetches.
    """
    symbol = (symbol or "").strip().upper()
    df = store.read(KIND_BARS, source, symbol, interval)
    if df is None or df.empty:
        return (
            f"No stored bars for {symbol} {interval} ({source}). "
            f"Run data-ingest first."
        )
    path = store.path_for(KIND_BARS, source, symbol, interval)
    n = max(0, int(tail))
    view = df.tail(n) if n else df
    return (
        f"{len(df)} {interval} bars for {symbol} ({source}); showing last {len(view)}.\n"
        f"Stored at {path}\n\n"
        f"{view.to_string(index=False)}"
    )


# ── Live equity tools (read-through OpenBB; not lake-cached) ─────────────────


@mcp.tool(name="equity-quote")
def equity_quote(symbol: str, source: str = "yfinance") -> str:
    """
    Latest quote for one equity ``symbol`` (e.g. "AAPL"): price, bid/ask, day range,
    volume, market cap, and related fields. Live — fetched fresh each call, not cached.
    """
    return equity.quote(symbol.strip().upper(), provider=source)


@mcp.tool(name="equity-fundamentals")
def equity_fundamentals(
    symbol: str, statement: str = "income", limit: int = 4, source: str = "yfinance"
) -> str:
    """
    A fundamental financial statement for ``symbol``. ``statement`` is one of:
    income, balance, cash (each shows the last ``limit`` periods), or metrics, dividends.
    Live — fetched fresh each call, not cached.
    """
    return equity.fundamentals(symbol.strip().upper(), statement, limit, provider=source)


@mcp.tool(name="equity-profile")
def equity_profile(symbol: str, source: str = "yfinance") -> str:
    """
    Company profile for ``symbol``: name, exchange, sector/industry, description, and
    identifiers. Live — fetched fresh each call, not cached.
    """
    return equity.profile(symbol.strip().upper(), provider=source)


@mcp.tool(name="equity-estimates")
def equity_estimates(symbol: str, source: str = "yfinance") -> str:
    """
    Analyst price-target consensus and recommendation for ``symbol``.
    Live — fetched fresh each call, not cached.
    """
    return equity.consensus(symbol.strip().upper(), provider=source)


@mcp.tool(name="equity-ownership")
def equity_ownership(symbol: str, source: str = "yfinance") -> str:
    """
    Share statistics for ``symbol``: shares outstanding, float, and short interest.
    Live — fetched fresh each call, not cached.
    """
    return equity.share_statistics(symbol.strip().upper(), provider=source)


@mcp.tool(name="equity-discovery")
def equity_discovery(category: str = "gainers", limit: int = 20, source: str = "yfinance") -> str:
    """
    A market screen (not symbol-specific). ``category`` is one of: gainers, losers,
    active, growth_tech, aggressive_small_caps, undervalued_growth, undervalued_large_caps.
    Returns up to ``limit`` rows. Live — fetched fresh each call, not cached.
    """
    return equity.discovery(category, limit, provider=source)


def main() -> None:
    load_env()
    port = int(os.getenv("MCP_PORT", "8062"))
    # data returns trusted, structured market data -> no guardrail / approval.
    serve(mcp, port=port)


if __name__ == "__main__":
    main()
