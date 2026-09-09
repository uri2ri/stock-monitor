"""요구사항 #4 회귀 테스트 - 체결 시 추가매수 재검증.

추가매수 트리거는 어제 종가로 잡히지만 실제 체결은 오늘 시가다. 그 사이
갭다운으로 아직 기준가에 못 미치거나, 갭업으로 추격 허용범위
(PYRAMID_MAX_CHASE_ATR)를 벗어나면 그 체결은 취소하고 오늘 재평가에
맡겨야 한다 - 어제 판단을 그대로 밀어붙여 나쁜 가격에 체결하면 안 된다.
"""

from __future__ import annotations

import pandas as pd
import pytest

import backtest
import core

from tests.helpers import build_df, calendar_df, flat_row, install_universe, row, warmup_rows

CALENDAR = "005930"


def _entry_setup(rows, all_dates):
    """80일 워밍업 + 돌파 + 체결까지 공통 준비. (entry_atr, step, fill1) 반환."""
    breakout_close = 10_480.0
    rows.append(row(breakout_close, 10_600.0, 10_000.0, breakout_close))  # idx 80
    df_thru_breakout = build_df(rows, dates=all_dates[: len(rows)])
    entry_atr = core.round_atr(core.calc_atr(df_thru_breakout))
    step = core.PYRAMID_ATR_STEP * entry_atr

    entry_fill_open = 10_500.0
    rows.append(row(entry_fill_open, 10_650.0, 10_400.0, 10_500.0))  # idx 81
    fill1 = entry_fill_open * (1 + backtest.SLIPPAGE)
    for _ in range(5):
        rows.append(flat_row(10_500.0, half_spread=150.0))  # idx 82..86
    return entry_atr, step, fill1


def test_gap_down_cancels_add_and_reevaluates_same_day(monkeypatch):
    T = "T00020"
    all_dates = pd.bdate_range(start="2020-01-02", periods=200)
    cal_df = calendar_df(start="2020-01-02", periods=200)

    rows = warmup_rows(80, close=10_000.0, half_spread=300.0)
    entry_atr, step, fill1 = _entry_setup(rows, all_dates)
    next_price = fill1 + step

    trigger_close = next_price + 2.0
    rows.append(row(trigger_close, trigger_close + 100, trigger_close - 100,
                     trigger_close))  # idx 87 - 트리거일

    # 체결 예정일 - 갭다운으로 기준가 밑에서 시가가 열린다(체결 취소돼야
    # 함). 종가는 다시 기준가 위로 회복시켜 "취소 후 같은 날 재평가"까지
    # 확인한다.
    gap_down_open = next_price - 50.0
    recovered_close = next_price + 60.0
    rows.append(row(gap_down_open, recovered_close + 50, gap_down_open - 50,
                     recovered_close))  # idx 88

    # 실제 체결일 - 재평가로 다시 잡힌 트리거가 여기서 체결된다.
    real_fill_open = recovered_close
    rows.append(row(real_fill_open, real_fill_open + 100, real_fill_open - 100,
                     real_fill_open))  # idx 89
    for _ in range(5):
        rows.append(flat_row(real_fill_open, half_spread=100.0))

    t_df = build_df(rows, dates=all_dates[: len(rows)])
    install_universe(monkeypatch, {CALENDAR: cal_df, T: t_df})

    start = all_dates[0].date()
    end = all_dates[100].date()
    result = backtest.run_portfolio_backtest(
        [CALENDAR, T], start, end, starting_capital=10_000_000.0,
    )

    assert result["trades"] == []
    pos = result["open_positions"][T]
    assert pos.num_units == 2, "갭다운 취소 후 회복일에 재평가돼 결국 2유닛이 됐어야 한다"

    fill2 = pos.units[1].buy_price
    cancelled_fill = gap_down_open * (1 + backtest.SLIPPAGE)
    real_fill = real_fill_open * (1 + backtest.SLIPPAGE)
    assert fill2 == pytest.approx(real_fill)
    assert fill2 != pytest.approx(cancelled_fill)


def test_gap_up_beyond_chase_cancels_and_reanchors_at_fill_time(monkeypatch):
    T = "T00021"
    all_dates = pd.bdate_range(start="2020-01-02", periods=200)
    cal_df = calendar_df(start="2020-01-02", periods=200)

    rows = warmup_rows(80, close=10_000.0, half_spread=300.0)
    entry_atr, step, fill1 = _entry_setup(rows, all_dates)
    next_price = fill1 + step

    trigger_close = next_price + 2.0
    rows.append(row(trigger_close, trigger_close + 100, trigger_close - 100,
                     trigger_close))  # idx 87 - 트리거일(추격범위 안, 정상 승인)

    # 체결 예정일 - 밤새 추격 허용범위(PYRAMID_MAX_CHASE_ATR × ATR)를 훨씬
    # 넘는 갭업 시가로 열린다. 이 갭은 트리거 평가 시점(어제 종가)엔 전혀
    # 안 보였던 것이므로, 체결 직전 재검증이 없다면 그대로 나쁜 가격에
    # 사버리게 된다.
    chase_limit = backtest.PYRAMID_MAX_CHASE_ATR * entry_atr
    gap_up_open = next_price + chase_limit + 3 * step  # 허용범위를 한참 초과
    rows.append(row(gap_up_open, gap_up_open + 100, gap_up_open - 100,
                     gap_up_open))  # idx 88
    for _ in range(5):
        rows.append(flat_row(gap_up_open, half_spread=100.0))

    t_df = build_df(rows, dates=all_dates[: len(rows)])
    install_universe(monkeypatch, {CALENDAR: cal_df, T: t_df})

    start = all_dates[0].date()
    end = all_dates[100].date()
    result = backtest.run_portfolio_backtest(
        [CALENDAR, T], start, end, starting_capital=10_000_000.0,
    )

    assert result["trades"] == []
    pos = result["open_positions"][T]
    assert pos.num_units == 1, "추격 허용범위를 벗어난 갭업이면 그 가격에 사면 안 된다"
    # 대신 기준가만 재기준됐어야 한다(다음 창을 여는 정상 동작).
    assert pos.pyramid_anchor is not None
    assert pos.pyramid_anchor > next_price
