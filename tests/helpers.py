"""장중 자동매수 파이프라인(scan_all.py/intraday_watch.py/kis_client.py)
회귀 테스트 공용 헬퍼. 실 네트워크(KRX/KIS/노션/카카오/네이버) 호출 없이
합성 데이터 + monkeypatch로만 검증한다.
"""

from __future__ import annotations

from datetime import date

import pandas as pd

import screener


def mock_trading_days(monkeypatch, expected_date: date) -> None:
    """screener.trading_days()를 '직전 거래일 = expected_date' 하나로 고정한다.

    intraday_watch._expected_trading_day()는 screener.trading_days(끝날짜, 1)로
    딱 하나만 요청하고 리스트의 마지막 원소만 쓴다 - 실제 KRX 호출 없이
    (원격 환경 제약) 이 반환값만 통제하면 신선도 판정을 원하는 대로
    재현할 수 있다. 어떤 (end, count)로 불려도 같은 값을 준다 - 이
    헬퍼를 쓰는 테스트들은 달력 자체의 범위 로직이 아니라 "직전 거래일이
    이 날짜다"라는 결과만 필요로 하기 때문이다(범위·휴장 로직 자체를
    검증하는 테스트는 test_bug4에서 별도로 직접 monkeypatch한다).
    """
    monkeypatch.setattr(
        screener, "trading_days",
        lambda end, count: [expected_date.strftime("%Y%m%d")],
    )


def make_hist(n_flat: int = 20, flat_close: float = 10_000.0,
              half_spread: float = 300.0, last_close: float = 10_500.0,
              last_high: float = 10_600.0, last_low: float = 10_000.0,
              volume: float = 5_000_000.0) -> pd.DataFrame:
    """scan_row()가 기대하는 형태(날짜·티커 없이 시가/고가/저가/종가/거래량만)의
    합성 OHLCV. 마지막 한 행만 당일 고가가 그 전 20일 고가보다 높게
    잡아, high_20(당일 포함)과 high_20_prev(당일 제외)가 서로 다른
    값으로 갈리게 한다 - 이게 bug1(high20 vs high20_next)의 핵심 조건.
    """
    rows = []
    for _ in range(n_flat):
        rows.append({
            "시가": flat_close, "고가": flat_close + half_spread,
            "저가": flat_close - half_spread, "종가": flat_close,
            "거래량": volume,
        })
    rows.append({
        "시가": last_close, "고가": last_high, "저가": last_low,
        "종가": last_close, "거래량": volume,
    })
    idx = pd.bdate_range("2020-01-02", periods=len(rows))
    return pd.DataFrame(rows, index=idx)
