"""버그 #4 회귀 테스트 - 야간 스캔이 오래됐으면 신규매수 보류.

load_watchlist()가 scan_date를 로그에만 찍고 신선도를 검사하지 않아,
야간 작업(scan_all.py)이 실패하거나 스케줄이 아예 안 돌면(GitHub
Actions 스케줄 실행 0건 전례 있음) scan_latest.csv가 며칠 전
기준선·ATR을 그대로 들고 있는데, 장중 감시는 그걸 알 방법이 없어
계속 낡은 기준으로 매수하고 있었다. scan_date가 "직전 거래일"이
아니면 신규 진입(run_auto_trade)만 보류하고, 보유 종목의
청산(run_auto_sell)·추가매수(run_auto_pyramid)는 스캔 신선도와 무관하게
계속 작동해야 한다.

이번 라운드: "직전 거래일"을 주말만 건너뛰는 로컬 계산이 아니라
screener.trading_days()(scan_all.py 자체 스캔이 이미 쓰는 KRX 거래일
달력 접근자)로 구한다 - 공휴일·거래소 특별 휴장일 다음날에도 정확해야
하고, 달력 조회 자체가 실패하면 "평일이니 어제"로 추정하지 않고
신선도를 "확인할 수 없음"(=신선하지 않음, 보류)으로 처리해야 한다.
원격 환경에서는 실제 KRX를 조회하지 않고 screener.trading_days를
그대로 monkeypatch로 대체한다.
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
import screener
from tests.helpers import mock_trading_days

KST = ZoneInfo("Asia/Seoul")


# ── _expected_trading_day() / _scan_is_stale() 단위 테스트 ────

def test_expected_trading_day_uses_calendar_not_local_weekday_skip():
    # 월요일 - 로컬 주말스킵 계산이면 금요일이 나오지만, 여기서는
    # screener.trading_days()가 돌려주는 값을 그대로 신뢰해야 한다
    # (예: 그 주 금요일이 임시공휴일이었다면 실제 직전 거래일은 목요일).
    monday = date(2026, 9, 14)
    with mock.patch.object(
        screener, "trading_days", return_value=["20260910"],  # 목요일
    ) as m:
        result = intraday_watch._expected_trading_day(monday)
    assert result == date(2026, 9, 10)
    # count=1 뒤에서 하나만 요청한다 - end는 today-1일.
    args, kwargs = m.call_args
    assert args[0] == monday - timedelta(days=1)
    assert args[1] == 1


def test_expected_trading_day_handles_day_after_holiday():
    # 화요일이 오늘이고, 월요일이 임시공휴일이라 직전 거래일이 금요일인
    # 경우(3일 연속 휴장: 토·일·월) - 달력이 이걸 올바르게 돌려준다고
    # 가정하고, 그 값을 그대로 쓰는지만 확인한다(로컬 계산이었다면
    # 월요일이 나왔을 잘못된 경우).
    tuesday = date(2026, 9, 15)
    with mock.patch.object(screener, "trading_days", return_value=["20260911"]):
        result = intraday_watch._expected_trading_day(tuesday)
    assert result == date(2026, 9, 11)  # 금요일


def test_expected_trading_day_none_when_calendar_unavailable():
    with mock.patch.object(
        screener, "trading_days", side_effect=screener.KrxUnavailable("장애"),
    ):
        result = intraday_watch._expected_trading_day(date(2026, 9, 17))
    assert result is None, "달력 조회 실패는 '평일이니 어제'로 추정하지 않고 None"


def test_expected_trading_day_none_when_calendar_returns_empty():
    with mock.patch.object(screener, "trading_days", return_value=[]):
        result = intraday_watch._expected_trading_day(date(2026, 9, 17))
    assert result is None


def test_expected_trading_day_none_on_unexpected_exception():
    # KrxUnavailable이 아닌 다른 예외(네트워크 라이브러리 내부 오류 등)도
    # 같이 fail-closed로 처리해야 한다 - 예외 타입을 좁게 잡지 않는다.
    with mock.patch.object(screener, "trading_days", side_effect=RuntimeError("boom")):
        result = intraday_watch._expected_trading_day(date(2026, 9, 17))
    assert result is None


def test_scan_is_stale_true_when_scan_date_too_old():
    with mock.patch.object(screener, "trading_days", return_value=["20260916"]):
        stale, expected = intraday_watch._scan_is_stale("20260914", date(2026, 9, 17))
    assert stale is True
    assert expected == date(2026, 9, 16)


def test_scan_is_stale_false_when_scan_date_matches_expected():
    with mock.patch.object(screener, "trading_days", return_value=["20260916"]):
        stale, expected = intraday_watch._scan_is_stale("20260916", date(2026, 9, 17))
    assert stale is False
    assert expected == date(2026, 9, 16)


def test_scan_is_stale_true_when_missing_or_malformed():
    with mock.patch.object(screener, "trading_days", return_value=["20260916"]):
        assert intraday_watch._scan_is_stale(None, date(2026, 9, 17))[0] is True
        assert intraday_watch._scan_is_stale("", date(2026, 9, 17))[0] is True
        assert intraday_watch._scan_is_stale("not-a-date", date(2026, 9, 17))[0] is True


def test_scan_is_stale_true_when_scan_date_is_in_the_future():
    # 시계 오류 등으로 미래 날짜가 찍힌 스캔도 "직전 거래일이 아님"으로
    # 걸린다(단순 비교라 미래·과거 모두 자동으로 커버된다).
    with mock.patch.object(screener, "trading_days", return_value=["20260916"]):
        stale, expected = intraday_watch._scan_is_stale("20260920", date(2026, 9, 17))
    assert stale is True
    assert expected == date(2026, 9, 16)


def test_scan_is_stale_true_when_calendar_unavailable_even_if_scan_date_plausible():
    # 달력을 못 믿으면(직전 거래일 자체를 모름) scan_date가 그럴듯한
    # 평일 날짜여도 "확인할 수 없으니 안전하게 신선하지 않음"으로 본다 -
    # "평일이라는 이유만으로 추정하지 않는다"는 요구사항의 핵심.
    with mock.patch.object(screener, "trading_days", return_value=[]):
        stale, expected = intraday_watch._scan_is_stale("20260916", date(2026, 9, 17))
    assert stale is True
    assert expected is None


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
    expected_date = now.date() - timedelta(days=1)
    mock_trading_days(monkeypatch, expected_date)
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
    fresh_date = now.date() - timedelta(days=1)
    mock_trading_days(monkeypatch, fresh_date)
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


def test_run_blocks_new_entry_but_keeps_sell_when_calendar_unavailable(
    tmp_path, monkeypatch,
):
    # 달력 조회 자체가 실패하는 경우(KrxUnavailable) - "평일이니 어제"로
    # 추정하지 않고 신규 진입을 보류해야 한다. 청산·추가매수는 계속
    # 작동해야 한다(신선도 검사는 신규 진입 경로에만 적용).
    monkeypatch.setattr(intraday_watch, "DATA_DIR", tmp_path)

    now = datetime.now(KST)
    frame = _make_scan_frame((now.date() - timedelta(days=1)).strftime("%Y%m%d"))

    monkeypatch.setattr(scan_all, "load_scan", lambda: frame.copy())
    monkeypatch.setattr(intraday_watch, "load_corr_units", lambda: None)
    monkeypatch.setattr(intraday_watch, "fetch_prices", lambda codes: {"005930": 10_400.0})
    monkeypatch.setattr(intraday_watch.kakao, "send_kakao_message", lambda msg: None)
    monkeypatch.setattr(kis_client, "_notify_warning_throttled", lambda key, msg: None)
    monkeypatch.setattr(screener, "trading_days",
                        mock.Mock(side_effect=screener.KrxUnavailable("장애")))

    run_auto_sell_mock = mock.Mock()
    run_auto_pyramid_mock = mock.Mock()
    run_auto_trade_mock = mock.Mock()
    monkeypatch.setattr(kis_client, "run_auto_sell", run_auto_sell_mock)
    monkeypatch.setattr(kis_client, "run_auto_pyramid", run_auto_pyramid_mock)
    monkeypatch.setattr(kis_client, "run_auto_trade", run_auto_trade_mock)

    intraday_watch.run(dry_run=False)

    run_auto_sell_mock.assert_called_once()
    run_auto_pyramid_mock.assert_called_once()
    run_auto_trade_mock.assert_not_called()
