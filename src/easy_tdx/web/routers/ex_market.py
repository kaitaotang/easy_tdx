"""扩展市场路由：期货、港股、美股等扩展市场行情数据。"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Depends, Query

from easy_tdx.web.convert import category_from_str, ex_market_from_str
from easy_tdx.web.deps import get_ex_client
from easy_tdx.web.schemas import DataFrameResponse

router = APIRouter(tags=["ex-market"])


def _records_to_df_resp(records: list[Any]) -> DataFrameResponse:
    """将 dataclass 列表转为 DataFrameResponse（过滤内部 _raw 字段）。"""
    import pandas as pd

    if not records:
        return DataFrameResponse(data=[], count=0)
    rows = [{k: v for k, v in asdict(r).items() if not k.startswith("_")} for r in records]
    df = pd.DataFrame(rows)
    return DataFrameResponse.from_dataframe(df)


@router.get("/ex/bars", response_model=DataFrameResponse)
async def ex_bars(
    market: str = Query(..., description="扩展市场代码，如 HK_MAIN_BOARD 或数字"),
    code: str = Query(..., description="合约/证券代码"),
    category: str = Query("DAY", description="K线周期: MIN_1/MIN_5/.../DAY/WEEK/MONTH"),
    start: int = Query(0, ge=0),
    count: int = Query(700, ge=1, le=700),
    client: Any = Depends(get_ex_client),
) -> DataFrameResponse:
    """获取扩展市场 K 线数据（走 MAC 协议客户端，支持美股/港股/期货）。"""
    from easy_tdx.mac.enums import Period

    # 前端 category (DAY/WEEK/MONTH/MIN_5/...) → MAC Period 枚举
    _PERIOD_MAP = {
        "DAY": Period.DAILY,
        "WEEK": Period.WEEKLY,
        "MONTH": Period.MONTHLY,
        "MIN_5": Period.MIN_5,
        "MIN_15": Period.MIN_15,
        "MIN_30": Period.MIN_30,
        "MIN_60": Period.MIN_60,
        "MIN_1": Period.MIN_1,
    }
    period = _PERIOD_MAP.get(category.upper(), Period.DAILY)

    df = await client.goods_kline(
        market=ex_market_from_str(market),
        code=code,
        period=period,
        start=start,
        count=count,
    )
    if df is None or df.empty:
        return DataFrameResponse(data=[], count=0)
    return DataFrameResponse.from_dataframe(df)


@router.get("/ex/quote", response_model=DataFrameResponse)
async def ex_quote(
    market: str = Query(..., description="扩展市场代码"),
    code: str = Query(..., description="合约/证券代码"),
    client: Any = Depends(get_ex_client),
) -> DataFrameResponse:
    """获取扩展市场实时报价。"""
    result = await client.get_instrument_quote(market=ex_market_from_str(market), code=code)
    if result is None:
        return DataFrameResponse(data=[], count=0)
    return _records_to_df_resp([result])


@router.get("/ex/minute", response_model=DataFrameResponse)
async def ex_minute(
    market: str = Query(..., description="扩展市场代码"),
    code: str = Query(..., description="合约/证券代码"),
    client: Any = Depends(get_ex_client),
) -> DataFrameResponse:
    """获取扩展市场分时数据。"""
    records = await client.get_minute_time_data(market=ex_market_from_str(market), code=code)
    return _records_to_df_resp(records)


@router.get("/ex/transaction", response_model=DataFrameResponse)
async def ex_transaction(
    market: str = Query(..., description="扩展市场代码"),
    code: str = Query(..., description="合约/证券代码"),
    start: int = Query(0, ge=0),
    count: int = Query(1800, ge=1, le=3000),
    client: Any = Depends(get_ex_client),
) -> DataFrameResponse:
    """获取扩展市场逐笔成交数据。"""
    records = await client.get_transaction_data(
        market=ex_market_from_str(market), code=code, start=start, count=count
    )
    return _records_to_df_resp(records)
