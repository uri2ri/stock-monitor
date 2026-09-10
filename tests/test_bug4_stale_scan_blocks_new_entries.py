"""버그 #4 회귀 테스트 - 야간 스캔이 오래됐으면 신규매수 보류.

load_watchlist()가 scan_date를 로그에만 찍고 신선도를 검사하지 않아,
야간 스캔이 실패하거나 안 돌면(GitHub Actions 스케줄 실행 0건 전례
있음) 오래된 기준선·ATR로 계속 매수하고 있었다. scan_date가 "직전
거래일"이 아니면 신규 진입(run_auto_trade)만 보류하고, 보유 종목의
청산(run_auto_sell)·추가매수(run_auto_pyramid)는 스캔 신선도와 무관하게
계속 작동해야 한다.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from unittest import mock
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

import intraday_watch
import kis_client
import scan_all

KST = ZoneInfo("Asia/Seoul")


# ── _expected_scan_date() / _scan_is_stale() 단위 테스트 ──────

def test_expected_scan_date_skips_weekend():
    monday = date(2026, 9, 14)  # 월요일
    assert monday.weekday() == 0
    assert intraday_watch._expected_scan_date(monday) == date(2026, 9, 11)  # 금요일


def test_expected_scan_date_weekday_is_previous_day():
    thursday = date(2026, 9, 17)
    assert thursday.weekday() == 3
    assert intraday_watch._expected_scan_date(thursday) == date(2026, 9, 16)


def test_scan_is_stale_true_when_scan_date_too_old():
    today = date(2026, 9, 17)  # 목요일, 기대 scan_date = 9/16
    stale, expected = intraday_watch._scan_is_stale("20260914", today)  # 3일 전 월요일
    assert stale is True
    assert expected == date(2026, 9, 16)


def test_scan_is_stale_false_when_scan_date_matches_expected():
    today = date(2026, 9, 17)
    stale, expected = intraday_watch._scan_is_stale("20260916", today)
    assert stale is False


def test_scan_is_stale_true_when_missing_or_malformed():
    today = date(2026, 9, 17)
    assert intraday_watch._scan_is_stale(None, today)[0] is True
    assert intraday_watch._scan_is_stale("", today)[0] is True
    assert intraday_watch._scan_is_stale("not-a-date", today)[0] is True


# ── run() 통합 테스트 - 신규 진입만 보류, 매도·추가매수는 정상 ────

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


def test_run_blocks_new_entry_but_keeps_sell_and_pyramid_when_scan_stale(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(intraday_watch, "DATA_DIR", tmp_path)

    now = datetime.now(KST)
    stale_date = now.date() - timedelta(days=10)  # 명백히 오래된 기준일
    frame = _make_scan_frame(stale_date.strftime("%Y%m%d"))

    monkeypatch.setattr(scan_all, "load_scan", lambda: frame.copy())
    monkeypatch.setattr(intraday_watch, "load_corr_units", lambda: None)
    # 돌파 조건을 만족하는 가격을 준다 - 신선도 게이트가 없다면 여기서
    # 바로 자동매수까지 갔을 상황.
    monkeypatch.setattr(intraday_watch, "fetch_prices", lambda codes: {"005930": 10_400.0})
    monkeypatch.setattr(intraday_watch.kakao, "send_kakao_message", lambda msg: None)
    monkeypatch.setattr(kis_client, "_notify_warning_throttled", lambda key, msg: None)

    run_auto_sell_mock = mock.Mock()
    run_auto_pyramid_mock = mock.Mock()
    run_auto_trade_mock = mock.Mock()
    monkeypatch.setattr(kis_client, "run_auto_sell", run_auto_sell_mock)
    monkeypatch.setattr(kis_client, "run_auto_pyramid", run_auto_pyramid_mock)
    monkeypatch.setattr(kis_client, "run_auto_trade", run_auto_trade_mock)

    intraday_watch.run(dry_run=False)

    # 청산·추가매수는 스캔 신선도와 무관하게 항상 실행돼야 한다.
    run_auto_sell_mock.assert_called_once()
    run_auto_pyramid_mock.assert_called_once()
    # 신규 진입(자동매수)은 낡은 기준선으로는 나가면 안 된다.
    run_auto_trade_mock.assert_not_called()


def test_run_calls_auto_trade_when_scan_is_fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(intraday_watch, "DATA_DIR", tmp_path)

    now = datetime.now(KST)
    fresh_date = intraday_watch._expected_scan_date(now.date())
    frame = _make_scan_frame(fresh_date.strftime("%Y%m%d"))

    monkeypatch.setattr(scan_all, "load_scan", lambda: frame.copy())
    monkeypatch.setattr(intraday_watch, "load_corr_units", lambda: None)
    monkeypatch.setattr(intraday_watch, "fetch_prices", lambda codes: {"005930": 10_400.0})
    monkeypatch.setattr(intraday_watch.kakao, "send_kakao_message", lambda msg: None)
    monkeypatch.setattr(kis_client, "run_auto_sell", lambda: None)
    monkeypatch.setattr(kis_client, "run_auto_pyramid", lambda: None)

    run_auto_trade_mock = mock.Mock()
    monkeypatch.setattr(kis_client, "run_auto_trade", run_auto_trade_mock)

    intraday_watch.run(dry_run=False)

    run_auto_trade_mock.assert_called_once()
    tickers = [c["ticker"] for c in run_auto_trade_mock.call_args.args[0]]
    assert "005930" in tickers
