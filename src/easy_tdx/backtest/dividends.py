"""股息率历史序列计算。

股息率不是 K 线自带的技术指标。这里使用除权除息记录中的现金分红和
当日收盘价计算“滚动 12 个月股息率”，并且只使用当日及之前已经发生的
分红，避免把未来分红泄漏到历史回测中。
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd


def _as_datetime(values: Any) -> pd.DatetimeIndex:
    """兼容 YYYYMMDD、Timestamp 和 ISO 字符串日期。"""
    series = pd.Series(values)
    if pd.api.types.is_numeric_dtype(series):
        parsed = pd.to_datetime(
            series.astype("Int64").astype(str), format="%Y%m%d", errors="coerce"
        )
    else:
        parsed = pd.to_datetime(series, errors="coerce")
    return pd.DatetimeIndex(parsed).normalize()


def build_dividend_profile(
    bars: pd.DataFrame,
    dividends: pd.DataFrame | list[dict[str, Any]] | None,
    *,
    trailing_days: int = 365,
    fallback_yield_pct: float | None = None,
    fallback_source: str | None = None,
) -> dict[str, Any]:
    """根据 K 线和除权除息记录生成股息率摘要及历史序列。

    ``yield_pct`` 的单位是百分比数值（3.2 表示 3.2%），与 TDX 的股息率
    字段保持一致；这和回测绩效里的 0~1 比率不同，因此字段名明确带有
    ``_pct``。
    """
    empty: dict[str, Any] = {
        "available": False,
        "current_yield_pct": None,
        "historical_percentile": None,
        "current_dividend_per_share": None,
        "as_of": None,
        "trailing_days": trailing_days,
        "event_count": 0,
        "history": [],
        "reference_only": False,
        "note": "行情源未提供可用的历史现金分红记录",
    }
    if bars is None or len(bars) == 0 or "datetime" not in bars or "close" not in bars:
        return _fallback_profile(empty, fallback_yield_pct, fallback_source)

    if dividends is None:
        return _fallback_profile(empty, fallback_yield_pct, fallback_source)
    div_df = dividends.copy() if isinstance(dividends, pd.DataFrame) else pd.DataFrame(dividends)
    if div_df.empty or "fenhong" not in div_df.columns:
        return _fallback_profile(empty, fallback_yield_pct, fallback_source)
    date_col = (
        "date"
        if "date" in div_df.columns
        else "datetime"
        if "datetime" in div_df.columns
        else None
    )
    if date_col is None:
        return _fallback_profile(empty, fallback_yield_pct, fallback_source)

    # 只取现金分红事件。XDXR 的 category=1 是除权除息；兼容调用方没有
    # category 列的精简事件格式，但不会把送转/配股误当现金股息。
    if "category" in div_df.columns:
        category = pd.to_numeric(div_df["category"], errors="coerce")
        div_df = div_df[category.eq(1)]
    event_dates = _as_datetime(div_df[date_col])
    cash = pd.to_numeric(div_df["fenhong"], errors="coerce")
    split = (
        pd.to_numeric(div_df["songzhuangu"], errors="coerce")
        if "songzhuangu" in div_df
        else pd.Series(0.0, index=div_df.index)
    ).fillna(0.0)
    rights = (
        pd.to_numeric(div_df["peigu"], errors="coerce")
        if "peigu" in div_df
        else pd.Series(0.0, index=div_df.index)
    ).fillna(0.0)
    valid = event_dates.notna() & cash.notna().to_numpy() & np.isfinite(cash.to_numpy())
    if not bool((valid & (cash.to_numpy() > 0)).any()):
        empty["note"] = "该标的没有可用的历史现金分红记录"
        return _fallback_profile(empty, fallback_yield_pct, fallback_source)
    event_frame = pd.DataFrame(
        {
            "date": event_dates[valid],
            "cash": cash.to_numpy()[valid],
            "share_factor": (1.0 + split.to_numpy() + rights.to_numpy())[valid],
        }
    )
    event_frame["cash"] = event_frame["cash"].clip(lower=0.0)
    event_frame["share_factor"] = event_frame["share_factor"].where(
        np.isfinite(event_frame["share_factor"]) & (event_frame["share_factor"] > 0), 1.0
    )
    # 同一除权日可能有重复记录，先合并，保证每笔事件只累计一次。
    event_frame = (
        event_frame.groupby("date", as_index=False)
        .agg(cash=("cash", "sum"), share_factor=("share_factor", "prod"))
        .sort_values("date")
    )

    dates = _as_datetime(bars["datetime"])
    close = pd.to_numeric(bars["close"], errors="coerce").to_numpy(dtype=float)
    valid_bar = dates.notna() & np.isfinite(close) & (close > 0)
    if not bool(valid_bar.any()):
        return _fallback_profile(empty, fallback_yield_pct, fallback_source)

    event_ns = event_frame["date"].to_numpy(dtype="datetime64[ns]").astype("int64")
    event_cash = event_frame["cash"].to_numpy(dtype=float)
    event_share_factor = event_frame["share_factor"].to_numpy(dtype=float)
    bar_ns = dates.to_numpy(dtype="datetime64[ns]").astype("int64")
    window_ns = np.timedelta64(trailing_days, "D").astype("timedelta64[ns]").astype("int64")
    left = np.searchsorted(event_ns, bar_ns - window_ns, side="left")
    right = np.searchsorted(event_ns, bar_ns, side="right")
    trailing_cash = np.zeros(len(bars), dtype=float)
    # 将窗口内较早的分红除以后续累计送转/配股因子，换算成当前 bar 的
    # 每股口径。事件数通常只有几十条，逐 bar 计算更易审计且开销很小。
    for i, (start, stop) in enumerate(zip(left, right, strict=True)):
        if start >= stop:
            continue
        later_factor = 1.0
        converted_cash = 0.0
        for event_idx in range(int(stop) - 1, int(start) - 1, -1):
            later_factor *= event_share_factor[event_idx]
            converted_cash += event_cash[event_idx] / later_factor
        trailing_cash[i] = converted_cash

    positive_cash_events = event_cash > 0
    first_event_ns = event_ns[np.flatnonzero(positive_cash_events)[0]]
    has_history = valid_bar & (bar_ns >= first_event_ns)
    yields = np.full(len(bars), np.nan, dtype=float)
    yields[has_history] = trailing_cash[has_history] / close[has_history] * 100.0
    valid_yield = np.isfinite(yields)
    if not bool(valid_yield.any()):
        empty["note"] = (
            "回测区间内没有已发生的现金分红，无法计算历史股息率"
        )
        return _fallback_profile(empty, fallback_yield_pct, fallback_source)

    history: list[dict[str, Any]] = []
    for i in np.flatnonzero(valid_yield):
        history.append(
            {
                "datetime": dates[i].isoformat(),
                "dividend_per_share": float(trailing_cash[i]),
                "close": float(close[i]),
                "yield_pct": float(yields[i]),
            }
        )
    current = float(yields[np.flatnonzero(valid_yield)[-1]])
    historical_percentile = float(np.mean(yields[valid_yield] <= current) * 100.0)
    latest_idx = int(np.flatnonzero(valid_yield)[-1])
    return {
        "available": True,
        "current_yield_pct": current,
        "historical_percentile": historical_percentile,
        "current_dividend_per_share": float(trailing_cash[latest_idx]),
        "as_of": dates[latest_idx].isoformat(),
        "trailing_days": trailing_days,
        "event_count": int(positive_cash_events.sum()),
        "history": history,
        "note": (
            "按除权日已发生现金分红计算近12个月股息率；"
            "分位值为当前回测区间内历史有效日的百分位"
        ),
    }


def _fallback_profile(
    profile: dict[str, Any], yield_pct: float | None, source: str | None
) -> dict[str, Any]:
    """用行情源提供的当前股息率补齐无历史现金分红的标的。

    只有一个当前值时不伪造历史曲线或历史分位，明确标记为“参考值”。
    """
    if yield_pct is None or not np.isfinite(yield_pct) or yield_pct <= 0:
        return profile
    label = source or "行情源当前股息率"
    profile.update(
        {
            "available": True,
            "current_yield_pct": float(yield_pct),
            "historical_percentile": None,
            "reference_only": True,
            "note": f"暂无可用历史现金分红，仅显示参考股息率（来源：{label}）",
            "source": label,
        }
    )
    return profile
