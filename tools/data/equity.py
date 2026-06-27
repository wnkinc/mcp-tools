"""Live equity tools backed by OpenBB (yfinance provider).

Direction B widens the tool past bars. Unlike bars — which are ingested into the parquet
lake and served from cache — these are **read-through**: each call hits OpenBB live and
returns formatted text. They all return *structured* market data (numbers, identifiers,
ratios), so they are trusted output and need no guardrail.

Adding a capability is a thin wrapper here plus an ``@mcp.tool`` in ``server.py``; adding
a data source is an extra pinned OpenBB provider extension (no code).
"""
from __future__ import annotations

import pandas as pd

DEFAULT_PROVIDER = "yfinance"

# equity.fundamental.<name>; the three statements take a ``limit`` (number of periods).
_STATEMENTS: dict[str, bool] = {
    "income": True,
    "balance": True,
    "cash": True,
    "metrics": False,
    "dividends": False,
}
# equity.discovery.<name> screens.
_DISCOVERY: frozenset[str] = frozenset(
    {
        "gainers",
        "losers",
        "active",
        "growth_tech",
        "aggressive_small_caps",
        "undervalued_growth",
        "undervalued_large_caps",
    }
)


def _obb():
    from openbb import obb

    return obb


def _render(obbject, *, max_rows: int | None = 20) -> str:
    """Render an OBBject's results as text.

    A single-row result (quote/profile/metrics) is shown as ``key: value`` lines;
    multi-row results as a (tail-limited) table.
    """
    df: pd.DataFrame = obbject.to_df()
    if df is None or len(df) == 0:
        return "(no data)"
    if len(df) == 1:
        row = df.reset_index().iloc[0]
        return "\n".join(
            f"{k}: {v}"
            for k, v in row.items()
            if str(v) not in ("", "nan", "NaN", "None", "NaT")
        )
    view = df.tail(max_rows) if max_rows else df
    return view.to_string()


def quote(symbol: str, provider: str = DEFAULT_PROVIDER) -> str:
    """Latest quote (price, bid/ask, day range, market cap, …)."""
    return _render(_obb().equity.price.quote(symbol=symbol, provider=provider))


def fundamentals(
    symbol: str, statement: str = "income", limit: int = 4, provider: str = DEFAULT_PROVIDER
) -> str:
    """One fundamental statement: income | balance | cash | metrics | dividends."""
    statement = (statement or "").lower().strip()
    if statement not in _STATEMENTS:
        raise ValueError(
            f"Unknown statement {statement!r}. Supported: {sorted(_STATEMENTS)}"
        )
    fn = getattr(_obb().equity.fundamental, statement)
    kwargs: dict = {"symbol": symbol, "provider": provider}
    if _STATEMENTS[statement]:  # income/balance/cash accept a period limit
        kwargs["limit"] = max(1, int(limit))
    return _render(fn(**kwargs), max_rows=max(1, int(limit)))


def profile(symbol: str, provider: str = DEFAULT_PROVIDER) -> str:
    """Company profile (name, exchange, sector, description, …)."""
    return _render(_obb().equity.profile(symbol=symbol, provider=provider))


def consensus(symbol: str, provider: str = DEFAULT_PROVIDER) -> str:
    """Analyst price-target consensus and recommendation."""
    return _render(_obb().equity.estimates.consensus(symbol=symbol, provider=provider))


def share_statistics(symbol: str, provider: str = DEFAULT_PROVIDER) -> str:
    """Share counts / float / short interest."""
    return _render(
        _obb().equity.ownership.share_statistics(symbol=symbol, provider=provider)
    )


def discovery(category: str = "gainers", limit: int = 20, provider: str = DEFAULT_PROVIDER) -> str:
    """A market screen: gainers | losers | active | growth_tech | aggressive_small_caps
    | undervalued_growth | undervalued_large_caps."""
    category = (category or "").lower().strip()
    if category not in _DISCOVERY:
        raise ValueError(
            f"Unknown category {category!r}. Supported: {sorted(_DISCOVERY)}"
        )
    fn = getattr(_obb().equity.discovery, category)
    return _render(fn(provider=provider), max_rows=max(1, int(limit)))
