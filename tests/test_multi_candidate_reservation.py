"""검토 항목 #3 검증 시나리오 - 여러 후보를 한 번에 승인할 때, 이미
승인한 후보의 예약 현금·유닛이 다음 후보 판정에 반영되는지.

select_buy_candidates()는 한 호출 안에서 여러 종목을 순서대로 보며
cash_remaining/group_units/total_units를 누적 소모한다. 이 예약이
"주문 직전 최신가 재검증"(KIS 조회) 이후에도 깨지지 않고 그대로
다음 후보에 넘어가는지 확인한다 - 최신가로 가격이 바뀌어도 반영된
unit_amount 그대로 cash_remaining에서 빠져야 한다.
"""

from __future__ import annotations

from unittest import mock

import core
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


def _setup_common(monkeypatch, cash: float, fresh_prices: dict[str, float]):
    monkeypatch.setattr(kis_client, "get_account_balance", lambda token: _balance(cash))
    monkeypatch.setattr(kis_client, "get_mock_account_corr_units",
                        lambda size, holdings: {"groups": {}, "total_units": 0})
    monkeypatch.setattr(notion_repo, "count_success_orders_today", lambda day, acct: 0)
    monkeypatch.setattr(kis_client, "_notify_failure", lambda msg: None)
    monkeypatch.setattr(kis_client, "_notify_warning_throttled", lambda key, msg: None)
    monkeypatch.setattr(
        kis_client, "get_price_quote",
        lambda token, ticker: {"price": fresh_prices[ticker], "market_warned": False},
    )


def test_second_candidate_blocked_by_cash_reserved_for_first(monkeypatch):
    # unit_shares = floor(10,000,000*0.005/500) = 100주 -> 1유닛 1,000,000원 안팎.
    # 현금이 딱 한 종목분(+비용)만 감당하도록 잡아, 두 번째 후보는
    # 첫 번째가 예약해간 현금 때문에 막혀야 한다.
    c1 = _candidate("000001", "전기전자", price=10_000.0, high20=9_900.0)
    c2 = _candidate("000002", "화학", price=10_000.0, high20=9_900.0)
    cash = 1_000_000.0 * 1.0025 + 1.0   # 딱 1종목분(비용 포함) + 약간의 여유
    _setup_common(monkeypatch, cash=cash,
                  fresh_prices={"000001": 10_000.0, "000002": 10_000.0})

    selected = kis_client.select_buy_candidates("dummy-token", [c1, c2])

    assert [s["ticker"] for s in selected] == ["000001"], (
        "첫 후보가 예약해간 현금 때문에 두 번째 후보는 이번 회차에 막혀야 한다"
    )


def test_second_candidate_in_same_sector_blocked_by_group_unit_cap(monkeypatch):
    # 같은 상관군(전기전자) 후보 core.MAX_UNITS_GROUP+1개를 주면, 캡을
    # 넘는 마지막 후보는 앞서 승인된 후보들의 유닛이 이미 반영돼 막혀야 한다.
    n = core.MAX_UNITS_GROUP + 1
    candidates = [
        _candidate(f"{i:06d}", "전기전자", price=10_000.0, high20=9_900.0)
        for i in range(n)
    ]
    fresh_prices = {c["ticker"]: 10_000.0 for c in candidates}
    _setup_common(monkeypatch, cash=1_000_000_000.0, fresh_prices=fresh_prices)
    # 이 시나리오는 상관군 캡만 따로 보려는 것이라, 하루 주문건수 상한
    # (MAX_ORDERS_PER_DAY=3)이 먼저 걸리지 않도록 넉넉히 늘려둔다.
    monkeypatch.setattr(kis_client, "MAX_ORDERS_PER_DAY", n)

    selected = kis_client.select_buy_candidates("dummy-token", candidates)

    assert len(selected) == core.MAX_UNITS_GROUP, (
        "상관군 캡을 넘는 후보는 앞서 예약된 유닛 때문에 막혀야 한다"
    )


def test_price_reverification_still_deducts_updated_amount_from_reservation(monkeypatch):
    # 첫 후보의 최신가(재검증)가 판정 시점보다 올라 unit_amount가 커졌으면,
    # 그 커진 금액 그대로 현금 예약에서 빠져야 한다 - 판정 시점(낮은)
    # 가격으로 예약했다면 통과했을 두 번째 후보가, 재검증 후 커진 예약
    # 때문에 막혀야 한다.
    c1 = _candidate("000001", "전기전자", price=9_950.0, high20=9_900.0)   # 판정 시점 가격
    c2 = _candidate("000002", "화학", price=9_950.0, high20=9_900.0)
    # unit_shares=100 -> 판정시점 unit_amount=995,000. 재검증 최신가는
    # 10,000원으로 올라 unit_amount=1,000,000이 된다(예약 차감은 비용
    # 미포함 unit_amount 기준). 그만큼 예약되고 나면, 두 번째 후보의
    # 비용 포함 필요금액(997,487.5원)을 감당하지 못할 만큼만 현금을 준다.
    cash = 1_997_000.0
    _setup_common(monkeypatch, cash=cash,
                  fresh_prices={"000001": 10_000.0, "000002": 9_950.0})

    selected = kis_client.select_buy_candidates("dummy-token", [c1, c2])

    assert [s["ticker"] for s in selected] == ["000001"]
    assert selected[0]["price"] == 10_000.0, "최신가로 갱신된 가격이 반영돼야 한다"
