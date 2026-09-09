"""Bug #4 회귀 테스트 - 추가매수 기준가가 진입가에 고정된 그리드였던 버그.

수정 전에는 진입 체결 시점에 core.build_pyramid(fill, atr, ...)로 전체
유닛의 매수가를 한 번에 다 정해서 pos.pyramid_plan에 고정해뒀다. 2유닛이
슬리피지·갭으로 "이론가"보다 훨씬 비싼 가격에 실제 체결돼도, 3유닛째
기준가는 여전히 "1유닛 체결가 + 2×step"이라는 원래 그리드값 그대로였다 -
kis_client.py 운영 로직(_pyramid_plan)은 반대로 "직전 실제 체결가"를
기준으로 다음 레벨을 잡는데, 백테스트만 다르게 동작했던 것이다.

이 테스트는 2유닛이 갭업으로 이론가보다 훨씬 비싸게 체결되게 만든 뒤,
"옛 고정 그리드로는 이미 트리거됐을 가격, 실제 체결가 기준 새 앵커로는
아직 트리거 안 될 가격" 구간에 3유닛 트리거를 놓아 두 로직의 차이를
정확히 가른다.
"""

from __future__ import annotations

import pandas as pd
import pytest

import backtest
import core

from tests.helpers import build_df, calendar_df, flat_row, install_universe, row

CALENDAR = "005930"
T4 = "T00005"


def _build_t4(all_dates: pd.DatetimeIndex) -> tuple[pd.DataFrame, dict]:
    rows = []
    for _ in range(80):
        rows.append(flat_row(10_000.0, half_spread=300.0))  # idx 0..79

    breakout_close = 10_480.0
    rows.append(row(breakout_close, 10_600.0, 10_000.0, breakout_close))  # idx 80

    df_thru_breakout = build_df(rows, dates=all_dates[: len(rows)])
    entry_atr = core.round_atr(core.calc_atr(df_thru_breakout))
    step = core.PYRAMID_ATR_STEP * entry_atr

    entry_fill_open = 10_500.0
    rows.append(row(entry_fill_open, 10_650.0, 10_400.0, 10_500.0))  # idx 81 - 1유닛 체결
    fill1 = entry_fill_open * (1 + backtest.SLIPPAGE)

    for _ in range(5):
        rows.append(flat_row(10_500.0, half_spread=150.0))  # idx 82..86

    unit2_trigger_price = fill1 + step
    trigger2_close = unit2_trigger_price + 2.0
    rows.append(row(trigger2_close, trigger2_close + 100, trigger2_close - 100,
                     trigger2_close))  # idx 87 - 2유닛 트리거일

    # 2유닛 체결일 - 큰 갭업 시가로 체결시켜 "이론 트리거가"와 "실제 체결가"를
    # 크게 벌린다(현실의 슬리피지·갭상승을 과장한 것).
    gap = 3 * step
    unit2_fill_open = trigger2_close + gap
    rows.append(row(unit2_fill_open, unit2_fill_open + 100, unit2_fill_open - 100,
                     unit2_fill_open))  # idx 88
    fill2 = unit2_fill_open * (1 + backtest.SLIPPAGE)

    # 옛 고정 그리드 기준 3유닛 트리거가(진입가 기준) - 이 값보다 살짝 위,
    # 그러나 "실제 체결가(fill2) 기준 새 앵커" 트리거보다는 한참 아래인
    # 구간에 며칠을 눌러 둔다. 옛 로직이면 여기서 이미 3유닛째가 나갔어야
    # 한다.
    old_grid_unit3_trigger = fill1 + 2 * step
    probe_close = old_grid_unit3_trigger + 3.0
    for _ in range(6):
        rows.append(flat_row(probe_close, half_spread=80.0))  # idx 89..94

    # 실제 체결가(fill2) 기준 새 앵커의 3유닛 트리거가 - 여기 도달해야만
    # (고정 그리드가 아니라) 새 로직에서도 3유닛째가 나가야 한다.
    new_anchor_unit3_trigger = fill2 + step
    trigger3_close = new_anchor_unit3_trigger + 2.0
    rows.append(row(trigger3_close, trigger3_close + 100, trigger3_close - 100,
                     trigger3_close))  # idx 95 - 3유닛 트리거일(새 앵커 기준)

    unit3_fill_open = trigger3_close
    rows.append(row(unit3_fill_open, unit3_fill_open + 100, unit3_fill_open - 100,
                     unit3_fill_open))  # idx 96 - 3유닛 체결일

    for _ in range(5):
        rows.append(flat_row(unit3_fill_open, half_spread=80.0))  # idx 97..101

    df = build_df(rows, dates=all_dates[: len(rows)])
    return df, {
        "entry_atr": entry_atr,
        "step": step,
        "fill1": fill1,
        "fill2": fill2,
        "old_grid_unit3_trigger": old_grid_unit3_trigger,
        "probe_close": probe_close,
        "new_anchor_unit3_trigger": new_anchor_unit3_trigger,
        "probe_day_index": 89,
        "trigger3_day_index": 95,
    }


def test_pyramid_anchor_follows_last_actual_fill_not_entry_grid(monkeypatch):
    all_dates = pd.bdate_range(start="2020-01-02", periods=200)
    cal_df = calendar_df(start="2020-01-02", periods=200)
    t4_df, info = _build_t4(all_dates)

    install_universe(monkeypatch, {CALENDAR: cal_df, T4: t4_df})

    start = all_dates[0].date()
    end = all_dates[110].date()
    result = backtest.run_portfolio_backtest(
        [CALENDAR, T4], start, end, starting_capital=10_000_000.0,
    )

    assert result["trades"] == []
    assert T4 in result["open_positions"]
    pos = result["open_positions"][T4]

    # 옛 고정 그리드 기준으로는 probe_close 구간에서 이미 3유닛째가
    # 나갔어야 하지만(old_grid_unit3_trigger를 넘었으므로), 실제 체결가
    # 기준 앵커로는 아직 한참 못 미친다 - 새 로직에서는 2유닛에 머물러야
    # 정상이다.
    assert info["probe_close"] > info["old_grid_unit3_trigger"]
    assert info["probe_close"] < info["new_anchor_unit3_trigger"]

    assert pos.num_units == 3, (
        f"3유닛째가 새 앵커({info['new_anchor_unit3_trigger']:.1f}) 기준으로 "
        f"trigger3_close 도달 후에 나갔어야 하는데 num_units={pos.num_units}"
    )

    fill3 = pos.units[2].buy_price
    # 3유닛 실제 체결가가 "새 앵커(fill2+step)" 근방에서 나왔는지만 확인한다 -
    # 옛 고정 그리드(fill1+2*step) 근방이 아니어야 한다.
    assert fill3 == pytest.approx(
        (info["new_anchor_unit3_trigger"] + 2.0) * (1 + backtest.SLIPPAGE), rel=1e-6
    )
    assert abs(fill3 - info["old_grid_unit3_trigger"]) > info["step"]
