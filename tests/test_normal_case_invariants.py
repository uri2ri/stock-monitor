"""정상 케이스 회귀 - 4개 버그 시나리오 어느 것에도 해당 안 되는 평범한
진입->청산 흐름이 여전히 잘 도는지, 그리고 회계 불변식이 항상 성립하는지
확인한다.

불변식:
    1. final_value(반환값) == final_cash + open_positions_value
    2. 거래 하나하나의 net_pnl 합계가, 그 거래들로 인한 실제 현금흐름
       변화(청산대금 합 - 매수원가 합)와 일치한다.
    3. 같은 종목이 trades에 두 번 이상 겹쳐 청산(중복 청산) 나오지 않는다.
"""

from __future__ import annotations

import pandas as pd
import pytest

import backtest

from tests.helpers import build_df, calendar_df, flat_row, install_universe, row

CALENDAR = "005930"
T5 = "T00006"


def _build_plain_round_trip(all_dates: pd.DatetimeIndex) -> pd.DataFrame:
    rows = []
    for _ in range(80):
        rows.append(flat_row(10_000.0, half_spread=300.0))  # idx 0..79

    rows.append(row(10_480.0, 10_600.0, 10_000.0, 10_480.0))  # idx 80 - 돌파
    rows.append(row(10_500.0, 10_650.0, 10_400.0, 10_500.0))  # idx 81 - 진입 체결

    for _ in range(10):
        rows.append(flat_row(10_500.0, half_spread=150.0))  # idx 82..91 - 평온

    # 급락 -> 손절 청산
    for i in range(5):
        c = 10_500.0 - (i + 1) * 500.0
        rows.append(row(c, c + 50, c - 50, c))  # idx 92..96

    for _ in range(10):
        rows.append(flat_row(8_000.0, half_spread=150.0))  # idx 97..106 - 청산 후 평온

    return build_df(rows, dates=all_dates[: len(rows)])


def test_plain_entry_exit_round_trip_balances(monkeypatch):
    all_dates = pd.bdate_range(start="2020-01-02", periods=200)
    cal_df = calendar_df(start="2020-01-02", periods=200)
    t5_df = _build_plain_round_trip(all_dates)

    install_universe(monkeypatch, {CALENDAR: cal_df, T5: t5_df})

    start = all_dates[0].date()
    end = all_dates[130].date()
    starting_capital = 10_000_000.0
    result = backtest.run_portfolio_backtest(
        [CALENDAR, T5], start, end, starting_capital=starting_capital,
    )

    trades = result["trades"]
    assert len(trades) == 1
    trade = trades[0]
    assert trade.ticker == T5
    assert trade.exit_reason  # 손절 사유 메모가 채워져 있어야 한다
    assert trade.net_pnl < 0  # 급락 후 손절이니 손실이어야 정상

    # 불변식 1: final_value == final_cash + open_positions_value
    assert result["final_value"] == pytest.approx(
        result["final_cash"] + result["open_positions_value"]
    )
    # 이 시나리오는 청산 후 재진입이 없으므로 최종 포지션이 비어 있어야 한다.
    assert result["open_positions"] == {}
    assert result["open_positions_value"] == 0.0

    # 불변식 2: 거래별 net_pnl 합 == 실제 현금흐름 변화
    total_net_pnl = sum(t.net_pnl for t in trades)
    total_cash_delta = sum(t.proceeds - t.total_invested for t in trades)
    assert total_net_pnl == pytest.approx(total_cash_delta)
    assert result["final_cash"] == pytest.approx(starting_capital + total_cash_delta)

    # 불변식 3: 같은 종목이 두 번 청산되지 않는다(중복 청산 없음).
    tickers_seen = [t.ticker for t in trades]
    assert len(tickers_seen) == len(set(tickers_seen))
