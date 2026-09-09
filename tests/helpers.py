"""backtest.py 회계 로직 회귀 테스트용 공용 헬퍼.

실 데이터(backtest_ohlcv.parquet)·KRX 로그인 없이 순수 합성 OHLCV로
run_portfolio_backtest()를 구동하기 위해 backtest.load_ticker_df와
screener.load_sector_map을 통째로 monkeypatch한다. core.py·kis_client.py는
건드리지 않는다 - 여기서도 그 둘을 patch하지 않는다.
"""

from __future__ import annotations

import pandas as pd

import backtest

COLUMNS = ["시가", "고가", "저가", "종가", "거래량", "유동성충족"]


def row(o: float, h: float, l: float, c: float, v: float = 5_000_000.0,
        liquid: bool = True) -> dict:
    return {"시가": o, "고가": h, "저가": l, "종가": c, "거래량": v, "유동성충족": liquid}


def flat_row(close: float, half_spread: float = 300.0, v: float = 5_000_000.0) -> dict:
    """전일 종가와 당일 종가가 같다고 가정한 평온한 캔들 (진폭만 half_spread*2)."""
    return row(close, close + half_spread, close - half_spread, close, v)


def warmup_rows(n: int = 80, close: float = 10_000.0, half_spread: float = 300.0,
                 v: float = 5_000_000.0) -> list[dict]:
    return [flat_row(close, half_spread, v) for _ in range(n)]


def build_df(rows: list[dict], start: str = "2020-01-02",
             dates: list[pd.Timestamp] | None = None) -> pd.DataFrame:
    """rows를 영업일 인덱스가 붙은 DataFrame으로 만든다.

    dates를 주면 그 날짜들을 그대로 인덱스로 쓴다(halt 시나리오처럼 특정
    날짜를 통째로 빼야 할 때). 없으면 start부터 영업일을 순서대로 붙인다.
    """
    if dates is None:
        idx = pd.bdate_range(start=start, periods=len(rows))
    else:
        idx = pd.DatetimeIndex(dates)
    df = pd.DataFrame(rows, index=idx)
    return df[COLUMNS]


def calendar_df(start: str = "2020-01-02", periods: int = 260,
                 close: float = 10_000.0) -> pd.DataFrame:
    """sim_days 계산에 쓰이는 달력 티커(005930)용 - 공백 없는 전체 영업일."""
    return build_df(warmup_rows(periods, close=close), start=start)


def install_universe(monkeypatch, dfs: dict[str, pd.DataFrame],
                      sector_map: dict[str, str] | None = None) -> None:
    def _fake_load(ticker: str) -> pd.DataFrame:
        if ticker not in dfs:
            raise ValueError(f"[{ticker}] 테스트 데이터 없음")
        return dfs[ticker]

    monkeypatch.setattr(backtest, "load_ticker_df", _fake_load)
    monkeypatch.setattr(backtest.screener, "load_sector_map",
                         lambda day, refresh=False: dict(sector_map or {}))
