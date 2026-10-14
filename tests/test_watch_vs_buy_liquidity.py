"""감시 대상(3억)과 자동매수 허용 대상(5억) 분리.

야간 스캔(scan_all.py)의 기록 하한을 20일 평균 거래대금 5억 → 3억
(WATCH_MIN_TRADING_VALUE)으로 낮췄다. 그대로 두면 3~5억 종목이 워치리스트를
거쳐 자동매수까지 가므로, kis_client.select_buy_candidates에 유동성 게이트
(core.BUY_MIN_TRADING_VALUE, 5억)를 같이 넣었다. 여기서 확인하는 것:

  1) 3~5억 종목이 스캔·워치리스트에 오르고, 돌파하면 [관찰·매수보류]
     알림이 가지만 매수 후보에서는 빠진다(사유는 돌파 추적·거절 묶음에).
  2) value_avg_20이 없거나 무효면 매수에서 뺀다(fail-closed).
  3) 5억 이상 종목은 기존과 같이 [돌파] 알림·매수 후보가 된다.
  4) value_avg_5·vol_active_days가 없는 구버전 CSV도 그대로 읽힌다.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest import mock
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

import core
import intraday_watch
import kis_client
import notion_repo
import scan_all
import screener
from helpers import block_external_http, mock_trading_days, scrub_credential_env

KST = ZoneInfo("Asia/Seoul")

ILLIQUID_VALUE = 400_000_000.0     # 감시는 되지만 매수는 안 되는 구간(3~5억)
LIQUID_VALUE = 1_000_000_000.0


def test_watch_floor_is_below_buy_floor():
    assert scan_all.WATCH_MIN_TRADING_VALUE == 300_000_000
    assert core.BUY_MIN_TRADING_VALUE == 500_000_000
    # 기존 한도는 건드리지 않는다.
    assert kis_client.MAX_ORDERS_PER_DAY == 3


# ── 1. 야간 스캔 ────────────────────────────────────────────

def _scan_days(n: int = 30) -> list[str]:
    return [d.strftime("%Y%m%d")
            for d in pd.bdate_range("2026-09-01", periods=n)]


def _ohlcv(ticker: str, days: list[str], volumes: list[float]) -> pd.DataFrame:
    rows = []
    for i, (day, vol) in enumerate(zip(days, volumes)):
        close = 10_000.0 + (50.0 if i % 2 else 0.0)
        rows.append({"날짜": day, "티커": ticker, "시가": close,
                     "고가": close + 300.0, "저가": close - 300.0,
                     "종가": close, "거래량": vol})
    return pd.DataFrame(rows)


@pytest.fixture
def scanned(monkeypatch):
    """scan()을 합성 시세로 끝까지 돌린다(KRX 호출 없음).

    000001: 평소 3.5억, 최근 5일 6억 → 20일 평균 약 4.1억(감시 O, 매수 X)
    000002: 하루 2억 → 3억 미만이라 기록하지 않는다
    000003: 하루 10억 → 기존과 같이 기록
    """
    days = _scan_days()
    # 종가가 10,000/10,050을 오가므로 거래량 35,000주 ≈ 3.5억.
    a_vol = [35_000.0] * 25 + [60_000.0] * 5
    hist = pd.concat([
        _ohlcv("000001", days, a_vol),
        _ohlcv("000002", days, [20_000.0] * 30),
        _ohlcv("000003", days, [100_000.0] * 30),
    ], ignore_index=True)
    tickers = ["000001", "000002", "000003"]
    meta_frame = pd.DataFrame(
        {"종목명": ["저유동", "초저유동", "고유동"], "시장": ["KOSDAQ"] * 3},
        index=tickers,
    )

    monkeypatch.setattr(screener, "check_krx_auth", lambda: None)
    monkeypatch.setattr(screener, "trading_days", lambda end, n: days)
    monkeypatch.setattr(scan_all, "ensure_history", lambda d: (hist, days[-1]))
    monkeypatch.setattr(screener, "load_meta",
                        lambda day: SimpleNamespace(frame=meta_frame, notes=[]))
    monkeypatch.setattr(screener, "prefilter_meta",
                        lambda meta: screener.Prefilter(tickers=tickers, total=3))
    monkeypatch.setattr(screener, "load_sector_map", lambda day: {})

    frame, _ = scan_all.scan()
    return frame.set_index("ticker")


def test_scan_records_3_to_5_eok_and_drops_below_3(scanned):
    assert "000001" in scanned.index          # 3~5억: 기록(감시 대상)
    assert "000003" in scanned.index          # 5억 이상: 기존과 같다
    assert "000002" not in scanned.index      # 3억 미만: 기록하지 않는다

    low = scanned.loc["000001"]
    assert 300_000_000 <= low["value_avg_20"] < 500_000_000
    assert not core.is_buy_liquid(low["value_avg_20"])


def test_scan_adds_recent_value_columns(scanned):
    assert set(scanned.reset_index().columns) == set(scan_all.SCAN_COLUMNS)

    low = scanned.loc["000001"]
    # 최근 5일 = 하루 60,000주 × 약 10,000원 ≈ 6억, 전부 20일 평균(≈4.1억) 초과
    assert low["value_avg_5"] == pytest.approx(6.0e8, rel=0.01)
    assert low["vol_active_days"] == 5

    flat = scanned.loc["000003"]
    assert flat["value_avg_5"] == pytest.approx(flat["value_avg_20"], rel=0.01)
    # 거래량이 일정해 종가가 10,050원인 날(홀수 번째 날)만 평균을 넘는다 -
    # 30일 중 마지막 5일(25~29번째)에서는 25·27·29번째, 3일.
    assert flat["vol_active_days"] == 3


# ── 공통: 스캔 CSV 행 ───────────────────────────────────────

def _scan_row(ticker, name, value_avg_20, *, scan_date="20260930",
              value_avg_5=None, vol_active_days=None, late_columns=True):
    row = {
        "scan_date": scan_date, "ticker": ticker, "name": name,
        "market": "KOSDAQ", "sector": "전기전자", "close": 10_000,
        "atr20": 500.0, "atr_pct": 5.0, "high20": 10_300.0,
        "high20_next": 10_300.0, "low10": 9_000.0, "gap": -300,
        "gap_atr": -0.6, "dist_to_break": 300, "dist_atr": 0.6,
        "vol_mult": 1.0, "unit_shares": 10, "value_avg_20": value_avg_20,
        "status": scan_all.STATUS_NEAR,
    }
    if late_columns:
        row["value_avg_5"] = value_avg_5
        row["vol_active_days"] = vol_active_days
    return row


# ── 2. 매수 게이트 ──────────────────────────────────────────

def _candidate(ticker, value_avg_20=LIQUID_VALUE, *, name=None, omit_value=False):
    c = {
        "ticker": ticker, "name": name or f"종목{ticker}", "sector": "전기전자",
        "price": 10_400.0, "atr20": 500.0, "high20": 10_300.0, "gap_atr": 0.2,
        "market": "KOSPI", "value_avg_20": value_avg_20,
    }
    if omit_value:
        del c["value_avg_20"]
    return c


@pytest.fixture
def gates(monkeypatch):
    scrub_credential_env(monkeypatch)
    block_external_http(monkeypatch)
    monkeypatch.setattr(kis_client, "MAX_ORDERS_PER_DAY", 3)
    monkeypatch.setattr(kis_client, "get_account_balance",
                        lambda token: {"account_size": 10_000_000.0,
                                       "available_cash": 1_000_000_000.0,
                                       "holdings": []})
    monkeypatch.setattr(kis_client, "get_mock_account_corr_units",
                        lambda size, holdings: {"groups": {}, "total_units": 0})
    monkeypatch.setattr(notion_repo, "count_success_orders_today",
                        lambda day, acct: 0)
    monkeypatch.setattr(notion_repo, "fetch_unconfirmed_sell_orders", lambda *a: [])
    monkeypatch.setattr(notion_repo, "find_auto_holding_page", lambda ticker: None)
    quotes = mock.Mock(return_value={"price": 10_400.0, "market_warned": False})
    monkeypatch.setattr(kis_client, "get_price_quote", quotes)
    failures = mock.Mock()
    monkeypatch.setattr(kis_client, "_notify_failure", failures)
    throttled: list[tuple[str, str]] = []
    monkeypatch.setattr(kis_client, "_notify_warning_throttled",
                        lambda k, m: throttled.append((k, m)))
    tracked: list[tuple[str, str]] = []
    monkeypatch.setattr(
        kis_client, "_track_no_entry",
        lambda c, reason, *a: tracked.append((c["ticker"], reason)))
    return SimpleNamespace(quotes=quotes, failures=failures,
                           throttled=throttled, tracked=tracked)


def _rejection_msgs(gates):
    return [m for k, m in gates.throttled if k == kis_client.WARN_NEW_BUY_REJECTED]


def test_illiquid_candidate_is_excluded_with_tracked_reason(gates):
    selected = kis_client.select_buy_candidates(
        "tok", [_candidate("000001", ILLIQUID_VALUE, name="저유동")])

    assert selected == []
    assert gates.tracked == [("000001", "유동성 미달(20일 4.0억 < 5억)")]
    # 조회 없는 산술 게이트 - 시세 조회까지 가지 않는다.
    gates.quotes.assert_not_called()
    gates.failures.assert_not_called()
    # 거절 묶음 알림에 합류한다.
    msgs = _rejection_msgs(gates)
    assert len(msgs) == 1
    assert "유동성 미달 1건: 저유동" in msgs[0]


@pytest.mark.parametrize("value", [None, float("nan"), float("inf"), 0, -1, "abc"])
def test_invalid_value_avg_20_is_fail_closed(gates, value):
    selected = kis_client.select_buy_candidates("tok", [_candidate("000001", value)])

    assert selected == []
    assert gates.tracked == [("000001", "유동성 미달(20일 확인불가 < 5억)")]
    gates.quotes.assert_not_called()


def test_missing_value_avg_20_key_is_fail_closed(gates):
    selected = kis_client.select_buy_candidates(
        "tok", [_candidate("000001", omit_value=True)])

    assert selected == []
    assert gates.tracked == [("000001", "유동성 미달(20일 확인불가 < 5억)")]


@pytest.mark.parametrize("value", [500_000_000, LIQUID_VALUE])
def test_liquid_candidate_passes_as_before(gates, value):
    selected = kis_client.select_buy_candidates("tok", [_candidate("000003", value)])

    assert [c["ticker"] for c in selected] == ["000003"]
    assert gates.tracked == []
    assert _rejection_msgs(gates) == []


def test_illiquid_does_not_take_a_slot_from_liquid(gates):
    """유동성 미달 후보가 갭이 더 낮아도 매수 자리를 차지하지 않는다."""
    low = {**_candidate("000001", ILLIQUID_VALUE), "gap_atr": 0.05}
    high = {**_candidate("000003", LIQUID_VALUE), "gap_atr": 0.3}

    selected = kis_client.select_buy_candidates("tok", [low, high])

    assert [c["ticker"] for c in selected] == ["000003"]
    assert [t for t, _ in gates.tracked] == ["000001"]


# ── 3. 알림 ─────────────────────────────────────────────────

NOW = datetime(2026, 10, 1, 9, 40, tzinfo=KST)


def _hit(ticker, name, value_avg_20, value_avg_5=None, active=None,
         status=intraday_watch.STATUS_ENTER, gap_atr=0.2):
    return {"ticker": ticker, "name": name, "sector": "전기전자",
            "price": 10_400.0, "high20": 10_300.0, "atr20": 500.0,
            "gap_atr": gap_atr, "unit_shares": 10, "status": status,
            "value_avg_20": value_avg_20, "value_avg_5": value_avg_5,
            "vol_active_days": active}


def test_watch_only_alert_is_separate_from_entry_alert(monkeypatch):
    monkeypatch.delenv("STREAMLIT_APP_URL", raising=False)
    hits = [
        _hit("000003", "고유동", LIQUID_VALUE),
        _hit("000001", "저유동", ILLIQUID_VALUE, 610_000_000.0, 3),
    ]

    messages = intraday_watch.build_messages(hits, NOW)

    assert len(messages) == 2
    entry, watch = messages
    assert entry.startswith("[돌파]09:40")
    assert "고유동" in entry and "저유동" not in entry
    assert watch == "[관찰·매수보류] 저유동 돌파 · 20일 4.0억 / 5일 6.1억 (활성 3/5일)"


def test_watch_only_alerts_are_bundled_into_one_message(monkeypatch):
    monkeypatch.delenv("STREAMLIT_APP_URL", raising=False)
    hits = [_hit(f"0000{i:02d}", f"저유동{i}", ILLIQUID_VALUE, 3.5e8, 1,
                 gap_atr=0.01 * i) for i in range(1, 11)]

    messages = intraday_watch.build_messages(hits, NOW)

    assert len(messages) == 1                     # 10종목이어도 한 통
    msg = messages[0]
    assert msg.startswith("[관찰·매수보류] 저유동1 돌파 · 20일 4.0억 / 5일 3.5억 (활성 1/5일)")
    assert msg.count("[관찰·매수보류]") == 1
    assert len(msg) <= intraday_watch.kakao.MAX_TEXT_LEN
    assert "외 " in msg and msg.endswith("종목")   # 넘치는 건 접는다


def test_chase_zone_illiquid_is_not_alerted():
    hits = [_hit("000001", "저유동", ILLIQUID_VALUE,
                 status=intraday_watch.STATUS_CHASE, gap_atr=0.8)]
    assert intraday_watch.build_messages(hits, NOW) == []


def test_missing_value_goes_to_watch_only_with_unknowns():
    messages = intraday_watch.build_messages([_hit("000001", "구버전", None)], NOW)
    assert messages == ["[관찰·매수보류] 구버전 돌파 · 20일 ?억 / 5일 ?억 (활성 ?/5일)"]


def test_liquid_only_alert_unchanged(monkeypatch):
    monkeypatch.delenv("STREAMLIT_APP_URL", raising=False)
    messages = intraday_watch.build_messages([_hit("000003", "고유동", LIQUID_VALUE)], NOW)
    assert messages == ["[돌파]09:40\n고유동 10,400 +0.20N"]


# ── 1~3 통합: 워치리스트 → 돌파 → 알림 → 매수 후보 ────────────

def test_end_to_end_watch_alert_but_no_buy(tmp_path, monkeypatch, gates):
    monkeypatch.setattr(intraday_watch, "DATA_DIR", tmp_path)
    now = datetime.now(KST)
    fresh = now.date() - timedelta(days=1)
    mock_trading_days(monkeypatch, fresh)
    frame = pd.DataFrame([
        _scan_row("000003", "고유동", LIQUID_VALUE, scan_date=fresh.strftime("%Y%m%d"),
                  value_avg_5=LIQUID_VALUE, vol_active_days=2),
        _scan_row("000001", "저유동", ILLIQUID_VALUE, scan_date=fresh.strftime("%Y%m%d"),
                  value_avg_5=610_000_000.0, vol_active_days=3),
    ])
    monkeypatch.setattr(scan_all, "load_scan", lambda: frame.copy())
    monkeypatch.setattr(intraday_watch, "load_corr_units", lambda: None)
    monkeypatch.setattr(intraday_watch, "fetch_prices",
                        lambda codes: {c: 10_400.0 for c in codes})
    monkeypatch.setattr(kis_client, "run_auto_sell", lambda: None)
    monkeypatch.setattr(kis_client, "run_auto_pyramid", lambda: None)
    monkeypatch.delenv("STREAMLIT_APP_URL", raising=False)
    sent = mock.Mock()
    monkeypatch.setattr(intraday_watch.kakao, "send_kakao_message", sent)
    run_auto_trade = mock.Mock()
    monkeypatch.setattr(kis_client, "run_auto_trade", run_auto_trade)

    # 워치리스트에는 둘 다 오른다.
    assert set(intraday_watch.load_watchlist()["ticker"]) == {"000001", "000003"}

    intraday_watch.run(dry_run=False)

    texts = [c.args[0] for c in sent.call_args_list]
    assert any(t.startswith("[돌파]") and "고유동" in t and "저유동" not in t
               for t in texts)
    assert "[관찰·매수보류] 저유동 돌파 · 20일 4.0억 / 5일 6.1억 (활성 3/5일)" in texts

    # 자동매수에는 진입가능 둘 다 넘어가고, 게이트가 저유동을 뺀다.
    candidates = run_auto_trade.call_args.args[0]
    assert {c["ticker"] for c in candidates} == {"000001", "000003"}
    selected = kis_client.select_buy_candidates("tok", candidates)
    assert [c["ticker"] for c in selected] == ["000003"]
    assert gates.tracked == [("000001", "유동성 미달(20일 4.0억 < 5억)")]

    # 하루 1회 알림(alerted)은 그대로 - 둘 다 이력에 남고 유동성 값도 같이 남는다.
    alerted = intraday_watch.load_alerted(now.strftime("%Y%m%d"))
    assert set(alerted) == {"000001", "000003"}
    assert alerted["000001"]["value_avg_20"] == ILLIQUID_VALUE
    assert alerted["000001"]["vol_active_days"] == 3

    # 다음 회차: 이미 알린 종목은 다시 알리지 않고, 저유동은 재시도 대상도 아니다.
    sent.reset_mock()
    run_auto_trade.reset_mock()
    monkeypatch.setattr(notion_repo, "has_order_today", lambda *a, **k: False)
    intraday_watch.run(dry_run=False)
    sent.assert_not_called()
    retried = run_auto_trade.call_args.args[0]
    assert [c["ticker"] for c in retried] == ["000003"]
    assert retried[0]["value_avg_20"] == LIQUID_VALUE


def test_retry_skips_alerted_record_without_value(monkeypatch):
    """배포 전에 저장된 알림 이력(value_avg_20 없음)은 재시도하지 않는다(fail-closed)."""
    monkeypatch.setattr(notion_repo, "has_order_today", lambda *a, **k: False)
    alerted = {"005930": {"status": intraday_watch.STATUS_ENTER, "name": "삼성전자",
                          "price": 10_400.0, "high20": 10_300.0, "atr20": 500.0}}
    assert intraday_watch._retry_candidates(alerted, NOW.date()) == []


# ── 4. 구버전 CSV ───────────────────────────────────────────

def test_old_csv_without_late_columns_loads(tmp_path, monkeypatch):
    path = tmp_path / "scan_latest.csv"
    monkeypatch.setattr(scan_all, "LATEST_PATH", path)
    pd.DataFrame([
        _scan_row("000003", "고유동", LIQUID_VALUE, late_columns=False),
        _scan_row("000001", "저유동", ILLIQUID_VALUE, late_columns=False),
    ]).to_csv(path, index=False, encoding="utf-8-sig")

    frame = scan_all.load_scan()
    assert frame is not None
    for col in scan_all.LATE_COLUMNS:
        assert col in frame.columns
        assert frame[col].isna().all()

    watch = intraday_watch.load_watchlist()
    assert set(watch["ticker"]) == {"000001", "000003"}

    hits = {h["ticker"]: h for h in
            (intraday_watch.judge(row, 10_400.0) for _, row in watch.iterrows())}
    assert hits["000003"]["value_avg_20"] == LIQUID_VALUE
    assert hits["000003"]["value_avg_5"] is None
    assert hits["000003"]["vol_active_days"] is None
    # 20일 평균은 구버전에도 있으므로 매수 판정은 그대로 된다.
    assert core.is_buy_liquid(hits["000003"]["value_avg_20"])
    assert not core.is_buy_liquid(hits["000001"]["value_avg_20"])

    messages = intraday_watch.build_messages(list(hits.values()), NOW)
    assert messages[-1] == "[관찰·매수보류] 저유동 돌파 · 20일 4.0억 / 5일 ?억 (활성 ?/5일)"
