"""요구사항 #6 회귀 테스트 - 예약·체결 한도(신규·추가 혼합).

같은 날 기존 보유 종목의 추가매수 트리거(4번 블록)와 새 종목의 신규
진입 트리거(5번 블록)가 같은 현금 풀을 나눠 쓴다. 예약 금액이 슬리피지·
수수료를 뺀 "원금"만 본다면, 두 후보의 원금 합은 남은 현금에 들어가도
실제 체결 원가(슬리피지+수수료 포함) 합은 넘는 경계 상황에서 - 옛
코드는 두 후보를 다 승인해버리고, 새 코드는(예약 자체에 비용을 포함하니)
뒤에 평가되는 후보(신규 진입)를 정확히 현금부족으로 거절해야 한다.

여러 개의 "채움용" 종목으로 계좌 현금을 정밀하게 그 경계까지 소진시켜
둔다(상관군·전체 유닛 캡, 일일 신규진입 상한은 이 테스트의 관심사가
아니므로 넉넉하게 풀어둔다).
"""

from __future__ import annotations

import pandas as pd
import pytest

import backtest
import core

from tests.helpers import build_df, calendar_df, flat_row, install_universe, row, warmup_rows

CALENDAR = "005930"
A = "T00040"
B = "T00041"


def _reserved(price: float, shares: int) -> float:
    """backtest._reserved_buy_cost()와 같은 식을 테스트 쪽에서 독립적으로
    계산한다 - 모듈 함수를 직접 부르면 그 함수 자체가 없던(이번 수정 전)
    코드에 대해서는 테스트를 아예 못 돌려보게 된다."""
    return price * shares * (1 + backtest.SLIPPAGE) * (1 + backtest.COST_RATE)


def _entry_only_df(all_dates, close=10_000.0, entry_open=10_500.0):
    rows = warmup_rows(80, close=close, half_spread=300.0)
    rows.append(row(close + 480.0, close + 600.0, close, close + 480.0))  # idx80 - 돌파
    rows.append(row(entry_open, entry_open + 150.0, entry_open - 100.0, entry_open))  # idx81 - 체결
    for _ in range(10):
        rows.append(flat_row(entry_open, half_spread=150.0))
    return build_df(rows, dates=all_dates[: len(rows)])


