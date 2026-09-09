"""Bug #2 회귀 테스트 - 보유 종목의 당일 시세가 없으면(휴장·데이터 공백) 그날
계좌평가액(equity_curve)에서 그 종목 가치가 0원 취급되던 버그.

수정 전:

    for ticker, pos in positions.items():
        c = _day_close(ticker, ts)
        if c is not None:
            mtm_equity += c * pos.total_shares
    equity_curve.append((today, mtm_equity))

시세가 없는 날은 그냥 더하지 않고 넘어간다 - 보유 중인 자산이 그날 하루
증발한 것처럼 계좌평가액이 실제보다 낮게 찍힌다(포지션 크기·유닛캡 계산의
기준이 되는 account_size에도 그대로 반영돼 그날의 게이트 판단까지 왜곡한다).
수정 후에는 마지막으로 확인된 종가를 그대로 들고 간다.

청산 신호와는 무관한 시나리오로 만든다(Bug #1과 섞이지 않도록) - 진입 후
주가는 계속 건강하게 유지되고, 딱 하루만 시세 공백을 준다.
"""

from __future__ import annotations

import pandas as pd
import pytest

import backtest

from tests.helpers import build_df, calendar_df, flat_row, install_universe, row, warmup_rows

CALENDAR = "005930"
T2 = "T00002"


def _build_t2_with_gap(all_dates: pd.DatetimeIndex) -> tuple[pd.DataFrame, dict]:
    n_warmup = 80
    rows = warmup_rows(n_warmup, close=10_000.0, half_spread=300.0)

    breakout_close = 10_480.0
    rows.append(row(breakout_close, 10_600.0, 10_000.0, breakout_close))  # idx 80

    entry_fill_open = 10_500.0
    rows.append(row(entry_fill_open, 10_650.0, 10_400.0, 10_500.0))  # idx 81, 체결일

    # 진입 후 건강하게 유지 - 청산 신호가 절대 나오지 않게 완만히 상승만 시킨다.
    last_close = 10_500.0
    for _ in range(5):
        last_close += 20.0
        rows.append(flat_row(last_close, half_spread=150.0))

    n_before_gap = len(rows)  # 이 날짜가 "공백 직전 마지막 확인 종가"가 된다
    last_known_close = last_close

    # 공백 직전 날짜들 + 하루를 통째로 건너뛴 날짜들.
    dates = list(all_dates[:n_before_gap]) + list(all_dates[n_before_gap + 1:])

    # 공백 뒤 재개 - 계속 건강하게.
    for _ in range(10):
        last_close += 20.0
        rows.append(flat_row(last_close, half_spread=150.0))

    df = build_df(rows, dates=dates[: len(rows)])
    gap_date = all_dates[n_before_gap]
    return df, {
        "last_known_close": last_known_close,
        "gap_date": gap_date,
        "entry_fill_open": entry_fill_open,
    }


def test_equity_curve_carries_last_known_close_through_quote_gap(monkeypatch):
    all_dates = pd.bdate_range(start="2020-01-02", periods=200)
    cal_df = calendar_df(start="2020-01-02", periods=200)
    t2_df, info = _build_t2_with_gap(all_dates)

    install_universe(monkeypatch, {CALENDAR: cal_df, T2: t2_df})

    start = all_dates[0].date()
    end = all_dates[100].date()
    starting_capital = 10_000_000.0
    result = backtest.run_portfolio_backtest(
        [CALENDAR, T2], start, end, starting_capital=starting_capital,
    )

    # 시세 공백일은 결코 청산되지 않았어야 한다(이 테스트는 Bug #2만 본다).
    assert result["trades"] == []
    assert T2 in result["open_positions"]
    pos = result["open_positions"][T2]

    equity_by_date = {d: v for d, v in result["equity_curve"]}
    gap_date = info["gap_date"].date()
    assert gap_date in equity_by_date, "공백일도 sim_days엔 포함돼 equity_curve에 한 줄 있어야 한다"

    fill = info["entry_fill_open"] * (1 + backtest.SLIPPAGE)
    total_needed = fill * pos.unit_shares * (1 + backtest.COST_RATE)
    cash_after_entry = starting_capital - total_needed
    expected_equity = cash_after_entry + info["last_known_close"] * pos.total_shares

    actual_equity = equity_by_date[gap_date]
    # 버그가 있었다면 이 값은 cash_after_entry(포지션 가치 0원 취급)에
    # 가까웠을 것이다 - last_known_close를 반영한 값과는 종목 시가총액만큼
    # 차이가 난다.
    assert actual_equity == pytest.approx(expected_equity, rel=1e-6)
    assert actual_equity != pytest.approx(cash_after_entry, rel=1e-6)
