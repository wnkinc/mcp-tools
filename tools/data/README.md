# data — historical market data via OpenBB, persisted to a parquet lake

A self-hosted MCP server for historical market data, built as a thin read-through over
[OpenBB](https://openbb.co) (yfinance provider). OHLCV **bars** (equity + crypto today)
are fetched and *persisted* to a plain parquet lake — each ingest merges into the
symbol's file (de-duplicated on timestamp), so a download is kept and accumulates across
calls. Exposed to the Claude apps over the standard mcp-tools security spine.

```
Claude app ──HTTPS──► Cloudflare Tunnel ──► this server (loopback :8062, OAuth-gated)
                                                  │
                                                  ▼
                          feeds: OpenBB (yfinance) ──► lake: parquet (merge + dedupe)
                                                  │
                                                  ▼
                          <DATA_ROOT>/<asset>/yfinance/<SYMBOL>/<interval>.parquet
```

## Design — three layers, one job each

The code is split so your *owned* surface stays constant as capabilities grow:

| Layer | File | Job | Grows when… |
|---|---|---|---|
| MCP edge | `server.py` | one thin `@mcp.tool` per capability (wires feed → lake → text) | you add a capability |
| OpenBB glue | `feeds.py` | one thin fetch fn per capability, returns OpenBB's frame **as-is** | you add a capability |
| Persistence | `lake.py` | **generic** parquet persist/merge/read, keyed by path segments | never (kind-agnostic) |

OpenBB does the fetch *and* the normalization (its standardized model returns
canonical-named OHLCV), so there is no hand-rolled downloader or normalizer. Commands and
providers are *separate* extensions: each **data type** is a command ext (`openbb-equity`,
`openbb-crypto`); each **source** is a provider ext (`openbb-yfinance`, `openbb-tiingo`).
Two kinds of growth, two costs:

- **New data type** = a command ext + a fn in `feeds.py` + a tool in `server.py`.
- **New source for an existing data type** = just a provider ext — *no new code*. Because
  OpenBB standardizes across providers, it's used via `source="tiingo"` on the same feed
  fn. (Keyed providers like tiingo need a token; `feeds._apply_credentials` injects it
  from the env onto `obb.user.credentials`, since OpenBB doesn't read credential env vars.)

`lake.py` is untouched by either.

Persistence is just pandas + parquet (via pyarrow) — no store engine, no cache catalog.
`lake.ingest` writes with `DataFrame.to_parquet`; a re-ingest reads the existing file,
concatenates, and drops duplicate index entries (the freshly fetched row wins, so
corrections and late values overwrite the stored one). Frames are persisted **as-is** —
OpenBB's own schema, its own `date` index.

## The store

Plain parquet, readable by any pandas/pyarrow/duckdb consumer. Self-describing layout —
the path is the metadata; the first segment is the dataset namespace (`equity`, `crypto`):

```
<DATA_ROOT>/<asset>/<source>/<symbol>/<interval>.parquet   e.g. var/data/equity/tiingo/AAPL/1d.parquet
```

Each ingest fetches the requested window and **merges** it into that file, de-duplicated
on the timestamp index, so the file accumulates history across calls (fetch 2024 today,
2023 tomorrow — the file holds both). `refresh=true` replaces the file with just the new
fetch instead of merging. There is no cache short-circuit: an ingest always hits OpenBB,
then folds the result into what's stored.

## MCP tools

| Tool | Purpose |
|---|---|
| `equity-ingest` | fetch equity OHLCV bars and merge them into the lake → summary |
| `crypto-ingest` | fetch crypto OHLCV bars and merge them into the lake → summary |
| `data-read` | read stored bars back out of the lake (any `asset`; read-only) |

`*-ingest` args: `symbol`, `interval?="1d"` (OpenBB's vocabulary:
`1m/2m/5m/15m/30m/60m/90m/1h/1d/5d/1W/1M/1Q`), `start?`/`end?` (ISO `YYYY-MM-DD`; omit
both = the provider's default window, ~1y for yfinance), `source?="yfinance"` (provider:
`yfinance` or `tiingo`), `refresh?=false`. `data-read` also takes `asset`
(`equity`|`crypto`) and `tail?=10`.

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
| `TIINGO_API_KEY` | _(empty)_ | required only for `source="tiingo"` (empty = tiingo disabled) |

## Egress

Under the L2 egress wall a tool can only reach hosts in its allowlist
(`security/egress-proxy/allowlist/data.txt`). The **yfinance** provider hits Yahoo Finance
(`.finance.yahoo.com`, `query1`/`query2`, `fc.yahoo.com`); the **tiingo** provider adds
`api.tiingo.com` (only when `source="tiingo"`);
the **Google OAuth** hosts are there because with `MCP_AUTH_ENABLED=1` the server verifies
tokens + fetches JWKS server-side through the same proxy (so a missing host fails *login*
closed, not just data). OpenBB does no network at import (the accessor is prebuilt and
frozen); it only reaches out on actual data calls. Discover any misses from `TCP_DENIED`
in `/var/log/squid/access.log`.
