"""The store — the only reader/writer of the canonical parquet data lake.

Layout (self-describing; the path *is* the metadata):

    <DATA_ROOT>/<kind>/<source>/<symbol>/<interval>.parquet
    e.g.  var/data/bars/yfinance/AAPL/1d.parquet

Engine: **DuckDB** does the parquet I/O (``COPY ... (FORMAT PARQUET)`` / ``read_parquet``)
and holds the cache catalog, replacing the hand-rolled pyarrow reader/writer. The bars
files stay plain, engine-agnostic parquet readable by any pandas/pyarrow/duckdb consumer;
only the *cache bookkeeping* lives in DuckDB.

Cache coverage is decided by comparing *requested* windows, not data bounds: markets
have holidays/weekends and a provider's ``end`` is often exclusive, so the stored data
rarely lands exactly on the requested edges. We record the requested ``[start, end]`` in
a small DuckDB catalog table (``<DATA_ROOT>/_catalog.duckdb``) and treat a new request as
covered iff its window is a subset of the stored one. Whole-range only — no incremental
gap-filling in this phase.
"""
from __future__ import annotations

import datetime
import os
import threading
from pathlib import Path

import duckdb
import pandas as pd

# Tool dir = this file's parent; the repo-local default lake lives under it.
_TOOL_ROOT = Path(__file__).resolve().parent

# DuckDB connections are not safe for concurrent use across threads (runs.py uses a
# thread pool), so serialize every catalog/parquet operation behind one lock and reuse
# one connection per catalog path (a second read-write connect() to the same file in the
# same process would fail to take the lock).
_lock = threading.RLock()
_conns: dict[str, duckdb.DuckDBPyConnection] = {}


def data_root() -> Path:
    """Shared data lake root; ``DATA_ROOT`` overrides the tool-local default."""
    return Path(os.environ.get("DATA_ROOT") or (_TOOL_ROOT / "var" / "data"))


def _safe(symbol: str) -> str:
    """Filesystem-safe symbol (only '/' is genuinely problematic on Linux)."""
    return symbol.replace("/", "_")


def path_for(kind: str, source: str, symbol: str, interval: str) -> Path:
    return data_root() / kind / source / _safe(symbol) / f"{interval}.parquet"


def _sql_str(value) -> str:
    """Escape a path/string for inlining into DuckDB SQL (single-quote doubling)."""
    return str(value).replace("'", "''")


def _catalog_path() -> Path:
    return data_root() / "_catalog.duckdb"


def _con() -> duckdb.DuckDBPyConnection:
    """Lazily open (and cache) the catalog connection for the current DATA_ROOT.

    Caller must hold ``_lock``.
    """
    p = _catalog_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    key = str(p)
    con = _conns.get(key)
    if con is None:
        con = duckdb.connect(key)
        # Render TIMESTAMPTZ back in UTC (DuckDB defaults to the machine's local zone),
        # keeping the canonical "timestamps are UTC" promise on read-back.
        con.execute("SET TimeZone = 'UTC'")
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS coverage (
                kind       VARCHAR,
                source     VARCHAR,
                symbol     VARCHAR,
                interval   VARCHAR,
                req_start  VARCHAR,
                req_end    VARCHAR,
                fetched_at VARCHAR,
                PRIMARY KEY (kind, source, symbol, interval)
            )
            """
        )
        _conns[key] = con
    return con


def write(
    kind: str,
    source: str,
    symbol: str,
    interval: str,
    df: pd.DataFrame,
    *,
    req_start: str | None,
    req_end: str | None,
) -> Path:
    """Write a canonical frame to parquet (overwriting) and record the requested window."""
    p = path_for(kind, source, symbol, interval)
    p.parent.mkdir(parents=True, exist_ok=True)
    fetched = datetime.datetime.now(datetime.UTC).isoformat()
    with _lock:
        con = _con()
        con.register("_write_df", df)
        try:
            con.execute(f"COPY _write_df TO '{_sql_str(p)}' (FORMAT PARQUET)")
        finally:
            con.unregister("_write_df")
        con.execute(
            """
            INSERT INTO coverage VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (kind, source, symbol, interval) DO UPDATE SET
                req_start  = excluded.req_start,
                req_end    = excluded.req_end,
                fetched_at = excluded.fetched_at
            """,
            [kind, source, symbol, interval, req_start, req_end, fetched],
        )
    return p


def read(kind: str, source: str, symbol: str, interval: str) -> pd.DataFrame | None:
    """Return the stored canonical frame, or None if nothing is stored yet."""
    p = path_for(kind, source, symbol, interval)
    if not p.exists():
        return None
    with _lock:
        con = _con()
        return con.execute(f"SELECT * FROM read_parquet('{_sql_str(p)}')").df()


def request_window(
    kind: str, source: str, symbol: str, interval: str
) -> tuple[str | None, str | None] | None:
    """Return the stored ``(req_start, req_end)``, or None if nothing is recorded."""
    with _lock:
        con = _con()
        row = con.execute(
            "SELECT req_start, req_end FROM coverage "
            "WHERE kind = ? AND source = ? AND symbol = ? AND interval = ?",
            [kind, source, symbol, interval],
        ).fetchone()
    if row is None:
        return None
    start, end = row
    return (start or None, end or None)


def covers(
    kind: str, source: str, symbol: str, interval: str, start: str | None, end: str | None
) -> bool:
    """True if a prior fetch's requested window already contains ``[start, end]``.

    A ``None`` stored bound means "unbounded" on that side (a full-history fetch);
    a ``None`` requested bound means the caller wants it unbounded, which only a
    stored unbounded bound can satisfy. The parquet file must also still exist.
    """
    if not path_for(kind, source, symbol, interval).exists():
        return False
    win = request_window(kind, source, symbol, interval)
    if win is None:
        return False
    stored_start, stored_end = win
    if stored_start is not None:  # we only have data from stored_start onward
        if start is None or pd.Timestamp(start) < pd.Timestamp(stored_start):
            return False
    if stored_end is not None:  # we only fetched up to stored_end
        if end is None or pd.Timestamp(end) > pd.Timestamp(stored_end):
            return False
    return True
