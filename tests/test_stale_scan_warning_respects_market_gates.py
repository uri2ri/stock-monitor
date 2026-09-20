"""낡은 스캔 경고는 휴장일·장시간 밖에 카톡으로 나가면 안 된다.

2026-09-20(일) cron-job.org가 요일을 모른 채 10분마다 auto-trade를
계속 트리거했고(run 35489001694 등 일요일 13:23~14:03 KST), 그때마다
"야간 스캔 기준일이 오래됐습니다 - 신규 진입을 보류합니다" 카톡이
억제 창(1시간)마다 나갔다. 정작 그 시간엔 신규 진입이 아예 일어나지
않는다 - run_auto_trade가 _within_trading_hours → _is_trading_day 두
게이트로 먼저 막기 때문이다. 같은 원인(주말 트리거)으로 2026-08-22(토)
토큰 발급 경고가 반복된 전례가 있어 만든 게이트인데, 이 경고만 그
게이트를 타지 않고 있었다.

검증 범위: 알림 발송 여부만. 낡은 스캔이 신규 진입을 막는 동작
자체(enterable 보류)는 건드리지 않았고 test_bug4_stale_scan_blocks_
new_entries.py가 계속 지킨다.
"""

from __future__ import annotations

import pandas as pd
import pytest

import intraday_watch
import kis_client
from tests.helpers import block_external_http, scrub_credential_env


@pytest.fixture(autouse=True)
def _no_external(monkeypatch):
    scrub_credential_env(monkeypatch)
    block_external_http(monkeypatch)
    # 날짜별 캐시가 테스트 사이에 새면 판정이 앞 테스트 값에 묶인다.
    kis_client._trading_day_cache.clear()
    yield
    kis_client._trading_day_cache.clear()


def _stale_scan_frame() -> pd.DataFrame:
    return pd.DataFrame([{
        "scan_date": "20260917", "ticker": "005930", "name": "삼성전자",
        "market": "KOSPI", "sector": "전기전자", "close": 70000,
        "atr20": 1000.0, "atr_pct": 1.4, "high20": 71000,
        "high20_next": 71000, "low10": 68000, "gap": -1000,
        "gap_atr": -1.0, "dist_to_break": 1000, "dist_atr": 1.0,
    }])


def _arrange(monkeypatch, *, within_hours: bool, trading_day=None):
    """run()을 낡은 스캔 판정까지만 태우고 알림 발송을 잡아낸다.

    trading_day=None이면 _is_trading_day()를 바꿔치기하지 않는다
    (진짜 판정 로직을 그대로 태우고 싶을 때).
    """
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(kis_client, "_notify_warning_throttled",
                        lambda key, msg: sent.append((key, msg)))
    monkeypatch.setattr(kis_client, "_within_trading_hours",
                        lambda *a, **k: within_hours)
    if trading_day is not None:
        monkeypatch.setattr(kis_client, "_is_trading_day", lambda *a, **k: trading_day)

    # 자동매도·추가매수·10시 기록은 이 테스트 대상이 아니다.
    monkeypatch.setattr(kis_client, "run_auto_sell", lambda: None)
    monkeypatch.setattr(kis_client, "run_auto_pyramid", lambda: None)
    monkeypatch.setattr(intraday_watch, "_fill_10am_prices", lambda *a, **k: False)
    monkeypatch.setattr(intraday_watch, "load_alerted", lambda day: {})

    monkeypatch.setattr(intraday_watch.scan_all, "load_scan", _stale_scan_frame)
    # 직전 거래일을 20260918로 고정 → scan_date 20260917은 낡은 것이 된다.
    monkeypatch.setattr(intraday_watch.screener, "trading_days",
                        lambda end, count: ["20260918"])
    # 워치리스트가 비어 있으면 run()이 곧바로 끝난다 - 경고 분기는 그
    # 앞이라 이 테스트가 보려는 지점은 이미 지난 뒤다.
    monkeypatch.setattr(intraday_watch, "load_watchlist", lambda: pd.DataFrame())
    monkeypatch.setattr(intraday_watch, "_retry_candidates", lambda *a, **k: [])
    return sent


def test_no_kakao_on_non_trading_day(monkeypatch):
    """일요일: 장시간 안이어도 거래일이 아니면 보내지 않는다."""
    sent = _arrange(monkeypatch, within_hours=True, trading_day=False)
    intraday_watch.run()
    assert sent == []


def test_no_kakao_outside_trading_hours(monkeypatch):
    """거래일이어도 장시간 밖이면 보내지 않는다."""
    sent = _arrange(monkeypatch, within_hours=False, trading_day=True)
    intraday_watch.run()
    assert sent == []


def test_kakao_sent_on_trading_day_within_hours(monkeypatch):
    """평일 장중에는 기존대로 보낸다 - 이때는 실제로 진입이 막힌다."""
    sent = _arrange(monkeypatch, within_hours=True, trading_day=True)
    intraday_watch.run()
    assert len(sent) == 1
    key, msg = sent[0]
    assert key == kis_client.WARN_STALE_SCAN
    assert "20260917" in msg and "20260918" in msg


def test_fail_open_when_trading_day_unknown(monkeypatch):
    """거래일 판정이 안 되면 보낸다 - 조용해지는 쪽으로 틀리지 않는다.

    _is_trading_day()를 가짜로 바꾸지 않고 진짜를 태운다. 달력 조회가
    실패해(last_trading_date() → None) 판정이 안 되는 상황을 만든 뒤,
    그래도 경고가 나가는지 본다.
    """
    sent = _arrange(monkeypatch, within_hours=True)          # 진짜 판정 사용
    monkeypatch.setattr(kis_client.core, "last_trading_date", lambda: None)

    assert kis_client._is_trading_day() is True      # fail-open 확인
    intraday_watch.run()
    assert len(sent) == 1
