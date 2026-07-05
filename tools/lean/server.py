"""lean -- QuantConnect Lean backtesting over MCP.

The engine has ONE operation: run an algorithm file against a config and emit a
folder of result files. The agent writes the QCAlgorithm Python class itself (the
whole Lean API surface lives in that code, not in tools here), so this server only
owns the invocation path: write algorithm -> write config -> run the launcher as a
subprocess -> read the results JSON back. No lean-cli: it runs backtests by
launching Docker containers, which a walled container must not do.

Runs INSIDE the pinned quantconnect/lean image (see Dockerfile), so the engine,
its miniconda Python, and the sample data under /Lean/Data are all present. Each
run gets a folder at BACKTESTS/<project>/<id>/ (lean-cli's
<project>/backtests/<timestamp>/ convention, rooted in the tool's state volume):
the algorithm, the config, the engine log, and the results JSON.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from fastmcp import FastMCP
from mcp.types import ToolAnnotations

# Make the repo root importable regardless of CWD, then load the shared serve()
# helper (applies OAuth + optional guardrail/approval, then runs).
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from security.serve import serve  # noqa: E402

# Engine locations inside the quantconnect/lean image; env-overridable for other
# substrates (e.g. a host checkout during development).
LAUNCHER_DIR = Path(os.getenv("LEAN_LAUNCHER_DIR", "/Lean/Launcher/bin/Debug"))
# The deploy points this at the shared lean-data volume (pipeline data ONLY --
# the data tool's lean-export writes it); default = the image's bundled samples.
DATA_FOLDER = os.getenv("LEAN_DATA_FOLDER", "/Lean/Data")
ENGINE_DATA = Path("/Lean/Data")
# Engine metadata (exchange hours, tick/lot sizes) the engine requires inside its
# data folder; seeded from the image at startup. NOT price data.
_ENGINE_METADATA = (
    "market-hours/market-hours-database.json",
    "symbol-properties/symbol-properties-database.csv",
)
BACKTESTS = Path(os.getenv("LEAN_BACKTESTS_DIR", "/app/state/backtests"))
MAX_RUN_SECONDS = int(os.getenv("LEAN_MAX_RUN_SECONDS", "1800"))
LOG_TAIL_LINES = 60

# Server-level instructions land in the client's system prompt: the cross-tool
# workflow lives here, per-tool contracts stay in each docstring.
INSTRUCTIONS = (
    "Backtests only find data this server's pipeline has exported — check "
    "available_data before writing an algorithm and subscribe exactly as listed. "
    "A missing series is added with the data tool (crypto-ingest, then lean-export)."
)

mcp = FastMCP(name="lean", instructions=INSTRUCTIONS)

_CLASS_RE = re.compile(r"^class\s+(\w+)\s*\([^)]*QCAlgorithm[^)]*\)", re.MULTILINE)
_SLUG_RE = re.compile(r"[^a-zA-Z0-9-]+")


def _build_config(job: Path, backtest_id: str, class_name: str) -> dict:
    """The launcher's backtesting config: Lean's shipped Launcher/config.json
    defaults (handler names verbatim) + this run's algorithm and output paths.

    Paths are absolute because the subprocess runs with cwd=job (the engine drops
    log.txt in cwd, and job is the writable place); composer-dll-directory then
    points back at the launcher bin so its plugin assemblies still resolve.
    """
    return {
        "environment": "backtesting",
        "algorithm-type-name": class_name,
        "algorithm-language": "Python",
        "algorithm-location": str(job / "main.py"),
        "algorithm-id": backtest_id,  # names the results file: <job>/<id>.json
        "composer-dll-directory": str(LAUNCHER_DIR),
        "data-folder": DATA_FOLDER,
        "results-destination-folder": str(job),
        "object-store-root": str(job / "storage"),
        # AlgorithmImports (and friends) live in the launcher bin; with cwd=job they
        # must be put on the embedded interpreter's path explicitly.
        "python-additional-paths": [str(LAUNCHER_DIR)],
        "debugging": False,
        "show-missing-data-logs": True,
        "log-handler": "QuantConnect.Logging.CompositeLogHandler",
        "messaging-handler": "QuantConnect.Messaging.Messaging",
        "job-queue-handler": "QuantConnect.Queues.JobQueue",
        "api-handler": "QuantConnect.Api.Api",
        "map-file-provider": "QuantConnect.Data.Auxiliary.LocalDiskMapFileProvider",
        "factor-file-provider": "QuantConnect.Data.Auxiliary.LocalDiskFactorFileProvider",
        "data-provider": "QuantConnect.Lean.Engine.DataFeeds.DefaultDataProvider",
        "data-channel-provider": "DataChannelProvider",
        "object-store": "QuantConnect.Lean.Engine.Storage.LocalObjectStore",
        "data-aggregator": "QuantConnect.Lean.Engine.DataFeeds.AggregationManager",
        "environments": {
            "backtesting": {
                "live-mode": False,
                "setup-handler": "QuantConnect.Lean.Engine.Setup.BacktestingSetupHandler",
                "result-handler": "QuantConnect.Lean.Engine.Results.BacktestingResultHandler",
                "data-feed-handler": "QuantConnect.Lean.Engine.DataFeeds.FileSystemDataFeed",
                "real-time-handler": "QuantConnect.Lean.Engine.RealTime.BacktestingRealTimeHandler",
                "history-provider": [
                    "QuantConnect.Lean.Engine.HistoricalData.SubscriptionDataReaderHistoryProvider"
                ],
                "transaction-handler": "QuantConnect.Lean.Engine.TransactionHandlers.BacktestingTransactionHandler",
            }
        },
    }


def _tail(path: Path, lines: int = LOG_TAIL_LINES) -> str:
    if not path.exists():
        return ""
    return "\n".join(path.read_text(errors="replace").splitlines()[-lines:])


# readOnlyHint groups Claude's permission categories (read-only vs write/delete),
# same mechanism as xmcp's build_annotations. backtest is the one write: each run
# creates a fresh folder (never overwrites), entirely local to the engine.
@mcp.tool(
    annotations=ToolAnnotations(
        title="Run Lean backtest",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=False,
    )
)
def backtest(code: str, name: str = "", project: str = "", timeout_seconds: int = 600) -> dict:
    """Run a Lean backtest of a Python QCAlgorithm and return its statistics.

    ``code`` is a complete algorithm module defining exactly one
    ``class <Name>(QCAlgorithm)`` (start with ``from AlgorithmImports import *``;
    set start/end dates, cash, and universe inside ``initialize``). ``project``
    groups related runs (e.g. iterations of one strategy) into one folder, like
    lean-cli's <project>/backtests/<timestamp> layout.

    DATA: call available_data() FIRST -- this server backtests only the data its
    pipeline has exported (nothing else exists), and dates outside its coverage
    find no bars. Subscribe exactly as listed, e.g. crypto:
    self.add_crypto("BTCUSD", Resolution.DAILY, Market.COINBASE). Missing a
    series? The data tool's crypto-ingest + lean-export add it.

    Runs synchronously -- typically tens of seconds. On failure the engine log
    tail comes back so the algorithm can be fixed and resubmitted.
    """
    match = _CLASS_RE.search(code)
    if not match:
        return {
            "status": "invalid",
            "error": "No `class <Name>(QCAlgorithm)` found in code; submit a complete "
            "algorithm module (subclassing QCAlgorithm directly).",
        }
    class_name = match.group(1)

    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    slug = _SLUG_RE.sub("-", name or class_name).strip("-").lower() or "backtest"
    backtest_id = f"{stamp}-{slug}"
    # The project is a shelving convention only (the engine never sees it); the id
    # stays the single retrieval handle. Always two levels, so "" gets a bucket.
    project_slug = _SLUG_RE.sub("-", project).strip("-").lower() or "default"
    job = BACKTESTS / project_slug / backtest_id
    job.mkdir(parents=True)

    (job / "main.py").write_text(code)
    config = _build_config(job, backtest_id, class_name)
    (job / "config.json").write_text(json.dumps(config, indent=2))

    timeout = max(30, min(timeout_seconds, MAX_RUN_SECONDS))
    # The engine must NOT see this wrapper's venv on PATH: pythonnet resolves the
    # embedded interpreter's prefix from the first `python` found there, and the
    # venv's empty site-packages then shadows the engine's miniconda (pandas etc.).
    env = {
        **os.environ,
        "PATH": os.pathsep.join(
            p for p in os.environ["PATH"].split(os.pathsep) if p != str(Path(sys.prefix) / "bin")
        ),
    }
    console = job / "console.log"
    try:
        with console.open("w") as out:
            proc = subprocess.run(
                [
                    "dotnet",
                    str(LAUNCHER_DIR / "QuantConnect.Lean.Launcher.dll"),
                    "--config",
                    str(job / "config.json"),
                ],
                cwd=job,
                env=env,
                stdout=out,
                stderr=subprocess.STDOUT,
                timeout=timeout,
            )
    except subprocess.TimeoutExpired:
        return {
            "status": "timeout",
            "id": backtest_id,
            "error": f"Engine exceeded {timeout}s; narrow the date range or universe.",
            "log_tail": _tail(console),
        }

    result_file = job / f"{backtest_id}.json"
    if proc.returncode != 0 or not result_file.exists():
        return {
            "status": "failed",
            "id": backtest_id,
            "exit_code": proc.returncode,
            # Algorithm errors (syntax, runtime, missing data) land in the engine log.
            "log_tail": _tail(job / "log.txt") or _tail(console),
        }

    data = json.loads(result_file.read_text())
    return {
        "status": "completed",
        "id": backtest_id,
        "statistics": data.get("statistics", {}),
        "runtime_statistics": data.get("runtimeStatistics", {}),
        "orders": len(data.get("orders", {})),
    }


@mcp.tool(
    annotations=ToolAnnotations(title="Backtest result", readOnlyHint=True, openWorldHint=False)
)
def backtest_result(backtest_id: str) -> dict:
    """Full results of a past backtest: statistics, trade/portfolio breakdowns,
    and order events. Run folders under the state volume persist across restarts."""
    # Ids embed a timestamp, so they're unique across projects: find the run
    # whatever project it was shelved under.
    job = next(BACKTESTS.glob(f"*/{backtest_id}"), None)
    if job is None or not (job / f"{backtest_id}.json").exists():
        return {
            "status": "not_found",
            "id": backtest_id,
            "error": "No results for this id (unknown, failed, or timed out).",
            "log_tail": _tail(job / "log.txt") if job else "",
        }
    result_file = job / f"{backtest_id}.json"

    data = json.loads(result_file.read_text())
    total = data.get("totalPerformance") or {}
    state = data.get("state", {})
    out = {
        # The engine's own verdict (Completed / RuntimeError / ...): it writes
        # results JSONs even for failed runs.
        "status": state.get("Status", "unknown"),
        "id": backtest_id,
        "statistics": data.get("statistics", {}),
        "runtime_statistics": data.get("runtimeStatistics", {}),
        "trade_statistics": total.get("tradeStatistics", {}),
        "portfolio_statistics": total.get("portfolioStatistics", {}),
        "orders": list(data.get("orders", {}).values()),
    }
    if state.get("RuntimeError"):
        out["runtime_error"] = state["RuntimeError"]
    return out


def _engine_status(job: Path) -> str:
    """The engine's own verdict from the small <id>-summary.json it writes per run
    (state.Status: Completed / RuntimeError / ...); it writes results JSONs even
    for failed runs, so file existence alone can't tell success from failure."""
    summary = job / f"{job.name}-summary.json"
    if not summary.exists():
        return "no_result"
    try:
        return json.loads(summary.read_text()).get("state", {}).get("Status", "unknown")
    except (OSError, json.JSONDecodeError):
        return "unknown"


