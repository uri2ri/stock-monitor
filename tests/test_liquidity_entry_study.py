"""backtest_liquidity_study - 3~5억 최초 돌파 뒤 늦은 진입 연구.

판정 함수(Case 1~7)는 숫자를 직접 넣어 보고, 보유 중 무영향(Case 8)·사이클
리셋(Case 9)·모드별 거래대금 시점은 합성 시세로 시뮬레이터를 끝까지 돌려 본다.
네트워크·실데이터 파일은 쓰지 않는다.
"""

from __future__ import annotations

import math
from datetime import date

import pandas as pd
import pytest

import backtest as bt
import backtest_liquidity_study as study
import core

EOK = 100_000_000
D0 = date(2020, 1, 1)
D1 = date(2020, 1, 10)


@pytest.fixture(autouse=True)
def _no_period_guard(monkeypatch):
    # 다른 테스트가 구간을 걸어 뒀어도 합성 날짜(2020년)가 막히지 않게 한다.
    monkeypatch.setattr(bt, "_PERIOD_NAME", None)
    monkeypatch.setattr(bt, "_PERIOD_MAX", None)


def _cycle(turnover, threshold=70_000, atr=4_000) -> study.TrendCycle:
    tracker = study.CycleTracker()
    return tracker.observe_breakout(D0, threshold=threshold, atr=atr,
                                    turnover_20=turnover, close=threshold + 500)


def _allowed(cycle, turnover, price):
    return {s: study.strategy_allows_entry(s, turnover_20=turnover, cycle=cycle,
                                           candidate_price=price)[0]
            for s in study.STRATEGIES}


# ── Case 1·2: 4억 최초 돌파 → 6억에서 재돌파 ──────────────────

def test_case1_late_gap_within_chase_limit():
    cyc = _cycle(4 * EOK)
    assert cyc.is_low_liquidity
    assert study.late_entry_gap_atr(71_500, cyc) == pytest.approx(0.375)
    assert _allowed(cyc, 6 * EOK, 71_500) == {
        study.STRATEGY_BASELINE: True,
        study.STRATEGY_SKIP: False,
        study.STRATEGY_GUARD: True,
    }


def test_case2_late_gap_beyond_chase_limit():
    cyc = _cycle(4 * EOK)
    assert study.late_entry_gap_atr(72_500, cyc) == pytest.approx(0.625)
    assert _allowed(cyc, 6 * EOK, 72_500) == {
        study.STRATEGY_BASELINE: True,
        study.STRATEGY_SKIP: False,
        study.STRATEGY_GUARD: False,
    }
    ok, reason = study.strategy_allows_entry(study.STRATEGY_GUARD, turnover_20=6 * EOK,
                                             cycle=cyc, candidate_price=72_500)
    assert (ok, reason) == (False, "늦은진입")


def test_guard_reads_core_chase_mult_at_call_time(monkeypatch):
    """0.5를 하드코딩하지 않았다 - core.CHASE_ATR_MULT를 바꾸면 판정이 따라간다."""
    cyc = _cycle(4 * EOK)
    monkeypatch.setattr(core, "CHASE_ATR_MULT", 0.3)
    assert _allowed(cyc, 6 * EOK, 71_500)[study.STRATEGY_GUARD] is False
    monkeypatch.setattr(core, "CHASE_ATR_MULT", 0.7)
    assert _allowed(cyc, 6 * EOK, 72_500)[study.STRATEGY_GUARD] is True


def test_all_strategies_respect_buy_threshold():
    """전략과 무관하게 5억 미만이면 신규매수 없음 - 라이브 매수 하한 그대로."""
    cyc = _cycle(4 * EOK)
    assert not any(_allowed(cyc, 4.9 * EOK, 70_500).values())
    assert not any(_allowed(None, float("nan"), 70_500).values())


# ── Case 3~6: 최초 돌파 거래대금 경계 ─────────────────────────

