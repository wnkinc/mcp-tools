# data — historical market data (Tiingo via OpenBB) → parquet lake

Self-hosted MCP server. Fetches OHLCV **bars** (equity, crypto, FX) from **Tiingo** via
[OpenBB](https://openbb.co) and persists them to a plain parquet lake, de-duplicated on
timestamp so repeated ingests accumulate history. Loopback `:8062`, OAuth-gated, behind the
standard mcp-tools security spine.

## MCP tools

| Tool | Purpose |
|---|---|
| `equity-ingest` | fetch equity OHLCV bars → merge into the lake |
| `crypto-ingest` | fetch crypto OHLCV bars → merge into the lake |
| `fx-ingest` | fetch FX (currency pair) OHLC bars → merge into the lake |
| `data-catalog` | list what's stored — the inventory of assets/symbols/intervals (read-only) |
| `data-read` | read one stored series back out of the lake (read-only) |

**`*-ingest` args:** `symbol` (Tiingo-style: crypto/FX are hyphen-less — `BTCUSD`, `EURUSD`),
`interval?="1d"` (`1m/2m/5m/15m/30m/60m/90m/1h/1d/5d/1W/1M/1Q`), `start?`/`end?` (ISO
`YYYY-MM-DD`), `refresh?=false` (replace instead of merge). `equity-ingest` also takes
`source?="tiingo"` — pass `"databento"` only when explicitly asked for it by name (paid SDK,
consolidated US equities, 1s/1m/1h/1d only, `start` required, needs `DATABENTO_API_KEY`).
**`data-catalog`** is discovery — call it to see what's available: every stored dataset
(`asset/source/symbol/interval` + row count + date span), optionally narrowed by `asset`
(`equity`|`crypto`|`fx`). **`data-read`** reads one series: `asset`, `symbol`, `interval?="1d"`,
`source?="tiingo"` (must match how it was ingested), `tail?=10`. Bars are keyed by interval AND
source; a read miss returns what *is* stored for that symbol, so the LLM can discover-then-drill
rather than dead-end.

Deep **intraday** pulls page the provider's per-request cap automatically. On Tiingo's free
tier a multi-year 1m pull can exhaust the hourly request limit and return a **PARTIAL** result
— just re-run later (keep `refresh=false`) and the lake merge extends coverage.

## Design

Three layers, so the owned surface stays constant as capabilities grow:

| File | Job |
|---|---|
| `server.py` | thin `@mcp.tool` per capability (wires feed → lake → text) |
| `feeds.py` | thin per-capability OpenBB fetch fns; pages Tiingo's intraday cap |
| `lake.py` | generic parquet persist/merge/read, keyed by path segments (never changes per capability) |

OpenBB standardizes fetch + schema across providers, so a **new data type** = a command ext
(`openbb-equity`/`-crypto`/`-currency`) + a `feeds` fn + a tool; a **new OpenBB source** =
just a provider ext, used via `provider=` (no new code). Keyed providers (Tiingo) need a
token — `feeds._apply_credentials` injects `TIINGO_API_KEY` onto `obb.user.credentials`, since
OpenBB doesn't read credential env vars. `openbb-yfinance` stays installed/reachable in code
but no tool uses it.

A source OpenBB doesn't front uses its **own SDK** behind the same `feeds` seam:
`feeds.databento_bars` (Databento) is an opt-in alternative for `equity-ingest`
(`source="databento"`), keyed into the lake under its own `databento` namespace. The lake
doesn't care whether a feed is OpenBB- or SDK-backed.

## The store

Self-describing layout — the path is the metadata (first segment = dataset namespace):

```
<DATA_ROOT>/<asset>/<source>/<symbol>/<interval>.parquet   e.g. var/data/fx/tiingo/EURUSD/1d.parquet
```

Plain parquet (pandas + pyarrow), readable by any consumer; frames are stored **as-is** in
the source's own schema. Each ingest merges into the file de-dup'd on the timestamp index;
`refresh=true` replaces.

## Setup & run

```bash
cd tools/data
python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -c "import openbb"   # PREBUILD the obb accessor into the venv (one-time)
cp env.example .env                   # set TIINGO_API_KEY (+ Google creds for public serving)
.venv/bin/python server.py            # serves on http://127.0.0.1:8062
.venv/bin/python -m pytest            # tests (no network)
```

OpenBB code-gens its `obb` accessor at import; the image prebuilds it at build time and
freezes it (`OPENBB_AUTO_BUILD=false`) so it never rebuilds on the read-only rootfs.
Rerun the prebuild only after adding/removing an extension. To publish: allowlist hosts
in `security/egress-proxy/allowlist/data.txt` and add a route in
`security/ingress/cloudflared.config.yml`, then `docker compose up -d --build data`.

## Env vars

| Var | Default | Meaning |
|---|---|---|
| `TIINGO_API_KEY` | _(empty)_ | **required** — Tiingo is the default provider; empty → ingest fails |
| `DATABENTO_API_KEY` | _(empty)_ | optional — only for `equity-ingest source="databento"` |
| `MCP_PORT` | `8062` | MCP port |
| `MCP_AUTH_ENABLED` | `0` | `1` = require Google OAuth (public serving) |
| `DATA_ROOT` | `/app/state/data` | parquet lake root (writable state volume) |
| `OPENBB_AUTO_BUILD` | `false` | freeze the prebuilt accessor; never rebuild at import |
| `HOME` | `/app/state` | OpenBB writes `$HOME/.openbb_platform`; must be writable |

## Egress

Behind the egress wall, allowed hosts live in `security/egress-proxy/allowlist/data.txt`:
`api.tiingo.com` (default data), `hist.databento.com` (only when `source="databento"`), + the
Google OAuth hosts (token/JWKS when `MCP_AUTH_ENABLED=1`). Yahoo hosts remain for the
still-installed `openbb-yfinance`. OpenBB does no network at import. Find misses via
`TCP_DENIED` in `/var/log/squid/access.log`.