@mcp.tool(
    annotations=ToolAnnotations(title="List backtests", readOnlyHint=True, openWorldHint=False)
)
def list_backtests(project: str = "") -> list[dict]:
    """Past backtests (newest first), optionally one project's only. Everything is
    derived from the run folders; the id embeds the run's timestamp and name."""
    pattern = _SLUG_RE.sub("-", project).strip("-").lower() or "*"
    runs = [
        {"id": job.name, "project": job.parent.name, "status": _engine_status(job)}
        for job in BACKTESTS.glob(f"{pattern}/*")
        if job.is_dir()
    ]
    return sorted(runs, key=lambda run: run["id"], reverse=True)


def _zip_coverage(zp: Path) -> tuple[str | None, str | None, int]:
    """(first bar date, last bar date, bar count) of a daily/hour Lean zip."""
    try:
        with zipfile.ZipFile(zp) as z:
            lines = z.read(z.namelist()[0]).decode(errors="replace").splitlines()
        first, last = lines[0].split(",", 1)[0][:8], lines[-1].split(",", 1)[0][:8]
        return first, last, len(lines)
    except (OSError, zipfile.BadZipFile, IndexError):
        return None, None, 0


@mcp.tool(
    annotations=ToolAnnotations(
        title="Available backtest data", readOnlyHint=True, openWorldHint=False
    )
)
def available_data() -> list[dict]:
    """The market data on this server, one entry per symbol/market/resolution with
    its date coverage (yyyymmdd). Backtests only work inside this coverage -- check
    it before writing an algorithm. Empty list = nothing exported yet: ingest and
    export a series via the data tool (crypto-ingest, then lean-export)."""
    root = Path(DATA_FOLDER)
    out: list[dict] = []
    asset_dirs = [
        p
        for p in (sorted(root.iterdir()) if root.exists() else [])
        if p.is_dir() and p.name not in ("market-hours", "symbol-properties")
    ]
    for asset in asset_dirs:
        for market in sorted(p for p in asset.iterdir() if p.is_dir()):
            for res in sorted(p for p in market.iterdir() if p.is_dir()):
                base = {"asset": asset.name, "market": market.name, "resolution": res.name}
                if res.name == "minute":
                    for sym in sorted(p for p in res.iterdir() if p.is_dir()):
                        days = sorted(f.name[:8] for f in sym.glob("*_trade.zip"))
                        if days:
                            out.append(
                                {
                                    **base,
                                    "symbol": sym.name.upper(),
                                    "start": days[0],
                                    "end": days[-1],
                                }
                            )
                else:
                    for zp in sorted(res.glob("*.zip")):
                        if zp.stem.endswith("_quote"):
                            continue
                        start, end, bars = _zip_coverage(zp)
                        out.append(
                            {
                                **base,
                                "symbol": zp.stem.removesuffix("_trade").upper(),
                                "start": start,
                                "end": end,
                                "bars": bars,
                            }
                        )
    return out


def _seed_engine_metadata() -> None:
    """Copy the engine's two metadata databases into the data folder if absent.

    The engine refuses to start without them, and the shared volume starts empty
    on purpose (no bundled price data). Version-matched to the engine by
    construction: they come from this same image.
    """
    if Path(DATA_FOLDER) == ENGINE_DATA:
        return
    for rel in _ENGINE_METADATA:
        dest = Path(DATA_FOLDER) / rel
        if not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ENGINE_DATA / rel, dest)


def main() -> None:
    _seed_engine_metadata()
    BACKTESTS.mkdir(parents=True, exist_ok=True)
    port = int(os.getenv("MCP_PORT", "8064"))
    # Trusted internal tool: it runs agent-authored code against local data and
    # returns engine output (no untrusted external content -> no guardrail leg).
    serve(mcp, port=port)


if __name__ == "__main__":
    main()
