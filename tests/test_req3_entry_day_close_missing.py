"""요구사항 #3 회귀 테스트 - 평가액 일관성.

진입 체결일 당일 종가가 없으면(휴장 등) 그 포지션을 0원 취급하지 않고
실제 진입 체결가를 대체 기준으로 써야 한다. 그리고 그 규칙은 일별
평가(equity_curve)와 최종 평가(open_positions_value/final_value)에 똑같이
적용돼야 한다.

진입일 자체의 종가만 무효로 만들고(고가·저가는 신경 쓰지 않는다 - 이
포지션에 대한 evaluate_holding 재평가는 시뮬레이션이 그날로 끝나 다시
호출되지 않는다), 시뮬레이션을 그 체결일에서 바로 끝낸다 - 그 뒤로도
종가가 무효인 채로 며칠 더 진행하면 core.calc_atr()의 Wilder 재귀식이
그 시점부터 영구히 NaN이 되는(core.py 자체의 별도 한계, 이번 수정
범위 밖) 문제와 섞여 이 테스트가 확인하려는 것과 무관한 예외가 난다.
"""

from __future__ import annotations

import pandas as pd
import pytest

import backtest

from tests.helpers import build_df, calendar_df, install_universe, row, warmup_rows

CALENDAR = "005930"
T3 = "T00012"


def _build_entry_day_no_close(all_dates: pd.DatetimeIndex) -> tuple[pd.DataFrame, dict]:
    rows = warmup_rows(80, close=10_000.0, half_spread=300.0)

    breakout_close = 10_480.0
    rows.append(row(breakout_close, 10_600.0, 10_000.0, breakout_close))  # idx 80 - 신호일

    entry_fill_open = 10_500.0
    # 체결(시가)은 정상, 종가만 무효 - 이 날이 곧 체결일이자 시뮬레이션의
    # 마지막 날이다.
    rows.append(row(entry_fill_open, 10_650.0, 10_400.0, float("nan")))  # idx 81 - 체결일

    df = build_df(rows, dates=all_dates[: len(rows)])
    entry_fill_date = all_dates[len(rows) - 1]
    return df, {"entry_fill_open": entry_fill_open, "entry_fill_date": entry_fill_date}


def test_entry_day_missing_close_falls_back_to_fill_price(monkeypatch):
    all_dates = pd.bdate_range(start="2020-01-02", periods=200)
    cal_df = calendar_df(start="2020-01-02", periods=200)
    t3_df, info = _build_entry_day_no_close(all_dates)

    install_universe(monkeypatch, {CALENDAR: cal_df, T3: t3_df})

    start = all_dates[0].date()
    end = info["entry_fill_date"].date()
    starting_capital = 10_000_000.0
    result = backtest.run_portfolio_backtest(
        [CALENDAR, T3], start, end, starting_capital=starting_capital,
    )

    assert T3 in result["open_positions"], "체결일 종가가 없어도 진입 자체는 됐어야 한다"
    pos = result["open_positions"][T3]

    fill = info["entry_fill_open"] * (1 + backtest.SLIPPAGE)
    total_needed = fill * pos.unit_shares * (1 + backtest.COST_RATE)
    expected_cash = starting_capital - total_needed
    expected_position_value = fill * pos.total_shares  # 체결가를 대체 기준으로

    # 일별 평가(equity_curve의 마지막 - 체결 당일) 확인.
    equity_by_date = dict(result["equity_curve"])
    last_equity = equity_by_date[info["entry_fill_date"].date()]
    assert last_equity == pytest.approx(expected_cash + expected_position_value)
    assert last_equity != pytest.approx(expected_cash)  # 포지션이 0원 취급되지 않았다

    # 최종 평가(open_positions_value/final_value)도 완전히 같은 규칙이어야 한다.
    assert result["open_positions_value"] == pytest.approx(expected_position_value)
    assert result["final_value"] == pytest.approx(expected_cash + expected_position_value)
    assert result["final_value"] == pytest.approx(last_equity)
