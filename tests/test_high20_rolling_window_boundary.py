"""검토 항목 #2 검증 시나리오 - 오래된 20일 구간의 최고가가 롤링
윈도우에서 빠지는 경계 날짜를 core.trend_signals()가 정확히 처리하는지.

high_20(당일 포함 20일 고가, next 거래일 판정 기준)과 high_20_prev
(당일 제외 20일 고가, 당일 판정 기준)는 인덱스가 하루 어긋난 두
윈도우다: high_20_prev는 highs[-21:-1](오늘 제외, 어제까지 20일)이고
high_20은 highs[-20:](오늘 포함 20일)이다. 그래서 "21번째 전날"의
고가는 high_20_prev의 창에는 들어가지만 high_20의 창에는 이미 빠져
있다 - 하루가 더 지나면(다음 스캔에서) 그 값은 high_20_prev의 창에서도
빠진다. 이 경계가 하루 단위로 정확히 이동하는지를 확인한다.
"""

from __future__ import annotations

import pandas as pd

import core

FLAT = 10_000.0
SPREAD = 100.0


def _flat_row(close: float = FLAT, high: float = None, low: float = None) -> dict:
    return {
        "시가": close, "고가": high if high is not None else close + SPREAD,
        "저가": low if low is not None else close - SPREAD, "종가": close,
        "거래량": 1_000_000.0,
    }


def _hist_with_spike_at(spike_index_from_end: int, n_days: int, spike_high: float) -> pd.DataFrame:
    """뒤에서 spike_index_from_end번째(1=오늘) 날의 고가만 spike_high로 높인다."""
    rows = [_flat_row() for _ in range(n_days)]
    rows[-spike_index_from_end]["고가"] = spike_high
    idx = pd.bdate_range("2020-01-02", periods=len(rows))
    return pd.DataFrame(rows, index=idx)


def test_day_21_from_end_is_in_high20_prev_but_not_in_high20():
    # 오늘 기준 21일 전(뒤에서 21번째)에만 신고가를 찍었다 -
    # high_20_prev(어제까지 20일, 뒤에서 2~21번째)의 창에는 들어가지만
    # high_20(오늘까지 20일, 뒤에서 1~20번째)의 창에는 이미 빠져 있다.
    spike_high = 20_000.0
    hist = _hist_with_spike_at(spike_index_from_end=21, n_days=25, spike_high=spike_high)
    sig = core.trend_signals(hist)

    assert sig.high_20_prev == spike_high, "21일 전 고가는 당일 제외 20일 창에 남아 있어야 한다"
    assert sig.high_20 != spike_high, "같은 값이 당일 포함 20일 창(더 짧게 밀린 창)에서는 빠져야 한다"
    assert sig.high_20 == FLAT + SPREAD


def test_spike_falls_out_of_both_windows_one_day_later():
    # 하루가 더 지나면(다음 거래일 스캔) 같은 스파이크가 이제 뒤에서
    # 22번째가 되어 high_20_prev의 창(뒤에서 2~21번째)에서도 빠진다.
    spike_high = 20_000.0
    hist_next_day = _hist_with_spike_at(spike_index_from_end=22, n_days=26, spike_high=spike_high)
    sig = core.trend_signals(hist_next_day)

    assert sig.high_20_prev == FLAT + SPREAD, "하루 더 지나면 21일 전 고가도 창에서 빠져야 한다"
    assert sig.high_20 == FLAT + SPREAD


def test_day_20_from_end_is_in_both_windows():
    # 뒤에서 20번째(오늘 포함 20일 창의 가장 오래된 날) 고가는 두 창
    # 모두에 들어가야 한다 - high_20(뒤에서 1~20)과 high_20_prev(뒤에서
    # 2~21) 둘 다 이 인덱스를 포함한다.
    spike_high = 20_000.0
    hist = _hist_with_spike_at(spike_index_from_end=20, n_days=25, spike_high=spike_high)
    sig = core.trend_signals(hist)

    assert sig.high_20 == spike_high
    assert sig.high_20_prev == spike_high