def test_mixed_new_and_add_reservation_rejects_second_when_cost_included(monkeypatch):
    all_dates = pd.bdate_range(start="2020-01-02", periods=200)
    cal_df = calendar_df(start="2020-01-02", periods=200)
    starting_capital = 10_000_000.0

    dfs = {CALENDAR: cal_df}

    # A: 실제로 계속 추적할 종목 - idx87에 2유닛째 트리거.
    a_rows = warmup_rows(80, close=10_000.0, half_spread=300.0)
    breakout_close = 10_480.0
    a_rows.append(row(breakout_close, 10_600.0, 10_000.0, breakout_close))  # idx80
    df_thru_breakout = build_df(a_rows, dates=all_dates[:81])
    entry_atr = core.round_atr(core.calc_atr(df_thru_breakout))
    step = core.PYRAMID_ATR_STEP * entry_atr
    shares = core.calc_position(entry_atr, starting_capital, core.RISK_PER_TRADE).unit_shares

    entry_open = 10_500.0
    a_rows.append(row(entry_open, entry_open + 150.0, entry_open - 100.0, entry_open))  # idx81
    fill1 = entry_open * (1 + backtest.SLIPPAGE)
    for _ in range(5):
        a_rows.append(flat_row(entry_open, half_spread=150.0))  # idx82..86

    next_price2 = fill1 + step
    trigger2_close = next_price2 + 2.0
    a_rows.append(row(trigger2_close, trigger2_close + 100, trigger2_close - 100,
                       trigger2_close))  # idx87 - A 2유닛 트리거일
    a_rows.append(row(trigger2_close, trigger2_close + 100, trigger2_close - 100,
                       trigger2_close))  # idx88 - A 2유닛 체결일(갭 없음)
    for _ in range(5):
        a_rows.append(flat_row(trigger2_close, half_spread=100.0))
    dfs[A] = build_df(a_rows, dates=all_dates[: len(a_rows)])

    # B: idx87에 돌파 신호(=A의 트리거일과 같은 날, 그래야 같은 날 현금 풀을
    # 나눠 쓰는 상황이 된다) -> idx88에 신규 진입 체결(=A의 2유닛 체결일과 같음).
    # 신호일 20일 고가가 b_price보다 0.3×ATR 아래에 있어야 "진입가능"
    # 구간(0 < gap_atr <= 0.5)에 들어간다.
    b_price = trigger2_close
    desired_high20_b = b_price - 0.3 * entry_atr  # entry_atr(=600)는 이 warmup 기법이면 항상 나오는 값
    close_b = desired_high20_b - 300.0  # half_spread=300
    b_rows = warmup_rows(87, close=close_b, half_spread=300.0)  # idx0..86
    b_rows.append(row(b_price, b_price + 100, b_price - 100, b_price))  # idx87 - 돌파 신호
    b_rows.append(row(b_price, b_price + 100, b_price - 100, b_price))  # idx88 - 체결
    for _ in range(5):
        b_rows.append(flat_row(b_price, half_spread=100.0))
    dfs[B] = build_df(b_rows, dates=all_dates[: len(b_rows)])

    a_unit2_raw = shares * trigger2_close
    a_unit2_cost = _reserved(trigger2_close, shares)
    b_raw = shares * b_price  # 계좌 규모가 비슷하게 유지되므로 B도 대략 같은 shares
    b_cost = _reserved(b_price, shares)
    raw_sum = a_unit2_raw + b_raw
    cost_sum = a_unit2_cost + b_cost
    target_cash_before_decision = raw_sum + (cost_sum - raw_sum) / 2

    total_needed_a_entry = fill1 * shares * (1 + backtest.COST_RATE)
    filler_open = 10_500.0
    filler_fill = filler_open * (1 + backtest.SLIPPAGE)
    filler_cost = filler_fill * shares * (1 + backtest.COST_RATE)

    to_drain = (starting_capital - total_needed_a_entry) - target_cash_before_decision
    n_full_fillers = int(to_drain // filler_cost)
    remainder = to_drain - n_full_fillers * filler_cost
    precision_open = remainder / (shares * (1 + backtest.SLIPPAGE) * (1 + backtest.COST_RATE))
    assert 0 < precision_open < filler_open, "정밀 조정용 filler 가격이 유효 범위를 벗어났다"

    filler_tickers = []
    for i in range(n_full_fillers):
        t = f"F{i:03d}"
        filler_tickers.append(t)
        dfs[t] = _entry_only_df(all_dates, close=10_000.0 + i, entry_open=filler_open)
    precision_ticker = "FPRECISE"
    filler_tickers.append(precision_ticker)
    dfs[precision_ticker] = _entry_only_df(all_dates, close=3_600.0, entry_open=precision_open)

    install_universe(monkeypatch, dfs)
    monkeypatch.setattr(backtest, "MAX_DAILY_ENTRIES", 999)

    tickers = [CALENDAR, A, B] + filler_tickers
    start = all_dates[0].date()
    end = all_dates[95].date()
    result = backtest.run_portfolio_backtest(
        tickers, start, end, starting_capital=starting_capital,
        unit_caps=(4, 999, 999),
    )

    # A의 2유닛째는 (4번 블록에서 먼저 평가되므로) 승인·체결됐어야 한다.
    assert A in result["open_positions"]
    assert result["open_positions"][A].num_units == 2

    # B는 A의 예약 이후 남은 현금으로 비용까지 포함해 계산하면 부족해
    # 거절됐어야 한다 - 원금만 봤다면(옛 코드) 승인됐을 상황이다.
    assert B not in result["open_positions"], (
        "비용을 뺀 원금만으로 현금을 예약했다면(예전 방식) B도 승인됐을 것이다 - "
        "예약 현금에 슬리피지·수수료를 포함해야 여기서 거절된다"
    )
    assert result["rejections"]["현금부족"] >= 1
