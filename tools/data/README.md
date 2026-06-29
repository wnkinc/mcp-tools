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
| `data-read` | read stored bars back out of the lake (read-only) |

**`*-ingest` args:** `symbol` (Tiingo-style: crypto/FX are hyphen-less — `BTCUSD`, `EURUSD`),
`interval?="1d"` (`1m/2m/5m/15m/30m/60m/90m/1h/1d/5d/1W/1M/1Q`), `start?`/`end?` (ISO
`YYYY-MM-DD`), `refresh?=false` (replace instead of merge). No `source` — provider is fixed
to Tiingo.
**`data-read` args:** `asset` (`equity`|`crypto`|`fx`), `symbol`, `interval?="1d"`, `tail?=10`.

Deep **intraday** pulls page Tiingo's 10k-bar-per-request cap automatically. On the free tier
a multi-year 1m pull can exhaust the hourly request limit and return a **PARTIAL** result —
just re-run later (keep `refresh=false`) and the lake merge extends coverage.

## Design

Three layers, so the owned surface stays constant as capabilities grow:

| File | Job |
|---|---|
| `server.py` | thin `@mcp.tool` per capability (wires feed → lake → text) |
| `feeds.py` | thin per-capability OpenBB fetch fns; pages Tiingo's intraday cap |
| `lake.py` | generic parquet persist/merge/read, keyed by path segments (never changes per capability) |

OpenBB standardizes fetch + schema across providers, so a **new data type** = a command ext
(`openbb-equity`/`-crypto`/`-currency`) + a `feeds` fn + a tool; a **new source** = just a
provider ext, used via `provider=` (no new code). Keyed providers (Tiingo) need a token —
`feeds._apply_credentials` injects `TIINGO_API_KEY` onto `obb.user.credentials`, since OpenBB
doesn't read credential env vars. `openbb-yfinance` stays installed/reachable in code but no
tool uses it.

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

OpenBB code-gens its `obb` accessor at import; on the read-only hardened unit it must be
prebuilt now and frozen (`OPENBB_AUTO_BUILD=false`). Rerun the prebuild only after
adding/removing an extension. To publish: allowlist hosts in
`security/egress-proxy/allowlist/data.txt`, then `sudo scripts/install-system.sh` +
`scripts/add-tunnel-route.sh data.secure-agentic-engineering.com 8062`.

## Env vars

| Var | Default | Meaning |
|---|---|---|
| `TIINGO_API_KEY` | _(empty)_ | **required** — Tiingo is the fixed provider; empty → ingest fails |
| `MCP_PORT` | `8062` | loopback MCP port |
| `MCP_AUTH_ENABLED` | `0` | `1` = require Google OAuth (public serving) |
| `DATA_ROOT` | `<tool>/var/data` | parquet lake root (StateDirectory on the unit) |
| `OPENBB_AUTO_BUILD` | `false` (unit) | freeze the prebuilt accessor; never rebuild at import |
| `HOME` | StateDirectory (unit) | OpenBB writes `$HOME/.openbb_platform`; must be writable |

## Egress

Behind the L2 egress wall, allowed hosts live in `security/egress-proxy/allowlist/data.txt`:
`api.tiingo.com` (data) + the Google OAuth hosts (token/JWKS when `MCP_AUTH_ENABLED=1`). Yahoo
hosts remain for the still-installed `openbb-yfinance`. OpenBB does no network at import. Find
misses via `TCP_DENIED` in `/var/log/squid/access.log`.
