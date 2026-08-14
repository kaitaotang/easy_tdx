"""单标回测股息率指标测试。"""

from __future__ import annotations

import pandas as pd
import pytest

from easy_tdx.backtest.dividends import build_dividend_profile


def _bars(start: str, periods: int, close: float = 10.0) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "datetime": pd.date_range(start, periods=periods, freq="D"),
            "close": [close] * periods,
        }
    )


def test_trailing_yield_uses_only_dividends_known_on_each_bar() -> None:
    bars = _bars("2024-01-01", 5)
    dividends = pd.DataFrame(
        [
            {"date": "2024-01-02", "category": 1, "fenhong": 0.2},
            {"date": "2024-01-04", "category": 1, "fenhong": 0.3},
        ]
    )

    profile = build_dividend_profile(bars, dividends)

    assert profile["available"] is True
    history = profile["history"]
    # 首笔分红发生前没有历史股息率，不能把未来分红泄漏到 1 月 1 日。
    assert history[0]["datetime"].startswith("2024-01-02")
    assert history[0]["yield_pct"] == pytest.approx(2.0)
    assert history[-1]["yield_pct"] == pytest.approx(5.0)
    assert profile["current_yield_pct"] == pytest.approx(5.0)
    assert profile["historical_percentile"] == pytest.approx(100.0)


def test_yield_adjusts_cash_per_share_for_bonus_shares() -> None:
    bars = _bars("2024-01-01", 3, close=10.0)
    dividends = pd.DataFrame(
        [
            {
                "date": 20240102,
                "category": 1,
                "fenhong": 0.2,
                "songzhuangu": 1.0,
                "peigu": 0.0,
            }
        ]
    )

    profile = build_dividend_profile(bars, dividends)

    # 每 1 股送转 1 股后，0.2 元/原股折为 0.1 元/新股口径。
    assert profile["current_dividend_per_share"] == pytest.approx(0.1)
    assert profile["current_yield_pct"] == pytest.approx(1.0)


def test_no_cash_dividend_returns_explicit_unavailable_profile() -> None:
    profile = build_dividend_profile(
        _bars("2024-01-01", 3),
        [{"date": "2024-01-02", "category": 1, "fenhong": 0.0}],
    )

    assert profile["available"] is False
    assert profile["current_yield_pct"] is None


def test_current_index_yield_fallback_does_not_invent_history() -> None:
    bars = _bars("2024-01-01", 3)
    profile = build_dividend_profile(
        bars,
        [],
        fallback_yield_pct=4.35,
        fallback_source="CSI_INDEX:H30269（512890跟踪指数）",
    )

    assert profile["available"] is True
    assert profile["current_yield_pct"] == pytest.approx(4.35)
    assert profile["historical_percentile"] is None
    assert profile["history"] == []
    assert profile["reference_only"] is True
    assert "H30269" in profile["note"]


def test_zero_quote_field_is_not_treated_as_zero_dividend_yield() -> None:
    profile = build_dividend_profile(
        _bars("2024-01-01", 3),
        [],
        fallback_yield_pct=0.0,
        fallback_source="CSI_INDEX:H30269",
    )

    assert profile["available"] is False
    assert profile["current_yield_pct"] is None
    assert "source" not in profile
