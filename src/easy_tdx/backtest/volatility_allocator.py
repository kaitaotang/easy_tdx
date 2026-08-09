"""组合回测的波动诊断与风险加权对照实验。

这个模块故意不改动原有的 ``MultiStrategyEngine`` 成交逻辑。它使用各策略已经
完成的独立回测净值，构造一个只用历史信息的“风险加权虚拟组合”，用于
回答两个问题：

* 当前买点处于该标的的低波动还是高波动区间？
* 该策略过去在低波动区间是否真的比高波动区间更赚钱？

对照实验不是实盘成交回放：它暂不模拟动态调仓手续费，页面会明确标注这一点。先观察
对照结果，再决定是否把风险加权和分批执行接入真实成交路径，可以避免把回测结果优化成
“看起来更好”但无法解释的黑盒。
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from easy_tdx.backtest.performance import PerformanceAnalyzer
from easy_tdx.backtest.types import BacktestResult


LOOKBACK = 120
ATR_WINDOW = 14
REALIZED_WINDOW = 20
MIN_REGIME_OBS = 10
MAX_STRATEGY_WEIGHT = 0.60
MAX_TOTAL_EXPOSURE = 0.90


def _datetime_index(values: Any) -> pd.DatetimeIndex:
    """把 int YYYYMMDD、Timestamp 和字符串日期统一成 DatetimeIndex。"""
    series = pd.Series(values)
    if pd.api.types.is_numeric_dtype(series):
        parsed = pd.to_datetime(
            series.astype("Int64").astype(str), format="%Y%m%d", errors="coerce"
        )
    else:
        parsed = pd.to_datetime(series, errors="coerce")
    return pd.DatetimeIndex(parsed)


def _volatility_frame(df: pd.DataFrame) -> pd.DataFrame:
    """计算每根 K 线的 ATR%、20 日实现波动率及波动区间。"""
    close = pd.to_numeric(df["close"], errors="coerce")
    high = pd.to_numeric(df["high"], errors="coerce")
    low = pd.to_numeric(df["low"], errors="coerce")
    previous_close = close.shift(1)
    true_range = pd.concat(
        [high - low, (high - previous_close).abs(), (low - previous_close).abs()], axis=1
    ).max(axis=1)
    atr = true_range.rolling(ATR_WINDOW, min_periods=ATR_WINDOW).mean()
    atr_pct = atr.divide(close.replace(0, np.nan)).replace([np.inf, -np.inf], np.nan)

    log_returns = np.log(close.where(close > 0)).diff()
    realized_vol = log_returns.rolling(REALIZED_WINDOW, min_periods=10).std() * np.sqrt(252)

    # 用滚动分位数判断相对高低，不把“2% 就是低波动”硬编码到所有股票上。
    min_periods = min(40, LOOKBACK)
    low_threshold = atr_pct.rolling(LOOKBACK, min_periods=min_periods).quantile(0.40)
    high_threshold = atr_pct.rolling(LOOKBACK, min_periods=min_periods).quantile(0.60)
    # 历史数据不足一个完整窗口时退化到 expanding 分位数，仍只使用当前以前的数据。
    low_threshold = low_threshold.fillna(atr_pct.expanding(min_periods=10).quantile(0.40))
    high_threshold = high_threshold.fillna(atr_pct.expanding(min_periods=10).quantile(0.60))
    atr_percentile = atr_pct.rolling(LOOKBACK, min_periods=10).apply(
        lambda values: float(np.mean(values <= values[-1])), raw=True
    )
    regime = pd.Series("normal", index=df.index, dtype="object")
    regime.loc[atr_pct <= low_threshold] = "low"
    regime.loc[atr_pct >= high_threshold] = "high"

    return pd.DataFrame(
        {
            "atr_pct": atr_pct.to_numpy(),
            "atr_percentile": atr_percentile.to_numpy(),
            "realized_vol": realized_vol.to_numpy(),
            "regime": regime.to_numpy(),
        },
        index=_datetime_index(df["datetime"]),
    )


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return default
    return value if np.isfinite(value) else default


def _profile_for_slot(slot: Any, result: BacktestResult) -> dict[str, Any]:
    """输出一个策略槽位的低/高波动历史关系摘要。"""
    vf = _volatility_frame(slot.df)
    equity = result.equity_curve.copy()
    eq_index = _datetime_index(equity["datetime"])
    eq_total = pd.Series(pd.to_numeric(equity["total"], errors="coerce").to_numpy(), index=eq_index)
    eq_ret = eq_total.pct_change()
    regime = vf["regime"].reindex(eq_index).ffill()
    atr_pct = vf["atr_pct"].reindex(eq_index).ffill()
    realized = vf["realized_vol"].reindex(eq_index).ffill()

    low = eq_ret[(regime == "low") & eq_ret.notna()]
    high = eq_ret[(regime == "high") & eq_ret.notna()]
    low_annual = _safe_float(low.mean() * 252) if len(low) else 0.0
    high_annual = _safe_float(high.mean() * 252) if len(high) else 0.0
    edge = low_annual - high_annual
    enough = len(low) >= MIN_REGIME_OBS and len(high) >= MIN_REGIME_OBS
    if not enough or abs(edge) < 0.02:
        relationship = "关系不明显"
    elif edge > 0:
        relationship = "低波动更有利"
    else:
        relationship = "高波动更有利"

    valid_atr = atr_pct.dropna()
    valid_realized = realized.dropna()
    current_atr = _safe_float(valid_atr.iloc[-1]) if len(valid_atr) else 0.0
    current_realized = _safe_float(valid_realized.iloc[-1]) if len(valid_realized) else 0.0
    current_regime = str(regime.iloc[-1]) if len(regime) else "normal"
    # 当前 ATR 在自身历史中的百分位，便于用户理解“低/高”是相对概念。
    valid_percentile = vf["atr_percentile"].dropna()
    atr_percentile = _safe_float(valid_percentile.iloc[-1]) if len(valid_percentile) else 0.0

    return {
        "strategy_label": slot.label,
        "symbol": slot.symbol,
        "current_atr_pct": current_atr,
        "current_realized_vol": current_realized,
        "current_regime": current_regime,
        "atr_percentile": atr_percentile,
        "low_vol_annual_return": low_annual,
        "high_vol_annual_return": high_annual,
        "low_vol_edge": edge,
        "relationship": relationship,
        "low_regime_observations": int(len(low)),
        "high_regime_observations": int(len(high)),
    }


def _annotate_trade_volatility(slot: Any, result: BacktestResult) -> None:
    """把成交日前可知的 ATR/波动状态写入交易明细，供组合页逐买点展示。"""
    if len(result.trades) == 0:
        return
    vf = _volatility_frame(slot.df)
    trades = result.trades.copy()
    trade_index = _datetime_index(trades["datetime"])
    aligned = vf.reindex(trade_index, method="ffill")
    is_buy = trades["direction"].astype(str).eq("BUY").to_numpy()
    for source, target in (
        ("atr_pct", "atr_pct"),
        ("atr_percentile", "atr_percentile"),
        ("realized_vol", "realized_vol"),
    ):
        values = aligned[source].to_numpy(copy=True)
        values[~is_buy] = np.nan
        trades[target] = values
    regimes = aligned["regime"].astype(object).to_numpy(copy=True)
    regimes[~is_buy] = None
    trades["volatility_regime"] = regimes
    result.trades = trades


def _cap_weights(raw: np.ndarray, active: np.ndarray) -> np.ndarray:
    """限制单策略和组合总仓位，剩余部分保留现金。"""
    weights = np.zeros_like(raw, dtype=float)
    active_indices = np.flatnonzero(active & np.isfinite(raw) & (raw > 0))
    if len(active_indices) == 0:
        return weights

    # 一个策略独自发信号时也不超过 60%；两个以上活跃时最多投入 90%。
    target_exposure = min(MAX_TOTAL_EXPOSURE, MAX_STRATEGY_WEIGHT * len(active_indices))
    remaining = target_exposure
    available = active_indices.tolist()
    while available and remaining > 1e-12:
        raw_sum = float(raw[available].sum())
        if raw_sum <= 0:
            break
        proposed = {i: remaining * raw[i] / raw_sum for i in available}
        capped = [i for i in available if proposed[i] > MAX_STRATEGY_WEIGHT]
        if not capped:
            for i, value in proposed.items():
                weights[i] = value
            break
        for i in capped:
            weights[i] = MAX_STRATEGY_WEIGHT
            remaining -= MAX_STRATEGY_WEIGHT
        available = [i for i in available if i not in capped]
    return weights


def _metric_frame(
    index: pd.DatetimeIndex, returns: pd.Series, total_cash: float
) -> tuple[pd.DataFrame, dict[str, float]]:
    """由日收益率生成净值曲线和与单标的一致的指标。"""
    clean_returns = returns.replace([np.inf, -np.inf], np.nan).fillna(0.0)
    totals = total_cash * (1.0 + clean_returns).cumprod()
    peak = totals.cummax()
    drawdown = peak - totals
    drawdown_pct = drawdown.divide(peak.replace(0, np.nan)).fillna(0.0)
    equity = pd.DataFrame(
        {
            "datetime": index,
            "total": totals.to_numpy(),
            "drawdown": drawdown.to_numpy(),
            "drawdown_pct": drawdown_pct.to_numpy(),
        }
    )
    empty_trades = pd.DataFrame(columns=["direction", "pnl", "rejected"])
    metrics = PerformanceAnalyzer(equity, empty_trades).compute()
    metrics["total_cash"] = float(total_cash)
    return equity, metrics


def build_volatility_comparison(
    slots: list[Any],
    results: dict[str, BacktestResult],
    total_cash: float,
    baseline_performance: dict[str, float],
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """生成波动诊断和只用历史信息的风险加权虚拟组合对照。"""
    profiles: dict[str, dict[str, Any]] = {}
    data: list[dict[str, Any]] = []
    for slot in slots:
        key = f"{slot.label}@{slot.symbol}"
        result = results.get(key)
        if result is None or len(result.equity_curve) == 0:
            continue
        profiles[key] = _profile_for_slot(slot, result)
        _annotate_trade_volatility(slot, result)
        vf = _volatility_frame(slot.df)
        eq = result.equity_curve
        eq_index = _datetime_index(eq["datetime"])
        initial = _safe_float(eq["total"].iloc[0], 1.0) or 1.0
        norm = pd.Series(
            pd.to_numeric(eq["total"], errors="coerce").to_numpy() / initial,
            index=eq_index,
        )
        pos = result.positions
        pos_index = _datetime_index(pos["datetime"]) if len(pos) else eq_index
        active = (
            pd.Series(pd.to_numeric(pos["size"], errors="coerce").to_numpy(), index=pos_index)
            if len(pos)
            else pd.Series(0.0, index=eq_index)
        )
        data.append(
            {
                "key": key,
                "norm": norm,
                "active": active > 0.5,
                "atr_pct": vf["atr_pct"],
                "realized_vol": vf["realized_vol"],
                "regime": vf["regime"],
            }
        )

    if not data:
        empty = {"total_return": 0.0, "annual_return": 0.0, "max_drawdown": 0.0, "sharpe": 0.0}
        return profiles, {
            "method": "past_only_volatility_aware",
            "lookback": LOOKBACK,
            "max_strategy_weight": MAX_STRATEGY_WEIGHT,
            "max_total_exposure": MAX_TOTAL_EXPOSURE,
            "baseline": empty,
            "adaptive": empty,
            "delta": empty,
            "note": "没有足够的策略净值数据，无法生成波动适配对照。",
        }

    index = pd.DatetimeIndex(sorted(set().union(*(set(item["norm"].index) for item in data))))
    returns: list[pd.Series] = []
    active_series: list[pd.Series] = []
    atr_series: list[pd.Series] = []
    realized_series: list[pd.Series] = []
    regime_series: list[pd.Series] = []
    for item in data:
        norm = item["norm"].reindex(index).ffill()
        previous = norm.shift(1)
        ret = norm.divide(previous).subtract(1.0)
        ret[(norm.isna()) | previous.isna()] = 0.0
        returns.append(ret.fillna(0.0))
        # 只用前一日持仓和波动，避免用成交日结果反推当天权重。
        active_series.append(
            item["active"].reindex(index).ffill().fillna(False).shift(1).fillna(False)
        )
        atr_series.append(item["atr_pct"].reindex(index).ffill().shift(1))
        realized_series.append(item["realized_vol"].reindex(index).ffill().shift(1))
        regime_series.append(item["regime"].reindex(index).ffill().shift(1))

    # 基准指标直接使用原组合净值；实验组从同一批独立日收益计算。
    adaptive_returns = pd.Series(0.0, index=index)
    for t in range(len(index)):
        active = np.array([bool(series.iloc[t]) for series in active_series], dtype=bool)
        raw = np.zeros(len(data), dtype=float)
        for i in range(len(data)):
            risk = _safe_float(realized_series[i].iloc[t])
            if risk <= 0:
                risk = _safe_float(atr_series[i].iloc[t]) * np.sqrt(252)
            if risk <= 0:
                risk = 0.25

            # 用 t 之前最多 120 根收益，估计该策略对低/高波动的偏好。
            start = max(0, t - LOOKBACK)
            hist_ret = returns[i].iloc[start:t]
            hist_regime = regime_series[i].iloc[start:t]
            low_ret = hist_ret[hist_regime == "low"].dropna()
            high_ret = hist_ret[hist_regime == "high"].dropna()
            edge = 0.0
            if len(low_ret) >= MIN_REGIME_OBS and len(high_ret) >= MIN_REGIME_OBS:
                edge = _safe_float((low_ret.mean() - high_ret.mean()) * 252)
            regime = str(regime_series[i].iloc[t])
            preference = 1.0
            if abs(edge) >= 0.02:
                if (edge > 0 and regime == "low") or (edge < 0 and regime == "high"):
                    preference = 1.25
                elif regime in {"low", "high"}:
                    preference = 0.75
            raw[i] = preference / risk
        weights = _cap_weights(raw, active)
        adaptive_returns.iloc[t] = float(
            sum(weights[i] * returns[i].iloc[t] for i in range(len(data)))
        )

    _, adaptive = _metric_frame(index, adaptive_returns, total_cash)
    baseline = {
        key: _safe_float(baseline_performance.get(key, 0.0))
        for key in ("total_return", "annual_return", "max_drawdown", "sharpe")
    }

    adaptive_summary = {
        key: _safe_float(adaptive[key])
        for key in ("total_return", "annual_return", "max_drawdown", "sharpe")
    }
    delta = {key: _safe_float(adaptive_summary[key] - baseline[key]) for key in adaptive_summary}
    comparison = {
        "method": "past_only_volatility_aware",
        "lookback": LOOKBACK,
        "max_strategy_weight": MAX_STRATEGY_WEIGHT,
        "max_total_exposure": MAX_TOTAL_EXPOSURE,
        "baseline": baseline,
        "adaptive": adaptive_summary,
        "delta": delta,
        "note": (
            "自适应结果是基于独立策略日收益的历史信息虚拟组合，暂未改变原始成交记录，"
            "也未计入动态调仓手续费。"
        ),
    }
    return profiles, comparison
