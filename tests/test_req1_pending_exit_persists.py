"""요구사항 #1 회귀 테스트 - 청산 요청 유지.

체결 실패한 청산 요청이 pending_exits.clear()로 사라지지 않고, 거래
재개일 시가에 정확히 한 번 청산돼야 한다. 특히 거래 재개일에 가격이
"반등"해서 손절선 위로 올라온 경우에도 - 이미 어제 내린 청산 결정을
번복해 계속 보유하거나(오히려 더 나쁘게는) 추가매수를 하면 안 된다.
"""

from __future__ import annotations

import pandas as pd
import pytest

import backtest
import core

from tests.helpers import build_df, calendar_df, flat_row, install_universe, row, warmup_rows

CALENDAR = "005930"
T1 = "T00010"


def _build_halt_then_rebound(all_dates: pd.DatetimeIndex) -> tuple[pd.DataFrame, dict]:
    rows = warmup_rows(80, close=10_000.0, half_spread=300.0)

    breakout_close = 10_480.0
    rows.append(row(breakout_close, 10_600.0, 10_000.0, breakout_close))  # idx 80

    entry_fill_open = 10_500.0
    rows.append(row(entry_fill_open, 10_650.0, 10_400.0, 10_500.0))  # idx 81

    for _ in range(5):
        rows.append(flat_row(10_500.0, half_spread=150.0))  # idx 82..86

    df_so_far = build_df(rows, dates=all_dates[: len(rows)])
    entry_atr = core.round_atr(core.calc_atr(df_so_far))
    fill1 = entry_fill_open * (1 + backtest.SLIPPAGE)
    stop_loss = fill1 - 2 * entry_atr
    crash_close = stop_loss - 3 * entry_atr
    rows.append(row(crash_close, crash_close + 50, crash_close - 50, crash_close))  # idx 87

    n_before_halt = len(rows)
    dates = list(all_dates[:n_before_halt]) + list(all_dates[n_before_halt + 2:])

    # 재개일 - 손절선은 물론 진입가·피라미딩 트리거 레벨보다도 훨씬 위로
    # "반등"한다. 이 가격에서 재평가했다면 추가매수 트리거 조건까지 만족할
    # 정도로 세게 반등시켜, "청산 대기 중엔 추가매수 절대 금지"까지 같이
    # 검증한다.
    rebound_open = fill1 + 10 * entry_atr
    rows.append(row(rebound_open, rebound_open + 100, rebound_open - 100, rebound_open))
    for _ in range(5):
        rows.append(flat_row(rebound_open, half_spread=100.0))

    df = build_df(rows, dates=dates[: len(rows)])
    return df, {
        "entry_atr": entry_atr, "fill1": fill1, "stop_loss": stop_loss,
        "crash_close": crash_close, "rebound_open": rebound_open,
    }


def test_pending_exit_survives_halt_and_fills_despite_rebound(monkeypatch):
    all_dates = pd.bdate_range(start="2020-01-02", periods=260)
    cal_df = calendar_df(start="2020-01-02", periods=260)
    t1_df, info = _build_halt_then_rebound(all_dates)

    install_universe(monkeypatch, {CALENDAR: cal_df, T1: t1_df})

    start = all_dates[0].date()
    end = all_dates[110].date()
    starting_capital = 10_000_000.0
    result = backtest.run_portfolio_backtest(
        [CALENDAR, T1], start, end, starting_capital=starting_capital,
    )

    trades_by_ticker = {t.ticker: t for t in result["trades"]}
    assert T1 in trades_by_ticker, "반등해도 어제 이미 내린 청산 결정을 그대로 체결했어야 한다"
    trade = trades_by_ticker[T1]

    # 재개일(반등한 그날) 시가에 체결됐어야 한다 - 반등가로 "잘" 팔린
    # 것이지, 재판정을 거쳐 다시 산 게 아니다.
    expected_fill = info["rebound_open"] * (1 - backtest.SLIPPAGE)
    expected_proceeds = expected_fill * trade.total_shares * (1 - backtest.COST_RATE)
    assert trade.proceeds == pytest.approx(expected_proceeds)
    assert trade.final_units == 1  # 청산 대기 중 추가매수가 있었다면 2 이상이었을 것

    # 정확히 한 번만 청산됐다 - 같은 종목 트레이드가 중복되지 않는다.
    assert sum(1 for t in result["trades"] if t.ticker == T1) == 1
    assert T1 not in result["open_positions"]
