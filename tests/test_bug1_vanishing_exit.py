"""Bug #1 회귀 테스트 - 청산 체결일에 시가가 없으면(거래정지 등) 포지션이
장부에서 그냥 사라지던 버그.

수정 전 backtest.py의 pending_exits 처리는:

    pos = positions.pop(ticker, None)   # <- 체결 성공 여부와 무관하게 먼저 뺀다
    op = _day_open(ticker, ts)
    if op is None:
        continue                        # 체결도 없고 trade 기록도 없이 그냥 넘어감

이러면 그 포지션은 trades에도, open_positions에도, cash에도 다시는 나타나지
않는다 - 총자산에서 그 포지션 가치만큼이 그냥 증발한다. 수정 후에는 시가가
없으면 positions에서 빼지 않고 그대로 둬서, 다음날 다시 청산 판정을 받게 한다.
"""

from __future__ import annotations

import pandas as pd
import pytest

import backtest
import core

from tests.helpers import build_df, calendar_df, flat_row, install_universe, row, warmup_rows

CALENDAR = "005930"
T1 = "T00001"


def _build_t1_with_halt(all_dates: pd.DatetimeIndex) -> tuple[pd.DataFrame, dict]:
    """진입 -> 급락(손절 트리거) -> 체결일 이틀 거래정지 -> 재개 시나리오.

    Returns: (df, info) - info엔 나중 검증에 쓸 entry_atr 등 중간값을 담는다.
    """
    n_warmup = 80
    rows = warmup_rows(n_warmup, close=10_000.0, half_spread=300.0)

    # 80번째 행(0-idx 80) - 돌파일. H를 10600으로 고정해 ATR 기여분(H-L=600)을
    # 워밍업과 동일하게 유지하면서(=ATR 안 흔들림), 종가만 20일 고가(10300)
    # 위 0.3*ATR 지점으로 잡아 "진입가능" 구간에 확실히 넣는다.
    breakout_close = 10_480.0
    rows.append(row(breakout_close, 10_600.0, 10_000.0, breakout_close))

    # 81번째 행 - 진입 체결일(다음날 시가). 갭 없이 평온하게 유지.
    entry_fill_open = 10_500.0
    rows.append(row(entry_fill_open, 10_650.0, 10_400.0, 10_500.0))

    # 완충 구간 - 며칠 더 평온하게.
    for _ in range(5):
        rows.append(flat_row(10_500.0, half_spread=150.0))

    # 급락일 - entry_atr을 먼저 계산해 손절선 아래로 확실히 떨어뜨린다.
    df_so_far = build_df(rows, dates=all_dates[: len(rows)])
    entry_atr = core.round_atr(core.calc_atr(df_so_far))
    fill1 = entry_fill_open * (1 + backtest.SLIPPAGE)
    stop_loss = fill1 - 2 * entry_atr
    crash_close = stop_loss - 3 * entry_atr  # 여유 있게 손절선 아래
    rows.append(row(crash_close, crash_close + 50, crash_close - 50, crash_close))

    # 체결 예정일(급락 다음날) 이틀치를 통째로 뺀다 - 거래정지 시뮬레이션.
    n_before_halt = len(rows)
    dates = list(all_dates[:n_before_halt]) + list(all_dates[n_before_halt + 2:])

    # 거래정지 뒤 재개 - 여전히 손절선 아래인 채로 시가 체결.
    resume_open = crash_close - 10
    rows.append(row(resume_open, resume_open + 50, resume_open - 50, resume_open))
    for _ in range(5):
        rows.append(flat_row(resume_open, half_spread=100.0))

    df = build_df(rows, dates=dates[: len(rows)])
    return df, {
        "entry_atr": entry_atr,
        "fill1": fill1,
        "stop_loss": stop_loss,
        "crash_close": crash_close,
        "resume_open": resume_open,
        "n_before_halt": n_before_halt,
    }


def test_halted_exit_does_not_vanish_from_ledger(monkeypatch):
    all_dates = pd.bdate_range(start="2020-01-02", periods=260)
    cal_df = calendar_df(start="2020-01-02", periods=260)
    t1_df, info = _build_t1_with_halt(all_dates)

    install_universe(monkeypatch, {CALENDAR: cal_df, T1: t1_df})

    start = all_dates[0].date()
    end = all_dates[110].date()
    starting_capital = 10_000_000.0
    result = backtest.run_portfolio_backtest(
        [CALENDAR, T1], start, end, starting_capital=starting_capital,
    )

    trades_by_ticker = {t.ticker: t for t in result["trades"]}
    open_by_ticker = result["open_positions"]

    # 핵심 불변식: 한 번 진입한 포지션은 결국 trades(청산 완료) 아니면
    # open_positions(아직 보유 중) 둘 중 하나에 반드시 있어야 한다 - 둘
    # 다에 없으면 장부에서 증발한 것이다.
    assert (T1 in trades_by_ticker) != (T1 in open_by_ticker), (
        "T1이 trades와 open_positions 어느 쪽에도 없거나 양쪽에 겹쳐 있다 "
        f"(trades={T1 in trades_by_ticker}, open={T1 in open_by_ticker})"
    )
    assert T1 in trades_by_ticker, "거래정지가 풀린 뒤 결국 청산 체결됐어야 한다"

    trade = trades_by_ticker[T1]
    # 거래정지 이틀을 건너뛰고 재개일 시가로 체결됐는지 확인.
    expected_fill = info["resume_open"] * (1 - backtest.SLIPPAGE)
    expected_proceeds = expected_fill * trade.total_shares * (1 - backtest.COST_RATE)
    assert trade.proceeds == pytest.approx(expected_proceeds)

    # 총자산 보존 확인: 최종 현금 = 시작자본 - 진입원가 + 청산대금
    # (그 사이 다른 현금흐름 없음) - 포지션 가치가 중간에 그냥 사라지지 않았다.
    expected_final_cash = starting_capital - trade.total_invested + trade.proceeds
    assert result["final_cash"] == pytest.approx(expected_final_cash)
    assert result["open_positions_value"] == 0.0
    assert result["final_value"] == pytest.approx(expected_final_cash)