def test_case3_first_breakout_at_8eok_is_normal():
    cyc = _cycle(8 * EOK)
    assert cyc.band == study.BAND_5_10
    assert not cyc.is_low_liquidity
    # 최초 돌파선에서 멀리 와도 세 전략 모두 일반 신규매수 로직을 따른다.
    assert all(_allowed(cyc, 8 * EOK, 90_000).values())


def test_case4_below_watch_threshold_not_low_liquidity():
    cyc = _cycle(2.9 * EOK)
    assert cyc.band == study.BAND_BELOW_3
    assert not cyc.is_low_liquidity
    assert all(_allowed(cyc, 6 * EOK, 90_000).values())


def test_case5_exactly_3eok_is_observed():
    cyc = _cycle(3 * EOK)
    assert cyc.band == study.BAND_3_5
    assert cyc.is_low_liquidity


def test_case6_exactly_5eok_is_buyable_not_low_liquidity():
    cyc = _cycle(5 * EOK)
    assert cyc.band == study.BAND_5_10
    assert not cyc.is_low_liquidity
    assert core.is_buy_liquid(5 * EOK)
    assert all(_allowed(cyc, 5 * EOK, 90_000).values())


def test_liquidity_band_edges():
    assert study.liquidity_band(10 * EOK) == study.BAND_10_PLUS
    assert study.liquidity_band(9.99 * EOK) == study.BAND_5_10
    assert study.liquidity_band(None) == study.BAND_UNKNOWN
    assert study.liquidity_band(float("nan")) == study.BAND_UNKNOWN


# ── Case 7: 최초 돌파 ATR 무효 ─────────────────────────────────

@pytest.mark.parametrize("bad_atr", [0, float("nan"), -1, None, "x", float("inf")])
def test_case7_invalid_first_atr_fails_closed(bad_atr):
    cyc = _cycle(4 * EOK, atr=bad_atr)
    assert cyc.is_low_liquidity
    assert study.late_entry_gap_atr(71_000, cyc) is None
    ok, reason = study.strategy_allows_entry(study.STRATEGY_GUARD, turnover_20=6 * EOK,
                                             cycle=cyc, candidate_price=71_000)
    assert (ok, reason) == (False, "최초돌파ATR무효")
    assert _allowed(cyc, 6 * EOK, 71_000) == {
        study.STRATEGY_BASELINE: True,
        study.STRATEGY_SKIP: False,
        study.STRATEGY_GUARD: False,
    }


# ── 사이클 추적기 ────────────────────────────────────────────

def test_later_breakouts_keep_first_anchor():
    tracker = study.CycleTracker()
    first = tracker.observe_breakout(D0, threshold=70_000, atr=4_000,
                                     turnover_20=4 * EOK, close=70_500)
    again = tracker.observe_breakout(D1, threshold=75_000, atr=4_500,
                                     turnover_20=7 * EOK, close=76_000)
    assert again is first
    assert first.first_breakout_threshold == 70_000
    assert first.band == study.BAND_3_5
    assert len(tracker.history) == 1


def test_case9_cycle_reset_starts_new_cycle():
    tracker = study.CycleTracker()
    tracker.observe_breakout(D0, threshold=70_000, atr=4_000,
                             turnover_20=4 * EOK, close=70_500)
    # 최초 돌파 당일에는 종료 판정을 하지 않는다.
    assert not tracker.observe_close(D0, close=60_000, low10_prev=65_000)
    assert not tracker.observe_close(date(2020, 1, 2), close=69_000, low10_prev=68_000)
    assert tracker.observe_close(date(2020, 1, 3), close=68_000, low10_prev=68_000)  # <= 이면 종료
    assert tracker.active is None
    assert tracker.history[0].end_date == date(2020, 1, 3)

    new = tracker.observe_breakout(D1, threshold=72_000, atr=4_000,
                                   turnover_20=6 * EOK, close=73_000)
    assert new is not tracker.history[0]
    assert new.band == study.BAND_5_10
    assert all(_allowed(new, 6 * EOK, 90_000).values())


# ── 합성 시세로 시뮬레이터 전체 ────────────────────────────────

def _frame(rows) -> pd.DataFrame:
    idx = pd.bdate_range("2020-01-01", periods=len(rows))
    return pd.DataFrame(rows, index=idx, columns=["시가", "고가", "저가", "종가", "거래량"])


