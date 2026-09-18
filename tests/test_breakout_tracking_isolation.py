"""추적 기능이 기존 매매 경로를 바꾸지 않는다는 것과, 게이트별 미진입
분류가 호출부에서 명시적으로 들어온다는 것을 확인한다.

여기서 검증하는 건 두 가지다.

1. **격리** - 추적 저장소가 꺼져 있든, 모든 호출에서 예외를 던지든,
   `select_buy_candidates()`의 선정 결과와 `place_market_buy_order()`
   호출 여부는 완전히 같아야 한다. 예외를 삼키는 것만으로는 부족해서
   주문 경로에서 노션 호출이 **한 번도** 일어나지 않는지도 같이 본다
   (동기 호출이 끼면 주문이 그만큼 늦어진다).
2. **분류** - '자금부족'·'상한제한'은 사유 문자열을 뒤져서가 아니라
   게이트가 직접 지정해 들어와야 한다.

실 네트워크는 tests/helpers.py의 차단 장치로 막고, 노션 쓰기·주문·
카톡은 전부 mock으로 대체한다.
"""

from __future__ import annotations

from unittest import mock

import pytest

import breakout_tracker as bt
import core
import kis_client
import notion_repo
from tests.helpers import block_external_http, scrub_credential_env

ACCOUNT_SIZE = 10_000_000.0


class _ExplodingStore:
    """모든 접근이 실패하는 저장소. 격리가 진짜인지 보는 용도."""

    def __init__(self) -> None:
        self.calls = 0

    def configured(self) -> bool:
        self.calls += 1
        return True

    def find_active(self, ticker):
        self.calls += 1
        raise RuntimeError('notion down')

    def list_tracks(self, include_closed=False):
        self.calls += 1
        raise RuntimeError('notion down')

    def create(self, record):
        self.calls += 1
        raise RuntimeError('notion down')

    def update(self, key, fields):
        self.calls += 1
        raise RuntimeError('notion down')


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    scrub_credential_env(monkeypatch)
    block_external_http(monkeypatch)
    previous = bt.set_store(bt.MemoryStore())
    bt._pending.clear()
    bt._sent_tickers.clear()
    bt._held_tickers.clear()
    yield
    bt.set_store(previous)
    bt._pending.clear()
    bt._sent_tickers.clear()
    bt._held_tickers.clear()


def _balance(cash: float) -> dict:
    return {"account_size": ACCOUNT_SIZE, "available_cash": cash,
            "deposit_total": cash, "holdings": []}


def _candidate(ticker: str, sector: str = "전기전자", price: float = 10_000.0,
               high20: float = 9_900.0) -> dict:
    return {"ticker": ticker, "name": ticker, "price": price, "atr20": 500.0,
            "gap_atr": (price - high20) / 500.0, "sector": sector,
            "market": "KOSPI", "high20": high20}


def _wire_gates(monkeypatch, cash: float, *, corr=None, success_today: int = 0):
    monkeypatch.setattr(kis_client, "MAX_ORDERS_PER_DAY", 3)
    monkeypatch.setattr(kis_client, "get_account_balance", lambda token: _balance(cash))
    monkeypatch.setattr(kis_client, "get_mock_account_corr_units",
                        lambda size, holdings: corr or {"groups": {}, "total_units": 0})
    monkeypatch.setattr(notion_repo, "fetch_unconfirmed_sell_orders", lambda *a: [])
    monkeypatch.setattr(notion_repo, "count_success_orders_today",
                        lambda day, acct: success_today)
    monkeypatch.setattr(notion_repo, "find_auto_holding_page", lambda ticker: None)
    monkeypatch.setattr(kis_client, "get_price_quote",
                        lambda token, ticker: {"price": 10_000.0, "market_warned": False})
    monkeypatch.setattr(kis_client, "_notify_failure", lambda msg: None)
    monkeypatch.setattr(kis_client, "_notify_warning_throttled", lambda k, m: None)


# ── 분류 ────────────────────────────────────────────────────

def test_cash_gate_records_cash_category(monkeypatch):
    """현금이 모자라 막힌 건은 '자금부족'으로 남는다."""
    _wire_gates(monkeypatch, cash=1_000.0)
    bt.record_breakout({**_candidate("000390"), "high20": 9_900.0, "atr20": 500.0})

    assert kis_client.select_buy_candidates("token", [_candidate("000390")]) == []

    row = bt.get_store().find_active("000390")
    assert row["outcome"] == bt.OUTCOME_CASH
    assert "현금 부족" in row["outcome_reason"]


def test_group_cap_records_cap_category(monkeypatch):
    """상관군 캡에 걸린 건은 '상한제한'으로 남는다."""
    _wire_gates(monkeypatch, cash=1_000_000_000.0,
                corr={"groups": {"전기전자": core.MAX_UNITS_GROUP},
                      "total_units": core.MAX_UNITS_GROUP})
    bt.record_breakout(_candidate("000390"))

    assert kis_client.select_buy_candidates("token", [_candidate("000390")]) == []

    row = bt.get_store().find_active("000390")
    assert row["outcome"] == bt.OUTCOME_CAP
    assert "상관군 캡" in row["outcome_reason"]


