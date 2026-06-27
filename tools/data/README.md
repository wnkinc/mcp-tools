# data — market data via OpenBB into a DuckDB parquet lake

A self-hosted MCP server for market data. **Bars** are fetched through
[OpenBB](https://openbb.co) (yfinance provider) and ingested into a **canonical,
engine-agnostic parquet data lake** managed by **DuckDB**, so repeat ranges hit cache.
A set of **live `equity-*` tools** expose the rest of OpenBB's equity surface
(quote, fundamentals, profile, estimates, ownership, discovery) as read-through
passthroughs. Exposed to the Claude apps over the standard mcp-tools security spine.

```
Claude app ──HTTPS──► Cloudflare Tunnel ──► this server (loopback :8062, OAuth-gated)
                                                  │
                          ┌───────────────────────┴───────────────────────┐
                          ▼                                                 ▼
            bars: OpenBB (yfinance) ──► enforce_canonical ──► DuckDB    equity-*: OpenBB
                          │                                   parquet    (live, not cached)
                          ▼                                   lake
            <DATA_ROOT>/bars/yfinance/<SYMBOL>/<interval>.parquet
            <DATA_ROOT>/_catalog.duckdb   (cache coverage bookkeeping)
```

## What changed (OpenBB + DuckDB rewrite)

The earlier port hand-rolled a yfinance download (`sources/yfinance.py`), a normalizer
(`normalize.py`), and a pyarrow parquet reader/writer (`store.py`). Those are gone:

- **OpenBB** replaces fetch + normalize — its standardized model already returns
  canonical-named OHLCV. Commands and providers are *separate* extensions: `openbb-equity`
  (the `equity.*` commands) + `openbb-yfinance` (the data). Add a source = another provider
  extension; add a data type = another command extension.
- **DuckDB** replaces the parquet I/O and holds the cache catalog. Bars files stay plain,
  engine-agnostic parquet; only the cache bookkeeping lives in DuckDB.

What stays custom (and should): the MCP/OAuth surface (`server.py`), the async run
registry (`runs.py`), the canonical contract (`schema.py`), and the cache *policy*
(requested-window subset; DuckDB stores it, our code decides it).

## The store

Plain parquet, readable by any pandas/pyarrow/duckdb consumer. Self-describing layout
(the path is the metadata); a tiny DuckDB file alongside holds cache coverage:

```
<DATA_ROOT>/<kind>/<source>/<symbol>/<interval>.parquet   e.g. var/data/bars/yfinance/AAPL/1d.parquet
<DATA_ROOT>/_catalog.duckdb                                 coverage(kind,source,symbol,interval → req_start,req_end,fetched_at)
```

Canonical bars schema: `timestamp` (UTC), `open`, `high`, `low`, `close`, `volume`.

A request whose range is already stored returns a **cache hit** without re-downloading
(`refresh=true` forces a re-fetch). Coverage compares *requested* windows (recorded in
the catalog), so holidays/weekends and a provider's exclusive `end` don't defeat the cache.

## Code layout (separated by lifecycle stage)

| File | Role |
|---|---|
| `schema.py` | canonical contract + `enforce_canonical`; the `kinds` vocabulary |
| `sources/openbb_source.py` | fetch canonical-named bars via OpenBB (interval map + full-history anchor) |
| `store.py` | the only reader/writer — DuckDB parquet I/O + the cache catalog |
| `pipeline.py` | the deterministic bars ingest (fetch → enforce → store) |
| `runs.py` | in-process run registry + thread pool (slow runs return a PENDING run_id) |
| `equity.py` | live (non-cached) OpenBB equity passthroughs |
| `server.py` | FastMCP server: OAuth wiring + the MCP tools |

## MCP tools

| Tool | Purpose | Cached? |
|---|---|---|
| `data-ingest` | start a bars ingest → summary, or a `PENDING` run_id for a slow run | lake |
| `data-ingest-poll` | retrieve a slow run's summary by run_id | — |
| `data-ingest-cancel` | best-effort cancel a run | — |
| `data-read` | read canonical bars back out of the lake (read-only) | lake |
| `equity-quote` | latest quote (price, bid/ask, day range, market cap) | live |
| `equity-fundamentals` | income / balance / cash / metrics / dividends | live |
| `equity-profile` | company profile (name, exchange, sector, identifiers) | live |
| `equity-estimates` | analyst price-target consensus + recommendation | live |
| `equity-ownership` | shares outstanding / float / short interest | live |
| `equity-discovery` | market screens (gainers/losers/active/…) | live |

`data-ingest` args: `symbol`, `interval?="1d"` (1m/5m/15m/30m/1h/1d/1wk/1mo),
`start?`/`end?` (ISO `YYYY-MM-DD`; omit both = full history), `source?="yfinance"`,
`refresh?=false`.

## Setup & run

```bash
cd tools/data
python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -c "import openbb"   # PREBUILD OpenBB's accessor into the venv (one-time)
cp env.example .env                   # fill Google creds + email allowlist; set MCP_AUTH_ENABLED=1 for public
.venv/bin/python server.py            # serves on http://127.0.0.1:8062 (MCP_PORT)
.venv/bin/python -m pytest            # tests (no network; tmp data lake)
```

The prebuild matters: OpenBB code-generates its `obb` accessor at import time, and on the
hardened unit the venv is read-only — so the build must happen now (writable), and the unit
sets `OPENBB_AUTO_BUILD=false` so import never tries to write into the venv at runtime.
Rerun the prebuild only after adding/removing an OpenBB extension.

To publish to the Claude apps, follow the standard mcp-tools wiring: hosts in
`security/egress-proxy/allowlist/data.txt`, the squid `http_port`/`acl`/`http_access`
lines for `:8074`, `sudo scripts/install-system.sh`, then
`scripts/add-tunnel-route.sh data.secure-agentic-engineering.com 8062`.

## Env vars

| Var | Default | Meaning |
|---|---|---|
| `MCP_PORT` | `8062` | loopback MCP port |
| `MCP_AUTH_ENABLED` | `0` | `1` = require Google OAuth (public serving) |
| `OPENBB_AUTO_BUILD` | `false` (unit) | freeze the prebuilt accessor; never rebuild at import |
| `HOME` | StateDirectory (unit) | OpenBB derives `$HOME/.openbb_platform` from this; must be writable |
| `DATA_ROOT` | `<tool>/var/data` | data-lake root (set to the StateDirectory on the unit) |
| `DATA_MAX_WORKERS` | `4` | ingest thread-pool size |
| `DATA_INLINE_BUDGET_S` | `20` | seconds `data-ingest` blocks before returning a `PENDING` run_id |

## Egress

Under the L2 egress wall a tool can only reach hosts in its allowlist
(`security/egress-proxy/allowlist/data.txt`). All commands use the **yfinance** provider,
so the data hosts are Yahoo Finance (`.finance.yahoo.com`, `query1`/`query2`, `fc.yahoo.com`);
the **Google OAuth** hosts are there because with `MCP_AUTH_ENABLED=1` the server verifies
tokens + fetches JWKS server-side through the same proxy (so a missing host fails *login*
closed, not just data). OpenBB does no network at import (the accessor is prebuilt and
frozen); it only reaches out on actual data calls. Discover any misses from `TCP_DENIED`
in `/var/log/squid/access.log`.
