"""요구사항 #2 회귀 테스트 - 가격 유효성.

행 자체가 없는(halt) 경우는 Bug #1 테스트가 이미 다룬다. 여기서는 행은
있지만 시가·종가가 NaN·0·음수·거래량 0(명시적 거래 불가)인 날들을 연달아
섞어서, 그런 날엔 신규 진입·추가매수·청산 어느 쪽도 체결되지 않고, 최초로
유효한 가격이 나온 날에만 정확히 체결됨을 확인한다.
"""

from __future__ import annotations

import pandas as pd
import pytest

import backtest
import core

from tests.helpers import build_df, calendar_df, install_universe, row, warmup_rows

CALENDAR = "005930"
T2 = "T00011"


def _build_invalid_price_days(all_dates: pd.DatetimeIndex) -> tuple[pd.DataFrame, dict]:
    rows = warmup_rows(80, close=10_000.0, half_spread=300.0)

    breakout_close = 10_480.0
    rows.append(row(breakout_close, 10_600.0, 10_000.0, breakout_close))  # idx 80

    entry_fill_open = 10_500.0
    rows.append(row(entry_fill_open, 10_650.0, 10_400.0, 10_500.0))  # idx 81
    fill1 = entry_fill_open * (1 + backtest.SLIPPAGE)

    for _ in range(5):
        rows.append(row(10_500.0, 10_650.0, 10_400.0, 10_500.0))  # idx 82..86

    df_so_far = build_df(rows, dates=all_dates[: len(rows)])
    entry_atr = core.round_atr(core.calc_atr(df_so_far))
    stop_loss = fill1 - 2 * entry_atr
    crash_close = stop_loss - 3 * entry_atr
    rows.append(row(crash_close, crash_close + 50, crash_close - 50, crash_close))  # idx 87 - 손절 트리거

    # 체결(시가)만 무효로 만들고 고가·저가·종가는 정상 유지한다 - 체결은
    # 항상 시가로만 이뤄지므로(_day_open) 이게 실제 체결 가능 여부를 가른다.
    # 종가까지 NaN으로 만들면 core.calc_atr()의 Wilder 재귀식이 그 이후
    # 영원히 NaN으로 오염돼(실제 이 저장소 데이터 구조에서도 일어날 수 없는
    # 인위적 상황) 이 테스트의 목적(체결 유효성)과 무관한 별도 크래시가 난다.
    last_c = crash_close
    rows.append(row(float("nan"), last_c + 50, last_c - 50, last_c))         # idx 88 - 시가 NaN
    rows.append(row(0.0, last_c + 50, last_c - 50, last_c))                   # idx 89 - 시가 0
    rows.append(row(-100.0, last_c + 50, last_c - 50, last_c))               # idx 90 - 시가 음수
    rows.append(row(last_c, last_c + 50, last_c - 50, last_c, v=0.0))        # idx 91 - 거래량 0(명시적 거래 불가)

    resume_open = crash_close - 10
    rows.append(row(resume_open, resume_open + 50, resume_open - 50, resume_open))  # idx 92 - 첫 유효일
    for _ in range(5):
        rows.append(row(resume_open, resume_open + 50, resume_open - 50, resume_open))

    df = build_df(rows, dates=all_dates[: len(rows)])
    return df, {"fill1": fill1, "entry_atr": entry_atr, "resume_open": resume_open}


def test_invalid_prices_are_never_filled_and_exit_resumes_at_first_valid_day(monkeypatch):
    all_dates = pd.bdate_range(start="2020-01-02", periods=200)
    cal_df = calendar_df(start="2020-01-02", periods=200)
    t2_df, info = _build_invalid_price_days(all_dates)

    install_universe(monkeypatch, {CALENDAR: cal_df, T2: t2_df})

    start = all_dates[0].date()
    end = all_dates[110].date()
    starting_capital = 10_000_000.0
    result = backtest.run_portfolio_backtest(
        [CALENDAR, T2], start, end, starting_capital=starting_capital,
    )

    trades_by_ticker = {t.ticker: t for t in result["trades"]}
    assert T2 in trades_by_ticker, "NaN·0·음수·거래량0인 날들을 다 건너뛰고 결국 체결됐어야 한다"
    trade = trades_by_ticker[T2]

    expected_fill = info["resume_open"] * (1 - backtest.SLIPPAGE)
    expected_proceeds = expected_fill * trade.total_shares * (1 - backtest.COST_RATE)
    assert trade.proceeds == pytest.approx(expected_proceeds), (
        "무효한 가격이 하루라도 체결에 쓰였다면 이 값과 어긋난다"
    )
    assert sum(1 for t in result["trades"] if t.ticker == T2) == 1
