"""요구사항 #2 추가 회귀 - 시가·종가가 NaN이 아니라 진짜 None인 행.

_day_open()/_day_close()가 float(row["시가"])를 _is_valid_price() 호출보다
먼저 실행하면, 값이 NaN이 아니라 파이썬 None 그 자체일 때(object dtype
컬럼에서 흔함) float(None)이 그 자리에서 TypeError를 던져 시뮬레이션
전체가 죽는다. _is_valid_price()는 이런 값도 안전하게 처리하도록 이미
자기 안에서 try/except를 하고 있으므로, 원본 값을 그 함수에 그대로
넘겨야 한다(먼저 float()로 바꾸면 그 보호를 우회하게 된다).

pandas float64 컬럼에 None을 넣으면 조용히 NaN으로 바뀌어버려 이 상황을
재현 못 한다 - object dtype 컬럼이어야 None이 그대로 남는다(실 데이터
소스가 혼합 타입으로 들어올 때 실제로 이럴 수 있다).
"""

from __future__ import annotations

import pandas as pd

import backtest


def _df_with_none(none_col: str) -> pd.DataFrame:
    idx = pd.bdate_range("2020-01-02", periods=3)
    data = {
        "시가": [10_000.0, 10_000.0, 10_000.0],
        "고가": [10_100.0, 10_100.0, 10_100.0],
        "저가": [9_900.0, 9_900.0, 9_900.0],
        "종가": [10_000.0, 10_000.0, 10_000.0],
        "거래량": [1_000_000.0, 1_000_000.0, 1_000_000.0],
        "유동성충족": [True, True, True],
    }
    df = pd.DataFrame(data, index=idx)
    df[none_col] = df[none_col].astype(object)
    df.loc[idx[1], none_col] = None
    return df, idx


def test_day_open_none_value_returns_none_without_crashing(monkeypatch):
    df, idx = _df_with_none("시가")
    monkeypatch.setattr(backtest, "_ALL_DFS", {"T": df})

    assert backtest._day_open("T", idx[0]) == 10_000.0
    assert backtest._day_open("T", idx[1]) is None  # 예외 없이 None이어야 한다
    assert backtest._day_open("T", idx[2]) == 10_000.0


def test_day_close_none_value_returns_none_without_crashing(monkeypatch):
    df, idx = _df_with_none("종가")
    monkeypatch.setattr(backtest, "_ALL_DFS", {"T": df})

    assert backtest._day_close("T", idx[0]) == 10_000.0
    assert backtest._day_close("T", idx[1]) is None  # 예외 없이 None이어야 한다
    assert backtest._day_close("T", idx[2]) == 10_000.0
