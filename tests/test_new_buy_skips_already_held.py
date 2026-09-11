"""신규매수 경로가 이미 보유 중인 종목(매도 미확인 상태 포함)에 다시
들어가지 않는지 확인하는 회귀 테스트.

select_buy_candidates()의 상관군 캡 게이트는 "이 종목의 기존 유닛은 0"을
전제로 stock_units_after=1을 고정한다. 이미 자동으로 보유 중인 종목
(정상 보유 중이든, 매도를 접수했지만 아직 체결 확인 전이든 -
SELL_CONFIRMATION.md 참고, 둘 다 노션엔 "보유"로 남는다)이 워치리스트에
다시 걸리면 이 전제가 깨져 캡을 우회한 채 기존 보유 위에 조용히
병합돼버린다(_record_holding_after_buy의 find_auto_holding_page 병합
경로) - 매도 확인이 끝나지 않은 종목에 새 매수 주문이 겹칠 수도 있다.
"""
from __future__ import annotations

from unittest import mock

import kis_client
import notion_repo

ACCOUNT_SIZE = 10_000_000.0


def _balance(cash: float = ACCOUNT_SIZE) -> dict:
    return {"account_size": ACCOUNT_SIZE, "available_cash": cash,
            "deposit_total": cash, "holdings": []}


def _candidate(ticker="000001", price=10_000.0, high20=9_900.0, sector="전기전자") -> dict:
    return {
        "ticker": ticker, "name": ticker, "price": price, "atr20": 500.0,
        "gap_atr": (price - high20) / 500.0, "sector": sector,
        "market": "KOSPI", "high20": high20,
    }


def _setup(monkeypatch, *, find_page):
    monkeypatch.setattr(kis_client, "MAX_ORDERS_PER_DAY", 3)
    monkeypatch.setattr(kis_client, "get_account_balance", lambda token: _balance())
    monkeypatch.setattr(kis_client, "get_mock_account_corr_units",
                        lambda size, holdings: {"groups": {}, "total_units": 0})
    monkeypatch.setattr(notion_repo, "count_success_orders_today", lambda day, acct: 0)
    monkeypatch.setattr(notion_repo, "find_auto_holding_page", find_page)
    monkeypatch.setattr(kis_client, "get_price_quote",
                        lambda token, ticker: {"price": 10_000.0, "market_warned": False})
    monkeypatch.setattr(kis_client, "_notify_failure", mock.Mock())
    monkeypatch.setattr(kis_client, "_notify_warning_throttled", mock.Mock())


def test_already_held_ticker_is_skipped_not_merged(monkeypatch, caplog):
    _setup(monkeypatch, find_page=lambda ticker: "existing-page")
    with caplog.at_level("INFO", logger="kis_client"):
        selected = kis_client.select_buy_candidates("dummy-token", [_candidate()])
    assert selected == []
    kis_client._notify_failure.assert_not_called()
    assert any("이미 자동 보유 중" in r.message for r in caplog.records)


def test_pending_sell_still_marked_holding_blocks_new_buy(monkeypatch):
    # SELL_CONFIRMATION.md: 매도 접수 후 체결 확인 전까지 노션엔 여전히
    # "보유"로 남는다 - find_auto_holding_page가 그 행을 그대로 찾아낸다.
    _setup(monkeypatch, find_page=lambda ticker: "still-open-page")
    selected = kis_client.select_buy_candidates("dummy-token", [_candidate()])
    assert selected == []


def test_holding_lookup_failure_blocks_only_that_candidate(monkeypatch):
    def find_page(ticker):
        if ticker == "000001":
            raise RuntimeError("notion offline")
        return None
    _setup(monkeypatch, find_page=find_page)
    candidates = [_candidate("000001", high20=9_900.0),
                 _candidate("000002", high20=9_900.0)]
    selected = kis_client.select_buy_candidates("dummy-token", candidates)
    assert [c["ticker"] for c in selected] == ["000002"]
    assert any("기존 보유 여부 확인 실패" in c.args[0]
              for c in kis_client._notify_failure.call_args_list)


def test_new_ticker_not_held_passes_through(monkeypatch):
    _setup(monkeypatch, find_page=lambda ticker: None)
    selected = kis_client.select_buy_candidates("dummy-token", [_candidate()])
    assert [c["ticker"] for c in selected] == ["000001"]
