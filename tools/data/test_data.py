"""Tests for the persistence (lake) and OpenBB-fetch (feeds) layers.

``lake`` is exercised against an in-process tmp lake (no network). ``feeds`` is tested
with a fake ``obb`` so the right OpenBB endpoint + args are asserted without hitting
Yahoo Finance.
"""

from types import SimpleNamespace

import pandas as pd
import pytest

import feeds
import lake


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


# ── lake: generic parquet persistence ───────────────────────────────────────


def test_path_for_layout(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    assert lake.path_for("equity", "yfinance", "AAPL", "1d") == (
        tmp_path / "equity" / "yfinance" / "AAPL" / "1d.parquet"
    )


def test_path_for_safe_symbol(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    # '/' is the one genuinely problematic char on Linux (e.g. "BTC/USD").
    assert "BTC_USD" in str(lake.path_for("crypto", "yfinance", "BTC/USD", "1d"))


def test_ingest_persists_and_read_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    s = lake.ingest(("equity", "yfinance", "AAPL", "1d"), _frame(["2024-01-02", "2024-01-03"], [1.5, 2.0]))
    assert s["key"] == "equity/yfinance/AAPL/1d"
    assert s["rows"] == 2 and s["fetched"] == 2 and s["added"] == 2

    back = lake.read("equity", "yfinance", "AAPL", "1d")
    assert list(back["close"]) == [1.5, 2.0]
    assert back.index.name == "date"


def test_ingest_merges_dedupes_and_appends(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    key = ("equity", "yfinance", "AAPL", "1d")

    lake.ingest(key, _frame(["2024-01-01", "2024-01-02"], [1.0, 2.0]))
    # Re-fetch overlaps 01-02 (corrected close) and adds 01-03.
    s = lake.ingest(key, _frame(["2024-01-02", "2024-01-03"], [22.0, 3.0]))

    assert s["fetched"] == 2 and s["added"] == 1 and s["rows"] == 3  # only 01-03 is net-new
    back = lake.read(*key)
    assert list(back.index.strftime("%Y-%m-%d")) == ["2024-01-01", "2024-01-02", "2024-01-03"]
    assert back.loc["2024-01-02", "close"] == 22.0  # fetched row wins the dedupe


def test_refresh_replaces_stored_file(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    key = ("crypto", "yfinance", "BTC-USD", "1d")

    lake.ingest(key, _frame(["2024-01-01", "2024-01-02"], [1.0, 2.0]))
    s = lake.ingest(key, _frame(["2024-06-01"], [9.0]), refresh=True)

    assert s["rows"] == 1 and s["added"] == 1  # old rows gone, not merged
    assert list(lake.read(*key).index.strftime("%Y-%m-%d")) == ["2024-06-01"]


def test_ingest_empty_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    with pytest.raises(ValueError):
        lake.ingest(("equity", "yfinance", "AAPL", "1d"), _frame([], []))


def test_read_missing_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    assert lake.read("equity", "yfinance", "NOPE", "1d") is None


# ── feeds: OpenBB endpoint wrappers (fake obb, no network) ───────────────────


def _fake_obb(caps, df):
    """A stand-in ``obb`` whose <namespace>.price.historical records its kwargs into caps[ns]."""
    def endpoint(cap):
        def historical(**kwargs):
            cap.update(kwargs)
            return SimpleNamespace(to_df=lambda: df)
        return SimpleNamespace(price=SimpleNamespace(historical=historical))
    return SimpleNamespace(**{ns: endpoint(cap) for ns, cap in caps.items()})


def _caps():
    return {"equity": {}, "crypto": {}, "currency": {}}


def test_equity_bars_calls_equity_endpoint(monkeypatch):
    caps = _caps()
    df = _frame(["2024-01-02"], [1.0])
    monkeypatch.setattr(feeds, "_obb", lambda: _fake_obb(caps, df))

    out = feeds.equity_bars("AAPL", "1d", "2024-01-01", "2024-01-10")
    assert out is df
    assert caps["crypto"] == {} and caps["currency"] == {}  # only equity touched
    assert caps["equity"] == {
        "symbol": "AAPL", "interval": "1d", "start_date": "2024-01-01",
        "end_date": "2024-01-10", "provider": "tiingo",  # the fixed default provider
    }


def test_crypto_bars_calls_crypto_endpoint(monkeypatch):
    caps = _caps()
    monkeypatch.setattr(feeds, "_obb", lambda: _fake_obb(caps, _frame(["2024-01-02"], [1.0])))

    feeds.crypto_bars("BTC-USD", provider="yfinance")
    assert caps["equity"] == {} and caps["currency"] == {}
    assert caps["crypto"]["symbol"] == "BTC-USD" and caps["crypto"]["provider"] == "yfinance"


def test_fx_bars_calls_currency_endpoint(monkeypatch):
    caps = _caps()
    monkeypatch.setattr(feeds, "_obb", lambda: _fake_obb(caps, _frame(["2024-01-02"], [1.0])))

    feeds.fx_bars("EURUSD", "1d", "2024-01-01", "2024-01-10")
    assert caps["equity"] == {} and caps["crypto"] == {}
    assert caps["currency"]["symbol"] == "EURUSD" and caps["currency"]["provider"] == "tiingo"


def test_provider_passthrough(monkeypatch):
    caps = _caps()
    monkeypatch.setattr(feeds, "_obb", lambda: _fake_obb(caps, _frame(["2024-01-02"], [1.0])))
    feeds.equity_bars("AAPL", provider="tiingo")  # source flows straight through to OpenBB
    assert caps["equity"]["provider"] == "tiingo"


# ── feeds: provider credential injection (no OpenBB import) ───────────────────


def test_apply_credentials_injects_token_from_env(monkeypatch):
    monkeypatch.setenv("TIINGO_API_KEY", "tok-123")
    creds = SimpleNamespace(tiingo_token=None)
    feeds._apply_credentials(SimpleNamespace(user=SimpleNamespace(credentials=creds)))
    assert creds.tiingo_token == "tok-123"


def test_apply_credentials_noop_without_env(monkeypatch):
    monkeypatch.delenv("TIINGO_API_KEY", raising=False)
    creds = SimpleNamespace(tiingo_token=None)
    feeds._apply_credentials(SimpleNamespace(user=SimpleNamespace(credentials=creds)))
    assert creds.tiingo_token is None
