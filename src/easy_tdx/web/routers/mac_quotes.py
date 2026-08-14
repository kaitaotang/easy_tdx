"""MAC 行情路由：排行行情列表、竞价数据、异动行情。"""

from __future__ import annotations

from typing import Any

import pandas as pd
from fastapi import APIRouter, Depends, Query

from easy_tdx.web.convert import (
    category_mac_from_str,
    filter_types_from_str,
    market_value_from_str,
    sort_order_from_str,
    sort_type_from_str,
)
from easy_tdx.web.deps import get_mac_client, get_optional_ex_client, get_optional_mac_client
from easy_tdx.web.schemas import DataFrameResponse

router = APIRouter(tags=["mac-quotes"])


def _df_resp(df: Any) -> DataFrameResponse:
    return DataFrameResponse.from_dataframe(df)


@router.get("/mac/dividend-yield", response_model=DataFrameResponse)
async def dividend_yield(
    market: str = Query(..., description="MAC 市场编号或名称，如 SH/CSI_INDEX"),
    code: str = Query(..., min_length=6, max_length=6),
    client: Any | None = Depends(get_optional_mac_client),
    ex_client: Any | None = Depends(get_optional_ex_client),
) -> DataFrameResponse:
    """获取行情源当前股息率（可用于 ETF 跟踪指数的参考值）。"""
    from easy_tdx.codec.bitmap import FieldBit

    is_ex_market = market.upper() not in {"SZ", "SH", "BJ"}
    if not is_ex_market:
        market_value = market_value_from_str(market)
    else:
        from easy_tdx.web.convert import ex_market_from_str

        market_value = ex_market_from_str(market) if not market.isdigit() else int(market)
    # CSI_INDEX/H30269 等扩展市场必须使用 MAC 扩展客户端。普通 MAC
    # 客户端接受任意整数市场号，可能返回一行“字段全为 0”的假成功结果，
    # 因而不能把它当成有效股息率。
    if is_ex_market:
        df = None
    else:
        try:
            if client is None:
                raise RuntimeError("MAC 客户端未连接")
            df = await client.get_stock_quotes(
                [(market_value, code.upper())], fields=FieldBit.DIVIDEND_YIELD_RATE
            )
        except Exception:
            df = None
    if (df is None or df.empty) and ex_client is not None:
        df = await ex_client.goods_quotes(
            [(market_value, code.upper())], fields=FieldBit.DIVIDEND_YIELD_RATE
        )
    if df is None or df.empty:
        return DataFrameResponse(data=[], count=0)
    # 0、NaN、负数表示字段缺失/未覆盖，不是“股息率为 0%”。
    field = "dividend_yield_rate"
    if field in df.columns:
        value = pd.to_numeric(df[field], errors="coerce")
        if value.empty or not bool((value > 0).any()):
            return DataFrameResponse(data=[], count=0)
    return _df_resp(df)


@router.get("/mac/quote-list", response_model=DataFrameResponse)
async def quote_list(
    category: str = Query("A", description="市场分类: A/SH/SZ/KCB/BJ/CYB"),
    start: int = Query(0, ge=0, description="分页起始位置"),
    count: int = Query(80, ge=1, le=5000, description="返回数量"),
    sort_type: str = Query("CHANGE_PCT", description="排序字段"),
    sort_order: str = Query("DESC", description="排序方向: ASC/DESC"),
    exclude: str | None = Query(None, description="过滤标志（逗号分隔）: ST,KC,BJ,..."),
    client: Any = Depends(get_mac_client),
) -> DataFrameResponse:
    """获取分排行行情列表（涨幅/成交量/换手等排序）。"""
    exclude_flags = filter_types_from_str(exclude) if exclude else None
    df = await client.get_stock_quotes_list(
        category=category_mac_from_str(category),
        start=start,
        count=count,
        sort_type=sort_type_from_str(sort_type),
        sort_order=sort_order_from_str(sort_order),
        exclude_flags=exclude_flags,
    )
    return _df_resp(df)


@router.get("/mac/auction", response_model=DataFrameResponse)
async def auction(
    market: str = Query(..., description="市场: SZ, SH"),
    code: str = Query(..., min_length=6, max_length=6, description="6位股票代码"),
    client: Any = Depends(get_mac_client),
) -> DataFrameResponse:
    """获取集合竞价数据。"""
    df = await client.get_auction(market=market_value_from_str(market), code=code)
    return _df_resp(df)


@router.get("/mac/unusual", response_model=DataFrameResponse)
async def unusual(
    market: str = Query(..., description="市场: SZ, SH"),
    start: int = Query(0, ge=0, description="分页起始位置"),
    count: int = Query(50, ge=1, le=500, description="返回数量"),
    client: Any = Depends(get_mac_client),
) -> DataFrameResponse:
    """获取市场异动行情数据。"""
    df = await client.get_unusual(market=market_value_from_str(market), start=start, count=count)
    return _df_resp(df)
