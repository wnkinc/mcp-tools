"""Tests for the lean server's invocation path -- no engine, no network.

The engine subprocess is faked at the ``subprocess.run`` seam, which pins the two
regressions this wrapper exists to prevent: the launcher invocation contract
(dotnet + config path) and the venv-stripped PATH (pythonnet resolves the embedded
interpreter from the first ``python`` on PATH; the wrapper's venv would shadow the
engine's miniconda). Everything else (status parsing, run listing, data coverage)
runs against fake run folders.
"""

import importlib.util
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

# Both tools ship a `server.py`; load this one under a unique module name so the
# suites can't shadow each other in a whole-repo pytest run.
_SPEC = importlib.util.spec_from_file_location("lean_server", Path(__file__).with_name("server.py"))
server = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(server)

ALGO = "from AlgorithmImports import *\n\nclass MyAlgo(QCAlgorithm):\n    def initialize(self):\n        pass\n"


@pytest.fixture
def bt_root(tmp_path, monkeypatch):
    root = tmp_path / "backtests"
    monkeypatch.setattr(server, "BACKTESTS", root)
    return root


def _fake_engine(monkeypatch, captured, returncode=0, result=None, log=None):
    """subprocess.run stand-in: records the invocation, then 'writes' engine output."""

    def run(cmd, *, cwd, env, stdout, stderr, timeout):
        captured.update(cmd=cmd, cwd=cwd, env=env, timeout=timeout)
        job = Path(cwd)
        if log is not None:
            (job / "log.txt").write_text(log)
        if result is not None:
            bid = json.loads((job / "config.json").read_text())["algorithm-id"]
            (job / f"{bid}.json").write_text(json.dumps(result))
        return SimpleNamespace(returncode=returncode)

    monkeypatch.setattr(server.subprocess, "run", run)


# --- backtest: the invocation contract --------------------------------------------


def test_rejects_code_without_a_qcalgorithm_class(bt_root):
    out = server.backtest(code="print('not an algorithm')")
    assert out["status"] == "invalid"
    assert not bt_root.exists()  # rejected before any run folder is created


def test_runs_launcher_and_reports_statistics(bt_root, monkeypatch):
    captured: dict = {}
    _fake_engine(
        monkeypatch,
        captured,
        result={
            "statistics": {"Sharpe Ratio": "1.2"},
            "runtimeStatistics": {},
            "orders": {"1": {}},
        },
    )
    out = server.backtest(code=ALGO, name="My Strat!", project="Proj X")

    assert out["status"] == "completed"
    assert out["statistics"] == {"Sharpe Ratio": "1.2"}
    assert out["orders"] == 1
    assert out["id"].endswith("-my-strat")  # slugged name, timestamp prefix

    assert captured["cmd"][0] == "dotnet"
    assert captured["cmd"][1].endswith("QuantConnect.Lean.Launcher.dll")
    config = json.loads((Path(captured["cwd"]) / "config.json").read_text())
    assert captured["cmd"][3] == str(Path(captured["cwd"]) / "config.json")
    assert config["algorithm-type-name"] == "MyAlgo"
    # Shelved under the slugged project, engine cwd = the job folder.
    assert Path(captured["cwd"]).parent == bt_root / "proj-x"


def test_engine_path_has_wrapper_venv_stripped(bt_root, monkeypatch):
    captured: dict = {}
    _fake_engine(monkeypatch, captured, result={"statistics": {}})
    server.backtest(code=ALGO)
    engine_path = captured["env"]["PATH"].split(os.pathsep)
    assert str(Path(sys.prefix) / "bin") not in engine_path


def test_timeout_is_reported_and_clamped(bt_root, monkeypatch):
    def run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="dotnet", timeout=kwargs["timeout"])

    monkeypatch.setattr(server.subprocess, "run", run)
    out = server.backtest(code=ALGO, timeout_seconds=1)  # below the floor
    assert out["status"] == "timeout"
    assert "30s" in out["error"]  # clamped up to the 30s minimum


def test_failure_surfaces_engine_log_tail(bt_root, monkeypatch):
    _fake_engine(monkeypatch, {}, returncode=1, log="Loading...\nAlgorithmError: boom")
    out = server.backtest(code=ALGO)
    assert out["status"] == "failed"
    assert out["exit_code"] == 1
    assert "AlgorithmError: boom" in out["log_tail"]


# --- results + listing ------------------------------------------------------------


def _shelve_run(root: Path, project: str, bid: str, state: dict | None = None, **extra) -> Path:
    job = root / project / bid
    job.mkdir(parents=True)
    if state is not None:
        (job / f"{bid}.json").write_text(json.dumps({"state": state, **extra}))
        (job / f"{bid}-summary.json").write_text(json.dumps({"state": state}))
    return job


def test_backtest_result_not_found(bt_root):
    out = server.backtest_result(backtest_id="20260101-000000-nope")
    assert out["status"] == "not_found"


def test_backtest_result_reports_engine_verdict_and_error(bt_root):
    _shelve_run(
        bt_root,
        "default",
        "20260101-000000-bad",
        state={"Status": "RuntimeError", "RuntimeError": "division by zero"},
    )
    out = server.backtest_result(backtest_id="20260101-000000-bad")
    assert out["status"] == "RuntimeError"
    assert out["runtime_error"] == "division by zero"


def test_list_backtests_newest_first_and_project_filter(bt_root):
    _shelve_run(bt_root, "proj-a", "20260101-000000-old", state={"Status": "Completed"})
    _shelve_run(bt_root, "proj-b", "20260301-000000-new", state={"Status": "Completed"})
    _shelve_run(bt_root, "proj-a", "20260201-000000-mid")  # no summary yet

    runs = server.list_backtests()
    assert [r["id"][:8] for r in runs] == ["20260301", "20260201", "20260101"]
    assert runs[1]["status"] == "no_result"

    only_a = server.list_backtests(project="Proj A")  # slugs like backtest() does
    assert {r["project"] for r in only_a} == {"proj-a"}


def test_engine_status_tolerates_junk(tmp_path):
    job = tmp_path / "20260101-000000-x"
    job.mkdir()
    assert server._engine_status(job) == "no_result"
    (job / f"{job.name}-summary.json").write_text("not json{")
    assert server._engine_status(job) == "unknown"


# --- available_data: coverage derived from the Lean file layout --------------------


def _daily_zip(path: Path, rows: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(path.stem + ".csv", "\n".join(rows))


def test_available_data_reads_coverage(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "DATA_FOLDER", str(tmp_path))
    # Engine metadata dirs must not be reported as assets.
    (tmp_path / "market-hours").mkdir()
    _daily_zip(
        tmp_path / "crypto" / "coinbase" / "daily" / "btcusd_trade.zip",
        ["20240101 00:00,1,2,3,4,5", "20240315 00:00,1,2,3,4,5"],
    )
    minute = tmp_path / "crypto" / "coinbase" / "minute" / "btcusd"
    for day in ("20240110", "20240111"):
        _daily_zip(minute / f"{day}_trade.zip", ["0,1,2,3,4,5"])

    out = server.available_data()
    assert {
        "asset": "crypto",
        "market": "coinbase",
        "resolution": "daily",
        "symbol": "BTCUSD",
        "start": "20240101",
        "end": "20240315",
        "bars": 2,
    } in out
    assert {
        "asset": "crypto",
        "market": "coinbase",
        "resolution": "minute",
        "symbol": "BTCUSD",
        "start": "20240110",
        "end": "20240111",
    } in out
