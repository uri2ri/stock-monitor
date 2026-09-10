"""버그 #2 회귀 테스트 - 알림 완료가 자동매수 재시도까지 막던 문제.

예전에는 돌파를 알린 순간 주문 성공 여부와 무관하게 alerted에 기록해,
그 종목이 다음 회차 워치리스트에서 아예 빠졌다(재시도 불가). 이제
alerted는 "카톡 알림을 오늘 보냈는가"만 의미하고, "오늘 다시 사려
시도할까"는 매 회차 notion_repo.has_order_today()로 새로 판단한다.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest import mock
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

import intraday_watch
import kis_client
import notion_repo
import scan_all
from tests.helpers import mock_trading_days

KST = ZoneInfo("Asia/Seoul")


# ── _retry_candidates() 단위 테스트 ──────────────────────────

def test_retry_candidates_skips_status_chase():
    alerted = {"000001": {"status": intraday_watch.STATUS_CHASE, "name": "X"}}
    with mock.patch.object(notion_repo, "has_order_today") as m:
        result = intraday_watch._retry_candidates(alerted, datetime.now(KST).date())
    m.assert_not_called()
    assert result == []


def test_retry_candidates_included_when_no_order_recorded():
    alerted = {
        "005930": {
            "status": intraday_watch.STATUS_ENTER, "name": "삼성전자",
            "sector": "전기전자", "market": "KOSPI", "price": 10_400.0,
            "high20": 10_300.0, "atr20": 500.0, "gap_atr": 0.2, "unit_shares": 10,
        }
    }
    with mock.patch.object(notion_repo, "has_order_today", return_value=False) as m:
        result = intraday_watch._retry_candidates(alerted, datetime.now(KST).date())
    m.assert_called_once()
    assert m.call_args.kwargs["side"] == notion_repo.SIDE_BUY
    assert m.call_args.kwargs["order_type"] == notion_repo.ORDER_NEW
    assert len(result) == 1
    assert result[0]["ticker"] == "005930"
    assert result[0]["unit_shares"] == 10


def test_retry_candidates_blocked_when_order_already_recorded():
    alerted = {"005930": {"status": intraday_watch.STATUS_ENTER, "name": "삼성전자"}}
    with mock.patch.object(notion_repo, "has_order_today", return_value=True):
        result = intraday_watch._retry_candidates(alerted, datetime.now(KST).date())
    assert result == []


def test_retry_candidates_fail_closed_on_lookup_error():
    alerted = {"005930": {"status": intraday_watch.STATUS_ENTER, "name": "삼성전자"}}
    with mock.patch.object(notion_repo, "has_order_today",
                           side_effect=RuntimeError("노션 조회 실패")):
        result = intraday_watch._retry_candidates(alerted, datetime.now(KST).date())
    assert result == [], "조회 실패 시 재시도하지 않아야 한다(fail-closed)"


# ── run() 통합 테스트 - 주문 실패 후 다음 회차 재시도 ─────────

def _make_scan_frame(scan_date: str) -> pd.DataFrame:
    row = {
        "scan_date": scan_date, "ticker": "005930", "name": "삼성전자",
        "market": "KOSPI", "sector": "전기전자", "close": 10_000,
        "atr20": 500.0, "atr_pct": 5.0, "high20": 10_300.0,
        "high20_next": 10_300.0, "low10": 9_000.0, "gap": -300,
        "gap_atr": -0.6, "dist_to_break": 300, "dist_atr": 0.6,
        "vol_mult": 1.0, "unit_shares": 10, "value_avg_20": 1_000_000_000.0,
        "status": scan_all.STATUS_NEAR,
    }
    return pd.DataFrame([row])


def test_run_retries_next_cycle_after_order_failure_then_stops_once_ordered(tmp_path, monkeypatch):
    monkeypatch.setattr(intraday_watch, "DATA_DIR", tmp_path)

    now = datetime.now(KST)
    expected_scan_date = now.date() - timedelta(days=1)
    mock_trading_days(monkeypatch, expected_scan_date)
    frame = _make_scan_frame(expected_scan_date.strftime("%Y%m%d"))

    monkeypatch.setattr(scan_all, "load_scan", lambda: frame.copy())
    monkeypatch.setattr(intraday_watch, "load_corr_units", lambda: None)
    monkeypatch.setattr(intraday_watch, "fetch_prices", lambda codes: {"005930": 10_400.0})
    monkeypatch.setattr(kis_client, "run_auto_sell", lambda: None)
    monkeypatch.setattr(kis_client, "run_auto_pyramid", lambda: None)
    monkeypatch.setattr(intraday_watch.kakao, "send_kakao_message", lambda msg: None)

    # 1회차 - 돌파를 잡아 알림은 나가지만, 주문(run_auto_trade)은 실패한다.
    run_auto_trade_mock = mock.Mock(side_effect=RuntimeError("KIS 일시 오류"))
    monkeypatch.setattr(kis_client, "run_auto_trade", run_auto_trade_mock)
    with mock.patch.object(notion_repo, "has_order_today", return_value=False):
        intraday_watch.run(dry_run=False)

    assert run_auto_trade_mock.call_count == 1
    first_call_tickers = [c["ticker"] for c in run_auto_trade_mock.call_args.args[0]]
    assert "005930" in first_call_tickers

    alerted_after_1 = intraday_watch.load_alerted(now.strftime("%Y%m%d"))
    assert "005930" in alerted_after_1, "주문 실패해도 알림 이력 자체는 남아야 한다(중복 알림 방지)"

    # 2회차 - 아직 노션에 주문 기록이 없다(has_order_today=False) -> 재시도돼야 한다.
    run_auto_trade_mock2 = mock.Mock()
    monkeypatch.setattr(kis_client, "run_auto_trade", run_auto_trade_mock2)
    with mock.patch.object(notion_repo, "has_order_today", return_value=False):
        intraday_watch.run(dry_run=False)

    assert run_auto_trade_mock2.call_count == 1, "주문 실패 후에도 다음 회차에 재시도돼야 한다"
    second_call_tickers = [c["ticker"] for c in run_auto_trade_mock2.call_args.args[0]]
    assert "005930" in second_call_tickers

    # 3회차 - 노션에 이미 주문 기록이 있다(성공/주문중/실패 중 하나) -> 재시도 중단.
    run_auto_trade_mock3 = mock.Mock()
    monkeypatch.setattr(kis_client, "run_auto_trade", run_auto_trade_mock3)
    with mock.patch.object(notion_repo, "has_order_today", return_value=True):
        intraday_watch.run(dry_run=False)

    assert run_auto_trade_mock3.call_count == 0, "이미 주문 기록이 있으면 더 이상 재시도하면 안 된다"
