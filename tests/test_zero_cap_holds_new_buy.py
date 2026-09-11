"""신규매수 운영 보류(MAX_ORDERS_PER_DAY=0) 시 후보별 거절 카톡이
반복되던 문제의 최소 수정 검증.

select_buy_candidates()는 MAX_ORDERS_PER_DAY == 0이면 가격·현금 등
후보별 검사와 그때마다의 거절 알림(_notify_failure) 전에 바로 빈
목록을 반환해야 한다. 반면 상한이 3처럼 정상값이고 그날 이미 다
채워 remaining_slots == 0이 된 경우는 기존 후보별 검사·알림 흐름을
그대로 타야 한다(이번 수정 대상이 아님). 자동매도·추가매수 경로는
select_buy_candidates를 호출하지 않으므로 이 값에 영향받지 않는다.
"""

from __future__ import annotations

from unittest import mock

import kis_client
import notion_repo

ACCOUNT_SIZE = 10_000_000.0


def _balance(cash: float) -> dict:
    return {"account_size": ACCOUNT_SIZE, "available_cash": cash,
            "deposit_total": cash, "holdings": []}


def _candidate(ticker: str, sector: str, price: float, high20: float) -> dict:
    return {
        "ticker": ticker, "name": ticker, "price": price, "atr20": 500.0,
        "gap_atr": (price - high20) / 500.0, "sector": sector,
        "market": "KOSPI", "high20": high20,
    }


def test_zero_cap_returns_empty_before_any_candidate_check_or_notification(monkeypatch):
    monkeypatch.setattr(kis_client, "MAX_ORDERS_PER_DAY", 0)

    balance_mock = mock.Mock(side_effect=AssertionError(
        "일일 상한 0이면 계좌 조회조차 하지 말고 바로 반환해야 한다"))
    notify_failure_mock = mock.Mock()
    monkeypatch.setattr(kis_client, "get_account_balance", balance_mock)
    monkeypatch.setattr(kis_client, "_notify_failure", notify_failure_mock)

    candidates = [
        _candidate("000001", "전기전자", price=10_000.0, high20=9_900.0),
        _candidate("000002", "화학", price=10_000.0, high20=9_900.0),
    ]

    selected = kis_client.select_buy_candidates("dummy-token", candidates)

    assert selected == []
    balance_mock.assert_not_called()
    notify_failure_mock.assert_not_called()


def test_zero_cap_logs_operation_hold(monkeypatch, caplog):
    monkeypatch.setattr(kis_client, "MAX_ORDERS_PER_DAY", 0)

    with caplog.at_level("INFO", logger="kis_client"):
        selected = kis_client.select_buy_candidates("dummy-token", [
            _candidate("000001", "전기전자", price=10_000.0, high20=9_900.0),
        ])

    assert selected == []
    assert any("신규매수 운영 보류: 일일 상한 0" in r.message for r in caplog.records)


def test_nonzero_cap_still_runs_existing_candidate_checks_and_limit_notification(monkeypatch):
    # 상한이 정상값(3)이고 이미 그날 상한만큼 성공 주문이 쌓여
    # remaining_slots == 0이 된 경우 - 이번 수정 대상이 아니므로 기존처럼
    # 후보별 검사(가격·유닛금액·상관군·현금)를 그대로 거친 뒤 "우선순위
    # 밀림" 알림을 내야 한다.
    monkeypatch.setattr(kis_client, "MAX_ORDERS_PER_DAY", 3)
    monkeypatch.setattr(kis_client, "get_account_balance",
                        lambda token: _balance(1_000_000_000.0))
    monkeypatch.setattr(kis_client, "get_mock_account_corr_units",
                        lambda size, holdings: {"groups": {}, "total_units": 0})
    monkeypatch.setattr(notion_repo, "count_success_orders_today", lambda day, acct: 3)
    monkeypatch.setattr(
        kis_client, "get_price_quote",
        lambda token, ticker: {"price": 10_000.0, "market_warned": False},
    )
    notify_failure_mock = mock.Mock()
    monkeypatch.setattr(kis_client, "_notify_failure", notify_failure_mock)
    monkeypatch.setattr(kis_client, "_notify_warning_throttled", lambda k, m: None)

    candidate = _candidate("000001", "전기전자", price=10_000.0, high20=9_900.0)
    selected = kis_client.select_buy_candidates("dummy-token", [candidate])

    assert selected == []
    notify_failure_mock.assert_called_once()
    assert "우선순위 밀림" in notify_failure_mock.call_args.args[0]


def test_pyramid_add_order_unaffected_by_zero_new_buy_cap(monkeypatch):
    # 추가매수(ORDER_ADD)는 _check_order_allowed에서 MAX_PYRAMID_ORDERS_PER_DAY를
    # 쓴다 (kis_client.py:636) - 신규매수 상한(MAX_ORDERS_PER_DAY)이 0이어도
    # 이 캡과는 무관하게 정상 동작해야 한다.
    monkeypatch.setattr(kis_client, "MAX_ORDERS_PER_DAY", 0)
    monkeypatch.setattr(kis_client, "MAX_PYRAMID_ORDERS_PER_DAY", 4)
    monkeypatch.setattr(notion_repo, "count_orders_today", lambda *a, **k: 0)
    monkeypatch.setattr(notion_repo, "count_rejected_orders_today", lambda *a, **k: 0)
    monkeypatch.setattr(
        notion_repo, "count_orders_by_status_today",
        lambda *a, **k: {"성공": 3, "주문중": 0},
    )

    reason = kis_client._check_order_allowed("005930", order_type=notion_repo.ORDER_ADD)

    assert reason is None, "신규매수 상한이 0이어도 추가매수는 자기 캡(4)으로 판단해야 한다"


def test_auto_sell_run_auto_sell_never_references_new_buy_cap(monkeypatch):
    # 자동매도는 select_buy_candidates를 아예 거치지 않으므로
    # MAX_ORDERS_PER_DAY 값(0 포함)에 영향받지 않아야 한다.
    monkeypatch.setattr(kis_client, "MAX_ORDERS_PER_DAY", 0)
    select_mock = mock.Mock(
        side_effect=AssertionError("자동매도 경로는 select_buy_candidates를 부르면 안 된다"))
    monkeypatch.setattr(kis_client, "select_buy_candidates", select_mock)
    monkeypatch.setattr(kis_client, "_auto_trade_configured", lambda: False)

    kis_client.run_auto_sell(holdings=[])

    select_mock.assert_not_called()