def _base(n=60, vol=40_000):
    # 종가 10,000 · TR 400(ATR 4%) · 20일 고가 10,200. 거래량 40,000 = 일 4억.
    return [(10_000, 10_200, 9_800, 10_000, vol)] * n


def _run(rows, mode="intraday"):
    df = _frame(rows)
    return study.simulate_ticker("999999", df, mode, df.index[0].date(), df.index[-1].date(),
                                 name="합성")


def _rising_tail(start_close, n=10, step=30, vol=60_000):
    rows, c = [], start_close
    for _ in range(n):
        o, c = c, c + step
        rows.append((o, c + 100, c - 150, c, vol))
    return rows


@pytest.mark.parametrize("day60_high,guard_enters", [(10_300, True), (10_500, False)])
def test_sim_intraday_late_entry_after_lowliq_breakout(day60_high, guard_enters):
    """4억에서 장중 최초 돌파 → 그날 거래 폭증으로 다음날 5억 이상 → 재돌파."""
    rows = _base()
    rows.append((10_000, day60_high, 9_900, day60_high - 50, 400_000))   # 저유동 최초 돌파
    c60 = day60_high - 50
    rows.append((c60, day60_high + 150, c60 - 100, day60_high + 100, 60_000))  # 재돌파
    rows += _rising_tail(day60_high + 100)
    res = _run(rows)

    lowliq = [c for c in res.cycles if c.is_low_liquidity]
    assert len(lowliq) == 1
    cyc = lowliq[0]
    assert cyc.first_breakout_date == res.td.dates[60]
    assert cyc.first_breakout_threshold == 10_200
    assert cyc.first_breakout_atr == 400
    assert cyc.first_breakout_turnover == pytest.approx(4 * EOK)   # 전일까지 - 당일 폭증 미반영

    base = res.trades[study.STRATEGY_BASELINE]
    assert len(base) == 1 and base[0].signal_date == res.td.dates[61]
    assert base[0].band == study.BAND_3_5
    # 진입 검토가 = 당일 돌파선(전일 고가) → 최초 돌파선 대비 거리
    expected_gap = (day60_high - 10_200) / 400
    assert base[0].cycle_gap_atr == pytest.approx(expected_gap)
    assert res.trades[study.STRATEGY_SKIP] == []
    guard = res.trades[study.STRATEGY_GUARD]
    assert (len(guard) == 1) is guard_enters

    events = study.low_liquidity_events(res, res.td.dates[0], res.td.dates[-1])
    assert len(events) == 1
    ev = events[0]
    assert ev["first_5eok_date"] == res.td.dates[60]
    assert ev["baseline_entry_date"] == res.td.dates[61]
    assert ev["baseline_entry_gap_atr"] == pytest.approx(expected_gap)
    assert ev["skip_entered"] is False and ev["guard_entered"] is guard_enters


def test_sim_close_mode_counts_signal_day_turnover():
    """close 모드는 신호일 종가까지의 거래대금을 본다 - 같은 시세라도 최초 돌파가 5억 이상."""
    rows = _base()
    rows.append((10_000, 10_300, 9_900, 10_250, 400_000))
    rows += _rising_tail(10_250)
    res = _run(rows, mode="close")
    assert res.cycles[0].band == study.BAND_5_10
    assert not any(c.is_low_liquidity for c in res.cycles)
    entries = {s: [t.entry_date for t in res.trades[s]] for s in study.STRATEGIES}
    assert entries[study.STRATEGY_BASELINE] == [res.td.dates[61]]    # 다음날 시가 체결
    assert entries[study.STRATEGY_SKIP] == entries[study.STRATEGY_GUARD] == entries[study.STRATEGY_BASELINE]


