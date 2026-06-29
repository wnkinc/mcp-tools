# data — historical market data via OpenBB, persisted to a parquet lake

A self-hosted MCP server for historical market data, built as a thin read-through over
[OpenBB](https://openbb.co) (yfinance provider). **Bars** are fetched and *persisted*
to a plain parquet lake — each ingest merges into the symbol's file (de-duplicated on
timestamp), so a download is kept and accumulates across calls. Exposed to the Claude
apps over the standard mcp-tools security spine.

```
Claude app ──HTTPS──► Cloudflare Tunnel ──► this server (loopback :8062, OAuth-gated)
                                                  │
                                                  ▼
                          bars: OpenBB (yfinance) ──► parquet (merge + dedupe)
                                                  │
                                                  ▼
                          <DATA_ROOT>/bars/yfinance/<SYMBOL>/<interval>.parquet
```

## Design

OpenBB does the fetch *and* the normalization — its standardized model returns
canonical-named OHLCV, so there is no hand-rolled downloader, normalizer, or canonical
re-clean. Commands and providers are *separate* extensions: `openbb-equity` (the
`equity.*` commands) + `openbb-yfinance` (the data). Add a source = another provider
extension; add a data type = another command extension.

Persistence is just pandas + parquet (via pyarrow) — no separate store engine, no cache
catalog. Bars are written with `DataFrame.to_parquet`; a re-ingest reads the existing
file, concatenates, and drops duplicate timestamps (the freshly fetched bar wins, so
corrections and late volume overwrite the stored value). OpenBB's frame is persisted
**as-is** — its own schema, its own `date` index.

What stays custom (and should): the MCP/OAuth surface (`server.py`) and the thin
fetch-merge-persist glue (`bars.py`).

## The store

Plain parquet, readable by any pandas/pyarrow/duckdb consumer. Self-describing layout —
the path is the metadata:

```
<DATA_ROOT>/bars/<source>/<symbol>/<interval>.parquet   e.g. var/data/bars/yfinance/AAPL/1d.parquet
```

Each `data-ingest` fetches the requested window and **merges** it into that file,
de-duplicated on the timestamp index, so the file accumulates history across calls
(fetch 2024 today, 2023 tomorrow — the file holds both). `refresh=true` replaces the
file with just the new fetch instead of merging. There is no cache short-circuit: an
ingest always hits OpenBB, then folds the result into what's stored.

## Code layout

| File | Role |
|---|---|
| `bars.py` | fetch bars via OpenBB + persist to parquet (merge/dedupe/append); read back |
| `server.py` | FastMCP server: OAuth wiring + the MCP tools |

## MCP tools

| Tool | Purpose |
|---|---|
| `data-ingest` | fetch bars and merge them into the parquet lake → summary |
| `data-read` | read stored bars back out of the lake (read-only) |

`data-ingest` args: `symbol`, `interval?="1d"` (OpenBB's vocabulary:
`1m/2m/5m/15m/30m/60m/90m/1h/1d/5d/1W/1M/1Q`), `start?`/`end?` (ISO `YYYY-MM-DD`; omit
both = the provider's default window, ~1y for yfinance), `source?="yfinance"`,
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

## Egress

Under the L2 egress wall a tool can only reach hosts in its allowlist
(`security/egress-proxy/allowlist/data.txt`). All commands use the **yfinance** provider,
so the data hosts are Yahoo Finance (`.finance.yahoo.com`, `query1`/`query2`, `fc.yahoo.com`);
the **Google OAuth** hosts are there because with `MCP_AUTH_ENABLED=1` the server verifies
tokens + fetches JWKS server-side through the same proxy (so a missing host fails *login*
closed, not just data). OpenBB does no network at import (the accessor is prebuilt and
frozen); it only reaches out on actual data calls. Discover any misses from `TCP_DENIED`
in `/var/log/squid/access.log`.
