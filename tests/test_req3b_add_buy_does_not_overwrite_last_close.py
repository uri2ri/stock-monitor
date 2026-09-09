"""요구사항 #3 추가 회귀 - 추가매수 체결가가 마지막 유효 종가를 덮어쓰면
안 된다.

진입 때 last_known_close를 체결가로 seed하는 것과 달리, 추가매수 시점엔
이미 그 이전 며칠간의 유효한 종가가 last_known_close에 쌓여 있다. 그날
공교롭게 종가가 또 없다고 해서 "마지막 유효 종가"를 방금 산 추가매수
체결가로 덮어써 버리면, 원래 갖고 있던 유닛들까지 전부 그 체결가로
재평가되는 왜곡이 생긴다 - "마지막 유효 종가를 쓰라"는 요청과 다르다.

시나리오: 추가매수가 트리거된 다음 날(체결일) 시가가 트리거가보다
꽤 올라 있고(체결가가 직전 종가와 값이 달라진다), 공교롭게 그날 자체
종가는 무효(거래량 0)다. 이 날의 계좌평가액은 "그날 이전 마지막으로
확인된 종가"를 써야지, 그날 추가매수 체결가를 쓰면 안 된다.
"""

from __future__ import annotations

import pandas as pd
import pytest

import backtest
import core

from tests.helpers import build_df, calendar_df, flat_row, install_universe, row, warmup_rows

CALENDAR = "005930"
T = "T00050"


def test_add_buy_fill_does_not_overwrite_prior_valid_close(monkeypatch):
    all_dates = pd.bdate_range(start="2020-01-02", periods=200)
    cal_df = calendar_df(start="2020-01-02", periods=200)

    rows = warmup_rows(80, close=10_000.0, half_spread=300.0)
    breakout_close = 10_480.0
    rows.append(row(breakout_close, 10_600.0, 10_000.0, breakout_close))  # idx80
    df_thru_breakout = build_df(rows, dates=all_dates[:81])
    entry_atr = core.round_atr(core.calc_atr(df_thru_breakout))
    step = core.PYRAMID_ATR_STEP * entry_atr

    entry_open = 10_500.0
    rows.append(row(entry_open, entry_open + 150.0, entry_open - 100.0, entry_open))  # idx81
    fill1 = entry_open * (1 + backtest.SLIPPAGE)
    for _ in range(5):
        rows.append(flat_row(entry_open, half_spread=150.0))  # idx82..86 - 종가=10500 유지

    next_price = fill1 + step
    trigger_close = next_price + 2.0
    rows.append(row(trigger_close, trigger_close + 100, trigger_close - 100,
                     trigger_close))  # idx87 - 트리거일(이날 종가=trigger_close, 유효)

    # 체결일(idx88) - 시가는 트리거가보다 다소 올라 실제 체결가가 트리거
    # 종가와 확연히 달라지게 하되, 추격 허용범위 안에서(취소되지 않게).
    # 종가만 무효(NaN)로 만든다 - 거래량을 0으로 하면 시가까지 함께
    # 무효(_tradable_row)가 돼 체결 자체가 안 되므로, 체결(시가)은 살리고
    # 종가만 없앤다. 시뮬레이션을 이 날짜에서 바로 끝내(evaluate_holding이
    # 다시 안 불리게) core.calc_atr()이 이 NaN 종가를 재귀식에 영구히
    # 물들이는(이번 수정 범위 밖의 core.py 문제) 것을 피한다.
    fill_open = trigger_close + 0.3 * step
    fill2 = fill_open * (1 + backtest.SLIPPAGE)
    rows.append(row(fill_open, fill_open + 100, fill_open - 100, float("nan")))  # idx88

    t_df = build_df(rows, dates=all_dates[: len(rows)])
    install_universe(monkeypatch, {CALENDAR: cal_df, T: t_df})

    start = all_dates[0].date()
    end = all_dates[88].date()  # 체결일(idx88)에서 바로 끝낸다
    starting_capital = 10_000_000.0
    result = backtest.run_portfolio_backtest(
        [CALENDAR, T], start, end, starting_capital=starting_capital,
    )

    pos = result["open_positions"][T]
    assert pos.num_units == 2, "추가매수는 정상적으로 체결됐어야 한다(추격범위 안)"
    assert pos.units[1].buy_price == pytest.approx(fill2)

    # 체결일(idx88)의 계좌평가액은 "그 이전 마지막 유효 종가"(트리거일
    # 종가=trigger_close)를 써야지, 방금 산 추가매수 체결가(fill2)를 쓰면
    # 안 된다.
    equity_by_date = dict(result["equity_curve"])
    fill_day_equity = equity_by_date[all_dates[88].date()]

    expected_equity_correct = result["final_cash"] + trigger_close * pos.total_shares
    expected_equity_if_bug = result["final_cash"] + fill2 * pos.total_shares

    assert fill_day_equity == pytest.approx(expected_equity_correct), (
        f"체결일 평가액이 '마지막 유효 종가'({trigger_close:.1f}) 기준이 아니라 "
        f"'방금 산 체결가'({fill2:.1f}) 기준으로 나왔다"
    )
    assert fill_day_equity != pytest.approx(expected_equity_if_bug)

    # 최종 평가(open_positions_value)도 동일 규칙이어야 한다(요청한
    # "일별 평가와 최종 평가가 같은 규칙" 확인 겸).
    assert result["open_positions_value"] == pytest.approx(trigger_close * pos.total_shares)
