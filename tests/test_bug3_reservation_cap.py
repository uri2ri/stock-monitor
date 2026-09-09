"""Bug #3 회귀 테스트 - 같은 날 동시에 추가매수 트리거를 맞은 두 종목이
서로의 "아직 미체결" 상태를 못 보고 각자 상관군 캡을 통과해버려, 다음날
둘 다 체결되면 합계가 캡을 넘던 버그.

수정 전 4)번(피라미딩 트리거) 블록은 상관군 유닛수를 매번 positions에서
그대로 다시 세었다:

    group_units = sum(p.num_units for t2, p in positions.items() if ...)
    if group_units + 1 > core.MAX_UNITS_GROUP:
        ...거부...
    else:
        pending_adds[ticker] = {}   # <- 아직 positions는 안 바뀐다(체결은 내일)

같은 그룹 두 종목이 같은 날 이 블록에 들어오면, 둘 다 "지금 그룹 유닛수 +1"만
보고 캡을 통과해 둘 다 pending_adds에 들어간다 - 다음날 둘 다 체결되면 실제
합계는 +2가 된다. 수정 후에는 4)번 안에서 running 카운터를 두고 승인할
때마다 즉시 반영해, 같은 날 안에서도 두 번째 이후 후보는 첫 번째가 이미
"예약"한 몫까지 보고 판단한다.
"""

from __future__ import annotations

import pandas as pd
import pytest

import backtest
import core

from tests.helpers import build_df, calendar_df, flat_row, install_universe, row, warmup_rows

CALENDAR = "005930"
T3A = "T00003"
T3B = "T00004"
SECTOR = "SEC1"


def _build_twin_ticker(all_dates: pd.DatetimeIndex) -> tuple[pd.DataFrame, dict]:
    n_warmup = 80
    rows = warmup_rows(n_warmup, close=10_000.0, half_spread=300.0)

    breakout_close = 10_480.0
    rows.append(row(breakout_close, 10_600.0, 10_000.0, breakout_close))  # idx 80

    # 실제 진입 신호(_entry_signal_for)는 돌파일(idx80)까지의 데이터만 보고
    # atr을 계산한다 - 그 이후 며칠(체결일 포함)을 앞서 붙여넣고 계산하면
    # 다른 atr이 나와 트리거가 하루도 안 어긋나는 정밀 시나리오가 깨진다.
    df_thru_breakout = build_df(rows, dates=all_dates[: len(rows)])
    entry_atr = core.round_atr(core.calc_atr(df_thru_breakout))

    entry_fill_open = 10_500.0
    rows.append(row(entry_fill_open, 10_650.0, 10_400.0, 10_500.0))  # idx 81

    for _ in range(5):
        rows.append(flat_row(10_500.0, half_spread=150.0))  # idx 82..86

    fill1 = entry_fill_open * (1 + backtest.SLIPPAGE)
    step = core.PYRAMID_ATR_STEP * entry_atr
    next_price = fill1 + step

    trigger_close = next_price + 2.0  # 경계값 근처 부동소수 오차를 피하려는 여유
    rows.append(row(trigger_close, trigger_close + 100, trigger_close - 100, trigger_close))  # idx 87

    for _ in range(10):
        rows.append(flat_row(trigger_close, half_spread=150.0))

    df = build_df(rows, dates=all_dates[: len(rows)])
    return df, {"entry_atr": entry_atr, "fill1": fill1, "next_price": next_price}


def test_simultaneous_pyramid_adds_respect_group_cap(monkeypatch):
    all_dates = pd.bdate_range(start="2020-01-02", periods=200)
    cal_df = calendar_df(start="2020-01-02", periods=200)
    a_df, info = _build_twin_ticker(all_dates)
    b_df, _ = _build_twin_ticker(all_dates)  # 완전히 동일한 가격 흐름

    install_universe(
        monkeypatch,
        {CALENDAR: cal_df, T3A: a_df, T3B: b_df},
        sector_map={T3A: SECTOR, T3B: SECTOR},
    )

    start = all_dates[0].date()
    end = all_dates[100].date()
    result = backtest.run_portfolio_backtest(
        [CALENDAR, T3A, T3B], start, end,
        starting_capital=10_000_000.0,
        unit_caps=(4, 3, 100),  # 종목당 4 / 상관군 3 / 전체 무제한(사실상)
    )

    open_positions = result["open_positions"]
    assert T3A in open_positions and T3B in open_positions

    total_group_units = open_positions[T3A].num_units + open_positions[T3B].num_units
    assert total_group_units <= 3, (
        f"상관군캡(3)을 넘겼다: T3A={open_positions[T3A].num_units}유닛, "
        f"T3B={open_positions[T3B].num_units}유닛, 합계={total_group_units}"
    )
    # 두 종목 다 이미 1유닛씩(합계 2) 보유한 상태에서 트리거를 맞았으니, 캡이
    # 제대로 걸렸다면 정확히 한 종목만 추가매수가 승인돼야 한다(합계 3).
    assert total_group_units == 3
    assert result["rejections"]["상관군캡"] >= 1
