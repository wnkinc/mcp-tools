"""Tests for the bars persistence layer.

``bars._fetch`` (the only networked call) is monkeypatched so the parquet
merge/dedupe/append logic is exercised against an in-process tmp lake without
hitting Yahoo Finance.
"""

import pandas as pd
import pytest

import bars


def _frame(dates, close):
    """An OpenBB-shaped OHLCV frame: a ``date``-indexed DataFrame (persisted as-is)."""
    idx = pd.DatetimeIndex(pd.to_datetime(dates), name="date")
    return pd.DataFrame(
        {
            "open": close,
            "high": close,
            "low": close,
            "close": close,
            "volume": [100] * len(close),
        },
        index=idx,
    )


def _stub_fetch(monkeypatch, frame):
    monkeypatch.setattr(bars, "_fetch", lambda symbol, interval, start, end, source: frame)


def test_path_for_layout(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    p = bars.path_for("yfinance", "AAPL", "1d")
    assert p == tmp_path / "bars" / "yfinance" / "AAPL" / "1d.parquet"


def test_path_for_safe_symbol(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    # '/' is the one genuinely problematic char on Linux (e.g. "BTC/USD").
    assert bars.path_for("yfinance", "BTC/USD", "1d").name == "1d.parquet"
    assert "BTC_USD" in str(bars.path_for("yfinance", "BTC/USD", "1d"))


def test_ingest_persists_and_read_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    _stub_fetch(monkeypatch, _frame(["2024-01-02", "2024-01-03"], [1.5, 2.0]))

    s = bars.ingest("aapl", "1d")
    assert s["symbol"] == "AAPL"  # uppercased
    assert s["rows"] == 2 and s["fetched"] == 2 and s["added"] == 2

    back = bars.read("AAPL", "1d")
    assert list(back["close"]) == [1.5, 2.0]
    assert back.index.name == "date"


def test_ingest_merges_dedupes_and_appends(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))

    _stub_fetch(monkeypatch, _frame(["2024-01-01", "2024-01-02"], [1.0, 2.0]))
    bars.ingest("AAPL", "1d")

    # Re-fetch overlaps 01-02 (corrected close) and adds 01-03.
    _stub_fetch(monkeypatch, _frame(["2024-01-02", "2024-01-03"], [22.0, 3.0]))
    s = bars.ingest("AAPL", "1d")

    assert s["fetched"] == 2 and s["added"] == 1 and s["rows"] == 3  # only 01-03 is net-new

    back = bars.read("AAPL", "1d")
    assert list(back.index.strftime("%Y-%m-%d")) == ["2024-01-01", "2024-01-02", "2024-01-03"]
    assert back.loc["2024-01-02", "close"] == 22.0  # fetched bar wins the dedupe


def test_refresh_replaces_stored_file(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))

    _stub_fetch(monkeypatch, _frame(["2024-01-01", "2024-01-02"], [1.0, 2.0]))
    bars.ingest("AAPL", "1d")

    _stub_fetch(monkeypatch, _frame(["2024-06-01"], [9.0]))
    s = bars.ingest("AAPL", "1d", refresh=True)

    assert s["rows"] == 1 and s["added"] == 1  # old bars gone, not merged
    assert list(bars.read("AAPL", "1d").index.strftime("%Y-%m-%d")) == ["2024-06-01"]


def test_ingest_empty_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    _stub_fetch(monkeypatch, _frame([], []))
    with pytest.raises(ValueError):
        bars.ingest("AAPL", "1d")


def test_ingest_requires_symbol(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    with pytest.raises(ValueError):
        bars.ingest("   ", "1d")


def test_read_missing_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    assert bars.read("NOPE", "1d") is None
