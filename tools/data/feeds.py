"""Thin wrappers over OpenBB endpoints — the only layer that knows about OpenBB.

One function per capability, each calling a single OpenBB endpoint and returning its
standardized DataFrame **as-is** (OpenBB's own schema + ``date`` index). No persistence,
no MCP — just the fetch. Adding a capability = add a function here (+ a tool in
``server.py``); the persistence layer (``lake.py``) is untouched.

Each *data type* is a command extension (``openbb-equity``, ``openbb-crypto``); each
*data source* is a provider extension (``openbb-yfinance``, ``openbb-tiingo``). Because
OpenBB standardizes across providers, a new provider for an existing data type is nearly
free — it's just another ``provider=`` value on the same feed fn, no new code. Re-run the
accessor prebuild after adding any extension.

Keyed providers (e.g. tiingo) need a token. OpenBB does NOT read credential env vars, so
we inject them from the env (``.env``) onto ``obb.user.credentials`` — see
``_CREDENTIALS`` / ``_apply_credentials``. yfinance needs no key.
"""
from __future__ import annotations

import os

import pandas as pd

# Tiingo is the fixed provider for equity + crypto (deeper intraday history than yfinance,
# same daily) — the tools don't expose a provider choice. yfinance stays installed and
# reachable in code via provider="yfinance", but nothing uses it by default.
DEFAULT_PROVIDER = "tiingo"

# env var -> the obb.user.credentials attribute it populates. Add a keyed provider here.
_CREDENTIALS = {"TIINGO_API_KEY": "tiingo_token"}


def _apply_credentials(obb) -> None:
    """Inject provider tokens from the env onto OpenBB's credential store (idempotent)."""
    creds = obb.user.credentials
    for env_var, attr in _CREDENTIALS.items():
        token = os.getenv(env_var)
        if token and getattr(creds, attr, None) != token:
            setattr(creds, attr, token)


def _obb():
    from openbb import obb

    _apply_credentials(obb)
    return obb


def equity_bars(
    symbol: str, interval: str = "1d", start: str | None = None,
    end: str | None = None, provider: str = DEFAULT_PROVIDER,
) -> pd.DataFrame:
    """Historical OHLCV bars for an equity symbol (e.g. AAPL)."""
    return _obb().equity.price.historical(
        symbol=symbol, interval=interval, start_date=start, end_date=end, provider=provider
    ).to_df()


def crypto_bars(
    symbol: str, interval: str = "1d", start: str | None = None,
    end: str | None = None, provider: str = DEFAULT_PROVIDER,
) -> pd.DataFrame:
    """Historical OHLCV bars for a crypto pair (e.g. BTC-USD)."""
    return _obb().crypto.price.historical(
        symbol=symbol, interval=interval, start_date=start, end_date=end, provider=provider
    ).to_df()


def fx_bars(
    symbol: str, interval: str = "1d", start: str | None = None,
    end: str | None = None, provider: str = DEFAULT_PROVIDER,
) -> pd.DataFrame:
    """Historical OHLC bars for an FX pair (e.g. EURUSD). FX frames carry no volume."""
    return _obb().currency.price.historical(
        symbol=symbol, interval=interval, start_date=start, end_date=end, provider=provider
    ).to_df()
