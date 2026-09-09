"""요구사항 #5 회귀 테스트 - 추가매수 anchor.

급등으로 재기준(pyramid_anchor 설정)이 한 번 일어난 뒤, 그다음에 실제로
추가매수가 체결되면 - kis_client.py의 add_auto_holding_units()가 "실제로
샀으므로 기준가도 체결가로 맞춘다"며 pyramid_anchor를 그 체결가로 다시
맞추는 것과 똑같이 - 그 다음 기준가는 재기준값이 아니라 방금 산 실제
체결가를 따라야 한다.

수정 전에는 pyramid_anchor가 재기준 이후로 다신 안 바뀌어서(실제 매수가
일어나도 그대로), 그 다음다음 유닛 트리거가 훨씬 오래된 재기준값에
그대로 갇혀 있었다.
"""

from __future__ import annotations

import pandas as pd
import pytest

import backtest
import core

from tests.helpers import build_df, calendar_df, flat_row, install_universe, row, warmup_rows

CALENDAR = "005930"
T = "T00030"


def test_anchor_follows_next_real_fill_after_a_reanchor(monkeypatch):
    all_dates = pd.bdate_range(start="2020-01-02", periods=260)
    cal_df = calendar_df(start="2020-01-02", periods=260)

    rows = warmup_rows(80, close=10_000.0, half_spread=300.0)
    breakout_close = 10_480.0
    rows.append(row(breakout_close, 10_600.0, 10_000.0, breakout_close))  # idx 80
    df_thru_breakout = build_df(rows, dates=all_dates[: len(rows)])
    entry_atr = core.round_atr(core.calc_atr(df_thru_breakout))
    step = core.PYRAMID_ATR_STEP * entry_atr
    chase = backtest.PYRAMID_MAX_CHASE_ATR * entry_atr

    entry_fill_open = 10_500.0
    rows.append(row(entry_fill_open, 10_650.0, 10_400.0, 10_500.0))  # idx 81
    fill1 = entry_fill_open * (1 + backtest.SLIPPAGE)
    for _ in range(5):
        rows.append(flat_row(10_500.0, half_spread=150.0))  # idx 82..86

    # 1) 급등으로 재기준(reanchor) 발생 - 트리거 평가일 종가가 추격 허용
    #    범위를 넘겨서, 매수 없이 기준가만 올라간다.
    next_price_1 = fill1 + step
    reanchor_close = next_price_1 + chase + 3 * step
    rows.append(row(reanchor_close, reanchor_close + 100, reanchor_close - 100,
                     reanchor_close))  # idx 87
    skipped = int((reanchor_close - fill1) // step)
    reanchored_value = fill1 + skipped * step

    for _ in range(5):
        rows.append(flat_row(reanchor_close, half_spread=100.0))  # idx 88..92

    # 2) 재기준된 값 기준으로 정상 범위 안에서 실제 추가매수가 체결된다.
    next_price_2 = reanchored_value + step
    trigger2_close = next_price_2 + 2.0
    rows.append(row(trigger2_close, trigger2_close + 100, trigger2_close - 100,
                     trigger2_close))  # idx 93 - 2유닛 트리거
    real_fill2_open = trigger2_close  # 갭 없이 정상 체결
    rows.append(row(real_fill2_open, real_fill2_open + 100, real_fill2_open - 100,
                     real_fill2_open))  # idx 94 - 2유닛 체결
    fill2 = real_fill2_open * (1 + backtest.SLIPPAGE)

    for _ in range(5):
        rows.append(flat_row(real_fill2_open, half_spread=100.0))  # idx 95..99

    # 3) 다음(3유닛) 트리거가 "재기준값"이 아니라 "방금 산 실제 체결가
    #    (fill2)"를 기준으로 잡히는지 확인한다 - 재기준값 기준 다음 레벨
    #    (reanchored_value + 2*step)과 fill2 기준 다음 레벨(fill2 + step)은
    #    서로 다른 값이어야 의미가 있다.
    stale_next_price = reanchored_value + 2 * step
    correct_next_price = fill2 + step
    assert abs(correct_next_price - stale_next_price) > step * 0.1, (
        "두 기준이 우연히 같아지면 이 테스트가 버그를 못 가른다 - 시나리오를 조정할 것"
    )

    probe_close = min(stale_next_price, correct_next_price) + 2.0
    assert probe_close < max(stale_next_price, correct_next_price)
    rows.append(row(probe_close, probe_close + 100, probe_close - 100,
                     probe_close))  # idx 100 - 두 기준이 갈리는 지점

    for _ in range(5):
        rows.append(flat_row(probe_close, half_spread=100.0))  # idx 101..105

    t_df = build_df(rows, dates=all_dates[: len(rows)])
    install_universe(monkeypatch, {CALENDAR: cal_df, T: t_df})

    start = all_dates[0].date()
    end = all_dates[110].date()
    result = backtest.run_portfolio_backtest(
        [CALENDAR, T], start, end, starting_capital=10_000_000.0,
    )

    assert result["trades"] == []
    pos = result["open_positions"][T]
    assert pos.num_units == 2, (
        f"probe_close={probe_close:.1f}는 fill2 기준 다음 레벨({correct_next_price:.1f})에는 "
        f"못 미쳐야 한다 - 3유닛째가 나갔다면 anchor가 여전히 재기준값에 갇혀 있다는 뜻"
        f"(재기준값 기준 다음 레벨={stale_next_price:.1f})"
    )
    assert pos.units[1].buy_price == pytest.approx(fill2)