def test_daily_zero_cap_records_cap_category(monkeypatch):
    """신규매수 일일 상한 0(운영 보류)도 '상한제한'이다."""
    monkeypatch.setattr(kis_client, "MAX_ORDERS_PER_DAY", 0)
    bt.record_breakout(_candidate("000390"))

    assert kis_client.select_buy_candidates("token", [_candidate("000390")]) == []

    assert bt.get_store().find_active("000390")["outcome"] == bt.OUTCOME_CAP


def test_price_cap_is_not_reported_as_position_cap(monkeypatch):
    """'가격 상한 초과'는 유닛 상한과 다른 사건이라 '미주문'으로 남는다."""
    _wire_gates(monkeypatch, cash=1_000_000_000.0)
    expensive = _candidate("000390", price=kis_client.MAX_STOCK_PRICE + 1)
    bt.record_breakout(expensive)

    assert kis_client.select_buy_candidates("token", [expensive]) == []

    row = bt.get_store().find_active("000390")
    assert row["outcome"] == bt.OUTCOME_NOT_ORDERED
    assert "가격 상한 초과" in row["outcome_reason"]


# ── 격리 ────────────────────────────────────────────────────

@pytest.mark.parametrize("store", ["disabled", "exploding", "memory"])
def test_selection_is_identical_whatever_the_tracker_does(monkeypatch, store):
    """추적이 꺼져 있든 전부 터지든 선정 결과는 같아야 한다."""
    _wire_gates(monkeypatch, cash=1_000_000_000.0)
    if store == "disabled":
        bt.set_store(mock.Mock(configured=mock.Mock(return_value=False)))
    elif store == "exploding":
        bt.set_store(_ExplodingStore())

    selected = kis_client.select_buy_candidates("token", [_candidate("000390")])

    assert [c["ticker"] for c in selected] == ["000390"]
    assert selected[0]["unit_shares"] > 0


def test_order_path_makes_no_tracking_io_and_still_orders(monkeypatch):
    """운영 저장소를 붙인 채로 주문을 내도 추적 I/O가 한 번도 없어야 한다.

    `_guard`가 주문 경로의 기록을 메모리 큐에 쌓고 회차 끝(flush_events)에
    한꺼번에 처리하기 때문이다 - 종목마다 노션을 동기로 부르면 그만큼
    주문이 늦어진다.
    """
    _wire_gates(monkeypatch, cash=1_000_000_000.0)
    store = bt.NotionStore()
    touched = []
    monkeypatch.setattr(store, "configured", lambda: True)
    for name in ("find_active", "list_tracks", "create", "update"):
        monkeypatch.setattr(store, name,
                            mock.Mock(side_effect=lambda *a, **k: touched.append(1)))
    bt.set_store(store)

    monkeypatch.setattr(kis_client, "_auto_trade_configured", lambda: True)
    monkeypatch.setattr(kis_client, "_auto_trade_paused", lambda: False)
    monkeypatch.setattr(kis_client, "warn_pending_orders_at_startup", lambda: None)
    monkeypatch.setattr(kis_client, "_within_trading_hours", lambda: True)
    monkeypatch.setattr(kis_client, "_is_trading_day", lambda: True)
    monkeypatch.setattr(kis_client, "get_access_token", lambda: "token")
    order = mock.Mock(return_value={"status": "sent", "order_no": "A1"})
    monkeypatch.setattr(kis_client, "place_market_buy_order", order)
    monkeypatch.setattr(kis_client, "_record_holding_after_buy", lambda *a, **k: None)

    bt.record_breakout(_candidate("000390"))
    kis_client.run_auto_trade([_candidate("000390")])

    order.assert_called_once()
    assert touched == [], "주문 경로에서 추적 저장소를 건드리면 안 된다"
    assert bt._pending, "기록은 큐에 쌓여 회차 끝에 처리돼야 한다"


def test_flush_failure_does_not_escape(monkeypatch):
    """flush 중 저장소가 터져도 예외가 호출부로 새지 않는다."""
    store = _ExplodingStore()
    monkeypatch.setattr(store, "configured", lambda: True)
    bt.set_store(bt.NotionStore())
    monkeypatch.setattr(bt.get_store(), "configured", lambda: True)
    monkeypatch.setenv("BREAKOUT_TRACK_WRITER", "auto-trade")
    monkeypatch.setattr(bt.get_store(), "list_tracks",
                        mock.Mock(side_effect=RuntimeError("notion down")))

    bt.record_breakout(_candidate("000390"))
    bt.flush_events()          # 예외가 나면 테스트가 실패한다

    assert bt._pending == []


def test_intraday_run_flushes_even_when_the_round_raises(monkeypatch):
    """감시 회차가 예외로 끝나도 큐에 쌓인 추적 이벤트는 비운다."""
    import intraday_watch

    monkeypatch.setattr(intraday_watch, "_run",
                        mock.Mock(side_effect=RuntimeError("boom")))
    flush = mock.Mock()
    monkeypatch.setattr(bt, "flush_events", flush)

    with pytest.raises(RuntimeError):
        intraday_watch.run()

    flush.assert_called_once()


def test_dry_run_does_not_flush(monkeypatch):
    """미리보기 실행은 영속 기록을 만들지 않는다."""
    import intraday_watch

    monkeypatch.setattr(intraday_watch, "_run", mock.Mock(return_value=0))
    flush = mock.Mock()
    monkeypatch.setattr(bt, "flush_events", flush)

    intraday_watch.run(dry_run=True)

    flush.assert_not_called()
