"""버그 #1 회귀 테스트 - 다음 거래일 기준선이 하루 밀려 있던 문제.

scan_all.py가 high20(스캔 당일을 뺀 20일 고가)만 저장하고 그걸 그대로
다음 거래일 장중 판정에 쓰면, 스캔 당일 크게 오른 종목일수록 기준선이
실제보다 낮게 잡혀 가짜 돌파를 더 사게 된다. high20_next(당일 포함
20일 고가)를 별도로 저장하고 intraday_watch.judge()가 그걸 쓰도록
고쳤다.

검증:
  1) scan_all.scan_row()가 만든 행에서 high20과 high20_next가 서로
     다른 값으로 들어가는지(당일 자체가 새 고점을 찍은 시나리오).
  2) intraday_watch.judge()가 high20_next를 기준으로 판정하는지 -
     "당일 제외 20일 고가는 넘었지만 당일 포함 20일 고가는 아직
     안 넘은" 가격에서 예전 기준(high20)이면 돌파로 잘못 판정하고,
     새 기준(high20_next)이면 아직 돌파가 아니라고 정확히 판정하는지.
  3) high20_next 컬럼이 없는 구버전 scan_latest.csv 행에서는 경고를
     남기고 high20으로 대체(fallback)하는지.
"""

from __future__ import annotations

import pandas as pd
import pytest

import scan_all
import intraday_watch

from tests.helpers import make_hist


def test_scan_row_stores_distinct_high20_and_high20_next():
    hist = make_hist()  # 마지막 날 고가(10600)가 그 전 20일 고가(10300)보다 높다
    row = scan_all.scan_row(
        "005930", hist, name="삼성전자", market="KOSPI", sector="전기전자",
        capital=10_000_000.0, scan_day="20200203",
    )
    assert row is not None
    assert row["high20"] == 10300          # 당일(마지막 행) 제외 20일 고가
    assert row["high20_next"] == 10600     # 당일 포함 20일 고가
    assert row["high20"] != row["high20_next"]
    assert "high20_next" in scan_all.SCAN_COLUMNS


def test_judge_uses_high20_next_not_high20():
    # high20(옛 기준, 하루 밀림) = 10300, high20_next(새 기준) = 10600.
    # 가격 10400은 high20은 넘지만 high20_next는 아직 안 넘는다 -
    # 옛 기준이면 "돌파"로 잘못 판정하고, 새 기준이면 아직 돌파가 아니다.
    row = pd.Series({
        "ticker": "005930", "name": "삼성전자", "sector": "전기전자",
        "market": "KOSPI", "atr20": 500.0,
        "high20": 10_300.0, "high20_next": 10_600.0,
        "unit_shares": 10,
    })
    assert intraday_watch.judge(row, 10_400.0) is None, (
        "high20_next(10600)를 기준으로 삼아야 하므로 10400은 아직 돌파가 아니다"
    )
    hit = intraday_watch.judge(row, 10_650.0)
    assert hit is not None
    assert hit["high20"] == 10_600.0


def test_judge_falls_back_to_high20_when_high20_next_missing(caplog):
    # high20_next 컬럼이 없는 구버전 scan_latest.csv 행.
    row = pd.Series({
        "ticker": "005930", "name": "삼성전자", "sector": "전기전자",
        "market": "KOSPI", "atr20": 500.0,
        "high20": 10_300.0,
        "unit_shares": 10,
    })
    with caplog.at_level("WARNING"):
        hit = intraday_watch.judge(row, 10_400.0)
    assert hit is not None
    assert hit["high20"] == 10_300.0
    assert any("high20_next" in rec.message for rec in caplog.records)
