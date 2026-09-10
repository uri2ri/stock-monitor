"""검토 항목 #2 회귀 테스트 - 워치리스트 우선순위가 high20(당일 제외)
기준 dist_atr이 아니라 high20_next(당일 포함, 다음 거래일 실제 판정
기준선) 기준 dist_atr_next로 매겨지는지.

judge()는 high20_next로 돌파를 보는데 후보 선정(load_watchlist)만
high20 기준 dist_atr을 쓰면, 당일 장중 신고가를 찍고 종가는 눌린
종목처럼 두 기준이 크게 벌어지는 경우 순위와 실제 돌파선이 어긋난다
(MAX_WATCH=100 컷에서 진짜 임박 종목이 밀려날 수 있다). 이 파일은
그 순위가 실제로 dist_atr_next로 다시 매겨지는지, 구버전/무효
high20_next 행은 화면(scan_all.load_scan 원본)엔 남지만 워치리스트
(자동 신규매수 대상)에서는 빠지는지를 검증한다.
"""

from __future__ import annotations

import pandas as pd
import pytest

import intraday_watch
import scan_all


def _row(ticker: str, high20: float, high20_next: float, close: float = 10_000.0,
        atr20: float = 500.0, status: str = None) -> dict:
    return {
        "scan_date": "20260916", "ticker": ticker, "name": ticker,
        "market": "KOSPI", "sector": "전기전자", "close": close,
        "atr20": atr20, "atr_pct": 5.0, "high20": high20,
        "high20_next": high20_next, "low10": 9_000.0,
        "gap": high20 - close, "gap_atr": (high20 - close) / atr20,
        "dist_to_break": high20 - close, "dist_atr": (high20 - close) / atr20,
        "vol_mult": 1.0, "unit_shares": 10, "value_avg_20": 1_000_000_000.0,
        "status": status or scan_all.STATUS_NEAR,
    }


def test_ranking_order_follows_dist_atr_next_when_it_disagrees_with_dist_atr(monkeypatch):
    # A: high20(옛 기준) 거리가 가깝지만(dist_atr=0.2) high20_next(새
    #    기준) 거리는 멀다(dist_atr_next=1.5) - 당일 장중 신고가를 찍어
    #    high20_next가 훌쩍 올라간 경우.
    # B: 반대로 dist_atr은 멀지만(0.8) dist_atr_next는 가깝다(0.3).
    # dist_atr 기준이면 A가 먼저, dist_atr_next 기준이면 B가 먼저 와야 한다.
    row_a = _row("000001", high20=10_100.0, high20_next=10_750.0)   # dist_atr=0.2, next=1.5
    row_b = _row("000002", high20=10_400.0, high20_next=10_150.0)   # dist_atr=0.8, next=0.3
    frame = pd.DataFrame([row_a, row_b])
    monkeypatch.setattr(scan_all, "load_scan", lambda: frame)

    watch = intraday_watch.load_watchlist(threshold=2.0, limit=100)

    assert list(watch["ticker"]) == ["000002", "000001"], (
        "high20_next 기준 거리(dist_atr_next)로 정렬돼야 한다"
    )


def test_watchlist_threshold_applies_to_dist_atr_next_not_dist_atr(monkeypatch):
    # dist_atr(옛 기준)로는 임계값(1.0×ATR) 안이지만 dist_atr_next(새
    # 기준)로는 임계값 밖인 종목은 워치리스트에서 빠져야 한다.
    row = _row("000001", high20=10_200.0, high20_next=11_000.0)  # dist_atr=0.4, next=2.0
    frame = pd.DataFrame([row])
    monkeypatch.setattr(scan_all, "load_scan", lambda: frame)

    watch = intraday_watch.load_watchlist(threshold=1.0, limit=100)
    assert watch.empty, "dist_atr_next가 임계값을 넘으면 워치리스트에서 빠져야 한다"


def test_legacy_rows_without_high20_next_excluded_from_watchlist_but_visible_in_scan(
    monkeypatch,
):
    # 구버전 scan_latest.csv(high20_next 컬럼 자체가 없음)를 흉내낸다 -
    # 화면 표시(scan_all.load_scan 원본 프레임)에는 그대로 남지만,
    # 워치리스트(자동 신규매수 대상)에서는 빠져야 한다.
    legacy_row = _row("000001", high20=10_200.0, high20_next=10_500.0)
    del legacy_row["high20_next"]
    fresh_row = _row("000002", high20=10_100.0, high20_next=10_150.0)
    frame = pd.DataFrame([legacy_row, fresh_row])
    monkeypatch.setattr(scan_all, "load_scan", lambda: frame)

    # 화면(원본 스캔)에는 구버전 행도 그대로 있다.
    displayed = scan_all.load_scan()
    assert set(displayed["ticker"]) == {"000001", "000002"}

    watch = intraday_watch.load_watchlist(threshold=2.0, limit=100)
    assert list(watch["ticker"]) == ["000002"], (
        "high20_next가 없는 구버전 행은 워치리스트(신규매수 대상)에서 빠져야 한다"
    )


@pytest.mark.parametrize("bad_value", [float("nan"), float("inf"), 0, -50.0])
def test_invalid_high20_next_values_excluded_from_watchlist(monkeypatch, bad_value):
    bad_row = _row("000001", high20=10_200.0, high20_next=bad_value)
    good_row = _row("000002", high20=10_100.0, high20_next=10_150.0)
    frame = pd.DataFrame([bad_row, good_row])
    monkeypatch.setattr(scan_all, "load_scan", lambda: frame)

    watch = intraday_watch.load_watchlist(threshold=2.0, limit=100)
    assert list(watch["ticker"]) == ["000002"]


def test_watch_range_does_not_expand_to_already_broken_out_stocks(monkeypatch):
    # status="돌파"(당일 이미 돌파로 화면에 표시된) 종목은 워치리스트
    # 범위를 임의로 넓혀 포함시키지 않는다 - 기존 STATUS_NEAR 필터가
    # 그대로 유지돼야 한다는 확인.
    broken_row = _row("000001", high20=9_000.0, high20_next=9_100.0,
                      close=10_000.0, status=scan_all.STATUS_BREAKOUT)
    near_row = _row("000002", high20=10_100.0, high20_next=10_150.0)
    frame = pd.DataFrame([broken_row, near_row])
    monkeypatch.setattr(scan_all, "load_scan", lambda: frame)

    watch = intraday_watch.load_watchlist(threshold=2.0, limit=100)
    assert list(watch["ticker"]) == ["000002"]