def test_case8_held_position_unaffected(monkeypatch):
    """8억 최초 돌파로 세 전략이 같이 진입 → 보유 중 거래대금이 3~5억으로 떨어지고
    돌파가 이어져도 피라미딩·청산은 세 전략이 동일하고, 전략 필터는 불리지 않는다."""
    rows = _base(vol=80_000)
    rows.append((10_000, 10_300, 9_900, 10_250, 80_000))
    c = 10_250
    for _ in range(15):                                   # 상승 + 거래 감소(일 2억)
        o, c = c, c + 100
        rows.append((o, c + 100, c - 200, c, 20_000))
    rows.append((c, c, 9_000, 9_100, 20_000))             # 급락 → 청산 신호
    rows += [(9_100, 9_200, 9_000, 9_100, 20_000)] * 5

    calls = []
    real = study.strategy_allows_entry

    def spy(strategy, **kw):
        calls.append((strategy, kw["cycle"].first_breakout_date if kw["cycle"] else None))
        return real(strategy, **kw)

    monkeypatch.setattr(study, "strategy_allows_entry", spy)
    res = _run(rows)

    summary = {s: [(t.entry_date, t.exit_date, t.final_units, t.total_shares, round(t.net_pnl))
                   for t in res.trades[s]] for s in study.STRATEGIES}
    assert len(summary[study.STRATEGY_BASELINE]) == 1
    assert summary[study.STRATEGY_BASELINE][0][2] >= 2          # 피라미딩이 실제로 일어남
    assert summary[study.STRATEGY_SKIP] == summary[study.STRATEGY_BASELINE]
    assert summary[study.STRATEGY_GUARD] == summary[study.STRATEGY_BASELINE]

    t = res.trades[study.STRATEGY_BASELINE][0]
    held = [d for d in res.td.dates if t.entry_date < d < t.exit_date]
    triggered_while_held = [d for i, d in enumerate(res.td.dates)
                            if d in held and res.signals[i].triggered]
    assert triggered_while_held, "보유 중에도 돌파가 있어야 이 테스트가 의미 있다"
    # 필터 호출은 미보유 상태(진입 당일)뿐 - 보유 중 돌파일에는 한 번도 불리지 않는다.
    assert len(calls) == len(study.STRATEGIES)
    assert res.td.dates[-1] > t.exit_date


def test_case9_sim_new_cycle_after_trend_end():
    """저유동 최초 돌파 → 10일 저가 이탈로 추세 종료 → 5억 이상에서 새 20일 돌파는
    새 사이클이라 skip·guard도 정상 진입한다."""
    rows = _base()
    rows.append((10_000, 10_300, 9_900, 10_250, 40_000))   # 4억 최초 돌파 (day60)
    rows.append((10_250, 10_250, 9_500, 9_600, 40_000))    # day61 종가 ≤ 10일 저가 → 종료
    rows += [(9_600, 9_800, 9_400, 9_600, 80_000)] * 23    # day62~84 횡보, 거래대금 증가
    rows.append((9_600, 10_000, 9_500, 9_900, 80_000))     # day85 새 돌파
    rows += _rising_tail(9_900, vol=80_000)
    res = _run(rows)

    assert len(res.cycles) == 2
    first, second = res.cycles
    assert first.is_low_liquidity and first.end_date == res.td.dates[61]
    assert second.first_breakout_date == res.td.dates[85]
    assert second.band == study.BAND_5_10
    for s in study.STRATEGIES:
        assert [t.signal_date for t in res.trades[s]] == [res.td.dates[85]], s


def test_output_paths_default_and_variants():
    s, e = study.output_paths("dev", "intraday", None)
    assert s.name == "bt_liquidity_entry_study_dev.csv"
    assert e.name == "bt_low_liquidity_breakouts.csv"
    s, e = study.output_paths("dev", "close", "171090")
    assert s.name == "bt_liquidity_entry_study_dev_close_171090.csv"
    assert e.name == "bt_low_liquidity_breakouts_close_171090.csv"
    s, e = study.output_paths("holdout", "intraday", None)
    assert e.name == "bt_low_liquidity_breakouts_holdout.csv"


def test_metrics_on_empty_trades():
    m = study.compute_study_metrics([])
    assert m["거래수"] == 0 and m["최종자산"] == bt.STARTING_CAPITAL
    assert math.isnan(m["평균R"])
