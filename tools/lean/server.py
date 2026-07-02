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
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from fastmcp import FastMCP

# Make the repo root importable regardless of CWD, then load the shared serve()
# helper (applies OAuth + optional guardrail/approval, then runs).
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from security.serve import serve  # noqa: E402

# Engine locations inside the quantconnect/lean image; env-overridable for other
# substrates (e.g. a host checkout during development).
LAUNCHER_DIR = Path(os.getenv("LEAN_LAUNCHER_DIR", "/Lean/Launcher/bin/Debug"))
DATA_FOLDER = os.getenv("LEAN_DATA_FOLDER", "/Lean/Data")
BACKTESTS = Path(os.getenv("LEAN_BACKTESTS_DIR", "/app/state/backtests"))
MAX_RUN_SECONDS = int(os.getenv("LEAN_MAX_RUN_SECONDS", "1800"))
LOG_TAIL_LINES = 60

mcp = FastMCP(name="lean")

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


@mcp.tool
def backtest(
    code: str, name: str = "", project: str = "", timeout_seconds: int = 600
) -> dict:
    """Run a Lean backtest of a Python QCAlgorithm and return its statistics.

    ``code`` is a complete algorithm module defining exactly one
    ``class <Name>(QCAlgorithm)`` (start with ``from AlgorithmImports import *``;
    set start/end dates, cash, and universe inside ``initialize``). ``project``
    groups related runs (e.g. iterations of one strategy) into one folder, like
    lean-cli's <project>/backtests/<timestamp> layout. Data available: the Lean
    sample set (e.g. SPY equity minute/daily). Runs synchronously -- typically
    tens of seconds. On failure the engine log tail comes back so the algorithm
    can be fixed and resubmitted.
    """
    match = _CLASS_RE.search(code)
    if not match:
        return {
            "status": "invalid",
            "error": "No `class <Name>(QCAlgorithm)` found in code; submit a complete "
            "algorithm module (subclassing QCAlgorithm directly).",
        }
    class_name = match.group(1)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
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
    env = {**os.environ, "PATH": os.pathsep.join(
        p for p in os.environ["PATH"].split(os.pathsep)
        if p != str(Path(sys.prefix) / "bin")
    )}
    console = job / "console.log"
    try:
        with console.open("w") as out:
            proc = subprocess.run(
                ["dotnet", str(LAUNCHER_DIR / "QuantConnect.Lean.Launcher.dll"),
                 "--config", str(job / "config.json")],
                cwd=job, env=env, stdout=out, stderr=subprocess.STDOUT, timeout=timeout,
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


@mcp.tool
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


@mcp.tool
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


def main() -> None:
    BACKTESTS.mkdir(parents=True, exist_ok=True)
    port = int(os.getenv("MCP_PORT", "8064"))
    # Trusted internal tool: it runs agent-authored code against local data and
    # returns engine output (no untrusted external content -> no guardrail leg).
    serve(mcp, port=port)


if __name__ == "__main__":
    main()
