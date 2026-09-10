"""버그 #3 회귀 테스트 - 주문 직전 최신 가격 재검증.

후보 판정은 네이버 현재가로 하는데, 그 뒤 KIS 응답에서 시장경보만 읽고
가격은 갱신하지 않았다. 판정과 실제 주문 사이 시차 동안 가격이 올라
추격범위(0.5×ATR)를 벗어나거나 돌파 자체가 무효화됐을 수 있는데 그걸
못 잡았다. 이제 select_buy_candidates()가 시장경보 조회에 얹어 받은
최신가로 돌파 유지·추격범위·유닛금액·현금여력을 다시 확인한다.
"""

from __future__ import annotations

from unittest import mock

import pytest

import core
import kis_client
import notion_repo

ACCOUNT_SIZE = 10_000_000.0


def _balance(cash: float = ACCOUNT_SIZE) -> dict:
    return {"account_size": ACCOUNT_SIZE, "available_cash": cash,
            "deposit_total": cash, "holdings": []}


def _candidate(price: float = 10_400.0) -> dict:
    return {
        "ticker": "005930", "name": "삼성전자", "price": price,
        "atr20": 500.0, "gap_atr": (price - 10_300.0) / 500.0,
        "sector": "전기전자", "market": "KOSPI", "high20": 10_300.0,
    }


def _run_select(monkeypatch, candidate, fresh_price=None, fresh_quote_error=None,
                 cash=ACCOUNT_SIZE):
    monkeypatch.setattr(kis_client, "get_account_balance", lambda token: _balance(cash))
    monkeypatch.setattr(kis_client, "get_mock_account_corr_units",
                        lambda size, holdings: {"groups": {}, "total_units": 0})
    monkeypatch.setattr(notion_repo, "count_success_orders_today", lambda day, acct: 0)
    monkeypatch.setattr(kis_client, "_notify_failure", lambda msg: None)
    monkeypatch.setattr(kis_client, "_notify_warning_throttled", lambda key, msg: None)

    if fresh_quote_error is not None:
        def _quote(token, ticker):
            raise fresh_quote_error
        monkeypatch.setattr(kis_client, "get_price_quote", _quote)
    else:
        monkeypatch.setattr(
            kis_client, "get_price_quote",
            lambda token, ticker: {"price": fresh_price, "market_warned": False},
        )

    return kis_client.select_buy_candidates("dummy-token", [candidate])


def test_price_still_within_chase_range_updates_price_and_proceeds(monkeypatch):
    candidate = _candidate(price=10_400.0)   # 판정 시점 가격(네이버)
    fresh_price = 10_500.0                    # 그 사이 조금 더 올랐지만 추격범위(10550) 안
    selected = _run_select(monkeypatch, candidate, fresh_price=fresh_price)

    assert len(selected) == 1
    assert selected[0]["price"] == fresh_price, "최신가로 갱신돼야 한다"


def test_price_moved_beyond_chase_range_blocks_order(monkeypatch):
    candidate = _candidate(price=10_400.0)
    # high20=10300, atr=500, 추격 허용 상한 = 10300+0.5*500 = 10550
    fresh_price = 10_800.0                    # 허용범위를 한참 초과
    selected = _run_select(monkeypatch, candidate, fresh_price=fresh_price)

    assert selected == [], "주문 직전 추격범위를 벗어났으면 주문을 막아야 한다"


def test_price_dropped_below_high20_blocks_order(monkeypatch):
    candidate = _candidate(price=10_400.0)
    fresh_price = 10_250.0                    # high20(10300) 아래로 되돌림 - 돌파 무효
    selected = _run_select(monkeypatch, candidate, fresh_price=fresh_price)

    assert selected == [], "돌파가 유지되지 않으면 주문을 막아야 한다"


def test_fresh_price_cash_shortfall_blocks_order(monkeypatch):
    candidate = _candidate(price=10_400.0)
    # unit_shares = floor(10,000,000*0.005/500) = 100주.
    # 판정 시점(네이버) 가격 10400원 기준 unit_amount=1,040,000원은 이
    # 현금(1,045,000원)에 들어가지만, 최신가(KIS) 10500원 기준
    # unit_amount=1,050,000원은 못 들어간다 - "재검증 없이 판정 시점
    # 가격으로만 현금을 확인했다면"(옛 코드) 통과했을 경계값이다.
    selected = _run_select(monkeypatch, candidate, fresh_price=10_500.0,
                            cash=1_045_000.0)
    assert selected == [], "최신가 기준으로 재계산한 현금이 부족하면 막아야 한다"


def test_price_quote_failure_falls_back_to_original_price(monkeypatch):
    # 기존 fail-open 동작 유지 확인 - 최신가 조회 자체가 실패하면
    # 판정 시점 가격 그대로 진행한다(시장경보도 모르니 그대로 통과).
    candidate = _candidate(price=10_400.0)
    selected = _run_select(monkeypatch, candidate,
                           fresh_quote_error=RuntimeError("타임아웃"))
    assert len(selected) == 1
    assert selected[0]["price"] == 10_400.0
