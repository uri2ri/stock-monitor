"""검토 항목 #5 검증 시나리오 - 카카오 발송 실패가 주문 상태를 성공으로
바꾸거나 재시도를 소진시키지 않는지.

run()은 판정(hits) -> 카톡 발송 -> 자동매수(run_auto_trade) -> 알림
이력 저장 순서다. 카톡 발송은 토큰 만료 등으로 실패할 수 있는데, 이
실패가 (a) 실제 매매(run_auto_trade 호출)를 막아서는 안 되고, (b) 주문
상태 자체를 조작(성공으로 위조)해서도 안 되며, (c) 알림 이력 저장을
막아 다음 회차에 같은 종목이 또 알림 대상이 되게 해서도(카톡 중복
발송) 안 된다. 재시도 여부의 진실은 언제나 notion_repo.has_order_today
(주문 상태)이지 alerted(카톡 이력)가 아니다 - 이 분리가 카톡 실패로
깨지지 않아야 한다.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest import mock
from zoneinfo import ZoneInfo

import pandas as pd

import intraday_watch
import kis_client
import scan_all
from tests.helpers import mock_trading_days

KST = ZoneInfo("Asia/Seoul")


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


def test_kakao_send_failure_does_not_block_auto_trade_or_alert_history(tmp_path, monkeypatch):
    monkeypatch.setattr(intraday_watch, "DATA_DIR", tmp_path)

    now = datetime.now(KST)
    fresh_date = now.date() - timedelta(days=1)
    mock_trading_days(monkeypatch, fresh_date)
    frame = _make_scan_frame(fresh_date.strftime("%Y%m%d"))

    monkeypatch.setattr(scan_all, "load_scan", lambda: frame.copy())
    monkeypatch.setattr(intraday_watch, "load_corr_units", lambda: None)
    monkeypatch.setattr(intraday_watch, "fetch_prices", lambda codes: {"005930": 10_400.0})
    monkeypatch.setattr(kis_client, "run_auto_sell", lambda: None)
    monkeypatch.setattr(kis_client, "run_auto_pyramid", lambda: None)

    # 카톡 발송이 매번 실패한다(예: 토큰 만료).
    monkeypatch.setattr(intraday_watch.kakao, "send_kakao_message",
                        mock.Mock(side_effect=RuntimeError("카카오 토큰 만료")))

    run_auto_trade_mock = mock.Mock()
    monkeypatch.setattr(kis_client, "run_auto_trade", run_auto_trade_mock)

    intraday_watch.run(dry_run=False)

    # (a) 카톡이 실패해도 실제 매매 호출은 그대로 나가야 한다.
    run_auto_trade_mock.assert_called_once()
    tickers = [c["ticker"] for c in run_auto_trade_mock.call_args.args[0]]
    assert "005930" in tickers

    # (c) 알림 이력도 카톡 실패와 무관하게 저장돼야 한다(다음 회차
    # 중복 판정을 피하기 위한 기록이지, 주문 상태의 진실이 아니다).
    alerted = intraday_watch.load_alerted(now.strftime("%Y%m%d"))
    assert "005930" in alerted


def test_alert_history_does_not_gate_order_retry_only_notion_does(tmp_path, monkeypatch):
    # alerted에 이미 있어도(카톡은 갔음) 노션에 주문 기록이 없으면
    # (has_order_today=False) 다음 회차에 실제 매수는 재시도돼야 한다 -
    # 카톡 이력과 주문 재시도 판단이 서로 다른 데이터에 근거한다는 확인.
    monkeypatch.setattr(intraday_watch, "DATA_DIR", tmp_path)
    now = datetime.now(KST)
    day = now.strftime("%Y%m%d")

    alerted = {
        "005930": {
            "status": intraday_watch.STATUS_ENTER, "name": "삼성전자",
            "sector": "전기전자", "market": "KOSPI", "price": 10_400.0,
            "high20": 10_300.0, "atr20": 500.0, "gap_atr": 0.2, "unit_shares": 10,
        }
    }
    intraday_watch.save_alerted(day, alerted)

    import notion_repo
    with mock.patch.object(notion_repo, "has_order_today", return_value=False):
        retry = intraday_watch._retry_candidates(alerted, now.date())
    assert len(retry) == 1 and retry[0]["ticker"] == "005930"
