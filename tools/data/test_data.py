"""Tests for the data-ingest tool.

``runs._run_ingest`` (the only networked call) is monkeypatched so the in-process
job-registry lifecycle is exercised without hitting Yahoo Finance.
``pipeline``/``store``/``schema`` are tested against an in-process tmp data lake.
"""

import time

import pandas as pd
import pytest

import pipeline
import runs
import schema
import store


def _wait(run_id, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = runs.status(run_id)
        if st is None or st != runs.RUNNING:
            return st
        time.sleep(0.02)
    raise AssertionError(f"run {run_id} did not finish within {timeout}s")


# ── run-registry lifecycle ──────────────────────────────────────────────────


def test_run_lifecycle_success(monkeypatch):
    summary = {"rows": 5, "symbol": "AAPL", "interval": "1d", "source": "yfinance",
               "start": "s", "end": "e", "path": "/p", "cached": False}
    monkeypatch.setattr(runs, "_run_ingest", lambda *a: summary)
    run_id = runs.start("AAPL", "1d", None, None, "yfinance", False)

    assert _wait(run_id) == runs.SUCCESS
    job = runs.result(run_id)
    assert job.result == summary
    assert job.error is None


def test_run_error_is_captured(monkeypatch):
    def boom(*a):
        raise RuntimeError("yahoo down")

    monkeypatch.setattr(runs, "_run_ingest", boom)
    run_id = runs.start("AAPL", "1d", None, None, "yfinance", False)

    assert _wait(run_id) == runs.ERROR
    assert "yahoo down" in runs.result(run_id).error


def test_args_forwarded(monkeypatch):
    calls = {}
    monkeypatch.setattr(
        runs, "_run_ingest",
        lambda symbol, interval, start, end, source, refresh: calls.update(
            symbol=symbol, interval=interval, start=start, end=end, source=source, refresh=refresh
        ) or {"rows": 1},
    )
    run_id = runs.start("msft", "1h", "2024-01-01", None, "yfinance", True)
    _wait(run_id)
    assert calls == {"symbol": "msft", "interval": "1h", "start": "2024-01-01",
                     "end": None, "source": "yfinance", "refresh": True}


def test_wait_returns_running_on_slow_job(monkeypatch):
    release = {"go": False}

    def slow(*a):
        while not release["go"]:
            time.sleep(0.01)
        return {"rows": 1}

    monkeypatch.setattr(runs, "_run_ingest", slow)
    run_id = runs.start("AAPL", "1d", None, None, "yfinance", False)
    assert runs.wait(run_id, budget_s=0.1) == runs.RUNNING  # still going → PENDING
    release["go"] = True
    assert _wait(run_id) == runs.SUCCESS


def test_status_and_result_unknown_run():
    assert runs.status("nope") is None
    assert runs.result("nope") is None


def test_cancel_unknown_run():
    hard, st = runs.cancel("nope")
    assert hard is False and st is None


# ── schema / store / pipeline (no network) ──────────────────────────────────


def _canonical_raw():
    """A frame shaped like ``openbb_source.fetch`` output: canonical column names,
    naive timestamps (enforce_canonical localizes to UTC)."""
    return pd.DataFrame(
        {
            "timestamp": pd.to_datetime(["2024-01-02", "2024-01-03"]),
            "open": [1.0, 1.5],
            "high": [2.0, 2.5],
            "low": [0.5, 1.0],
            "close": [1.5, 2.0],
            "volume": [100, 200],
        }
    )


def test_enforce_canonical_from_canonical_named():
    df = schema.enforce_canonical(_canonical_raw())
    assert list(df.columns) == list(schema.CANONICAL_COLUMNS)
    assert str(df["timestamp"].dt.tz) == "UTC"
    assert df["open"].dtype == "float64"
    assert len(df) == 2


def test_enforce_canonical_rejects_missing_columns():
    with pytest.raises(ValueError):
        schema.enforce_canonical(pd.DataFrame({"open": [1.0]}))


def test_obb_interval_mapping():
    from sources import openbb_source

    assert openbb_source.obb_interval("1wk") == "1W"  # yfinance weekly, not "1wk"
    assert openbb_source.obb_interval("1mo") == "1M"  # yfinance monthly, not "1mo"
    assert openbb_source.obb_interval("1d") == "1d"
    with pytest.raises(ValueError):
        openbb_source.obb_interval("3y")


def test_store_roundtrip_and_request_window(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))

    df = schema.enforce_canonical(_canonical_raw())
    store.write("bars", "yfinance", "AAPL", "1d", df, req_start="2024-01-01", req_end="2024-02-01")

    back = store.read("bars", "yfinance", "AAPL", "1d")
    assert len(back) == 2
    assert store.request_window("bars", "yfinance", "AAPL", "1d") == ("2024-01-01", "2024-02-01")


def test_store_covers_subset_and_superset(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))

    df = schema.enforce_canonical(_canonical_raw())
    store.write("bars", "yfinance", "AAPL", "1d", df, req_start="2024-01-01", req_end="2024-02-01")

    assert store.covers("bars", "yfinance", "AAPL", "1d", "2024-01-01", "2024-02-01") is True
    assert store.covers("bars", "yfinance", "AAPL", "1d", "2024-01-10", "2024-01-20") is True  # subset
    assert store.covers("bars", "yfinance", "AAPL", "1d", "2023-06-01", "2024-02-01") is False  # wider
    assert store.covers("bars", "yfinance", "AAPL", "1d", None, None) is False  # unbounded req
    assert store.covers("bars", "yfinance", "MSFT", "1d", "2024-01-01", "2024-02-01") is False  # absent


def test_pipeline_cache_hit_skips_fetch(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))

    fetch_calls = {"n": 0}

    def fake_fetch(symbol, interval, start, end):
        fetch_calls["n"] += 1
        return _canonical_raw()

    monkeypatch.setitem(pipeline._SOURCES, "yfinance", fake_fetch)

    r1 = pipeline.ingest("AAPL", "1d", "2024-01-01", "2024-02-01")
    assert r1["cached"] is False and fetch_calls["n"] == 1
    r2 = pipeline.ingest("AAPL", "1d", "2024-01-01", "2024-02-01")
    assert r2["cached"] is True and fetch_calls["n"] == 1  # no second fetch
    r3 = pipeline.ingest("AAPL", "1d", "2024-01-01", "2024-02-01", refresh=True)
    assert r3["cached"] is False and fetch_calls["n"] == 2  # refresh forces fetch


def test_pipeline_unknown_source_raises():
    with pytest.raises(ValueError):
        pipeline.ingest("AAPL", source="nope")
