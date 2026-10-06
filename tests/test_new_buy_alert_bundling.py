"""신규매수 거절 알림: 현금 게이트 앞당기기 + 사유별 묶음 발송.

배경: 2026-10-06 신규매수를 재개(MAX_ORDERS_PER_DAY 0→3)하자마자 거절
카톡이 폭주했다. 워치리스트 100종목 중 재시도 대상이 43종목이고, 후보마다
_notify_failure(억제 없음)를 부르며, 그게 10분마다 반복됐다. 실제로
카카오가 429로 토큰 발급을 거부해(run 37403047468 로그에 22회 연속)
거절 알림이 **주문 실패·체결 미확인 같은 중요한 알림까지 밀어냈다**.

두 가지로 고쳤고 여기서 검증한다.
  1) 가용현금이 어떤 후보의 1유닛 금액에도 못 미치면 후보 루프를 돌기
     전에 한 번만 알리고 끝낸다(조회 비용도 안 쓴다).
  2) 루프 안의 일상적 거절(가격 상한·유닛금액·상관군 캡·현금·우선순위·
     재검증류)은 쌓아 뒀다가 **한 통**으로 묶어 억제해 보낸다.

조회 실패·데이터 이상(기존 보유 확인 실패, ATR 이상, 시세 조회 실패 등)은
고장 신호라 기존 즉시 알림 경로를 그대로 쓴다 - 묶이면 안 된다.
종목별 상세는 카톡이 아니라 노션 돌파 추적(_track_no_entry)에 남는다.
"""

from __future__ import annotations

from unittest import mock

import pytest

import kis_client
import notion_repo
from helpers import block_external_http, scrub_credential_env


def _candidate(ticker, sector="전기전자", *, price=10_000.0, atr=500.0,
               high20=9_900.0, name=None):
    return {
        "ticker": ticker, "name": name or f"종목{ticker}", "sector": sector,
        "price": price, "atr20": atr, "high20": high20, "gap_atr": 0.2,
        "market": "KOSPI",
    }


def _balance(cash, account_size=10_000_000.0):
    return {"account_size": account_size, "available_cash": cash,
            "holdings": []}


@pytest.fixture
def gates(monkeypatch):
    """외부 호출을 전부 막고 알림만 잡는다."""
    scrub_credential_env(monkeypatch)
    block_external_http(monkeypatch)
    monkeypatch.setattr(kis_client, "MAX_ORDERS_PER_DAY", 3)
    monkeypatch.setattr(kis_client, "get_mock_account_corr_units",
                        lambda size, holdings: {"groups": {}, "total_units": 0})
    monkeypatch.setattr(notion_repo, "count_success_orders_today",
                        lambda day, acct: 0)
    monkeypatch.setattr(notion_repo, "fetch_unconfirmed_sell_orders", lambda *a: [])
    monkeypatch.setattr(notion_repo, "find_auto_holding_page", lambda ticker: None)
    monkeypatch.setattr(kis_client, "get_price_quote",
                        lambda token, ticker: {"price": 10_000.0,
                                               "market_warned": False})
    failures = mock.Mock()
    monkeypatch.setattr(kis_client, "_notify_failure", failures)
    throttled: list[tuple[str, str]] = []
    monkeypatch.setattr(kis_client, "_notify_warning_throttled",
                        lambda k, m: throttled.append((k, m)))
    tracked: list[tuple[str, str]] = []
    monkeypatch.setattr(
        kis_client, "_track_no_entry",
        lambda c, reason, *a: tracked.append((c["ticker"], reason)))
    return {"failures": failures, "throttled": throttled, "tracked": tracked}


def _msgs(gates, key):
    return [m for k, m in gates["throttled"] if k == key]


# ── 1. 현금 게이트를 루프 앞으로 당긴다 ──────────────────────

def test_no_cash_sends_one_alert_before_the_loop(gates, monkeypatch):
    """어떤 후보도 못 사면 후보별 검사 없이 한 통만 보낸다."""
    monkeypatch.setattr(kis_client, "get_account_balance",
                        lambda token: _balance(207_738.0))
    # 보유 조회를 하면 루프에 들어갔다는 뜻이다 - 호출되면 실패시킨다.
    monkeypatch.setattr(notion_repo, "find_auto_holding_page",
                        lambda ticker: pytest.fail("루프에 진입했다"))
    quotes = mock.Mock(side_effect=AssertionError("시세 조회까지 갔다"))
    monkeypatch.setattr(kis_client, "get_price_quote", quotes)

    candidates = [_candidate(f"00000{i}") for i in range(1, 8)]
    selected = kis_client.select_buy_candidates("tok", candidates)

    assert selected == []
    notices = _msgs(gates, kis_client.WARN_NEW_BUY_NO_CASH)
    assert len(notices) == 1                      # 후보가 7개여도 한 통
    assert "현금 부족" in notices[0]
    assert "207,738" in notices[0]                # 가용현금
    assert "7종목" in notices[0]
    gates["failures"].assert_not_called()
    quotes.assert_not_called()


def test_no_cash_still_records_every_candidate_in_tracking(gates, monkeypatch):
    """카톡을 묶는 대신 추적에는 종목별로 남아야 한다 - 정보가 사라지면 안 된다."""
    monkeypatch.setattr(kis_client, "get_account_balance",
                        lambda token: _balance(207_738.0))

    candidates = [_candidate("000001"), _candidate("000002")]
    kis_client.select_buy_candidates("tok", candidates)

    assert {t for t, _ in gates["tracked"]} == {"000001", "000002"}
    for _, reason in gates["tracked"]:
        assert "현금 부족" in reason
        assert "필요" in reason and "가용" in reason   # 금액이 남는다


def test_affordable_candidate_still_enters_the_loop(gates, monkeypatch):
    """살 수 있는 후보가 하나라도 있으면 조기 종료하지 않는다.

    조기 종료가 과하게 걸리면 살 수 있었던 주문을 조용히 건너뛴다 -
    이게 이 최적화의 유일한 위험이라 명시적으로 고정한다.
    """
    unit = kis_client.core.calc_position(500.0, 10_000_000.0).unit_shares
    assert unit > 0
    monkeypatch.setattr(kis_client, "get_account_balance",
                        lambda token: _balance(unit * 10_000.0 + 1_000_000))

    selected = kis_client.select_buy_candidates("tok", [_candidate("000001")])

    assert len(selected) == 1                     # 통과했다
    assert _msgs(gates, kis_client.WARN_NEW_BUY_NO_CASH) == []


# ── 2. 루프 안 거절은 한 통으로 묶는다 ───────────────────────

def test_many_rejections_become_one_message(gates, monkeypatch):
    """사유별로 묶이고, 종목이 많으면 접힌다."""
    monkeypatch.setattr(kis_client, "get_account_balance",
                        lambda token: _balance(1_000_000_000.0))
    # 상한을 0자리로 만들어 전부 "우선순위 밀림"으로 떨어뜨린다.
    monkeypatch.setattr(notion_repo, "count_success_orders_today",
                        lambda day, acct: 3)

    candidates = [_candidate(f"{i:06d}", name=f"종목{i}") for i in range(1, 9)]
    selected = kis_client.select_buy_candidates("tok", candidates)

    assert selected == []
    summaries = _msgs(gates, kis_client.WARN_NEW_BUY_REJECTED)
    assert len(summaries) == 1                    # 8종목 → 한 통
    body = summaries[0]
    assert "거절 8건" in body
    assert "우선순위 밀림 8건" in body
    assert "외 3종목" in body                      # 5개만 나열하고 접는다
    gates["failures"].assert_not_called()


def test_rejection_categories_are_grouped(gates, monkeypatch):
    """사유가 섞이면 사유별로 나뉘어 한 통에 담긴다."""
    monkeypatch.setattr(kis_client, "get_account_balance",
                        lambda token: _balance(1_000_000_000.0))
    over_cap = _candidate("000001", price=kis_client.MAX_STOCK_PRICE + 1,
                          name="비싼종목")
    # 상관군 캡을 0으로 만들어 두 번째 후보를 캡에서 떨어뜨린다.
    monkeypatch.setattr(kis_client, "core", kis_client.core)
    monkeypatch.setattr(kis_client.core, "MAX_UNITS_TOTAL", 0)

    kis_client.select_buy_candidates("tok", [over_cap, _candidate("000002")])

    body = _msgs(gates, kis_client.WARN_NEW_BUY_REJECTED)[0]
    assert "가격 상한 초과 1건" in body
    assert "상관군 캡 1건" in body
    assert "비싼종목" in body


def test_no_rejections_sends_nothing(gates, monkeypatch):
    """살 수 있으면 거절 알림이 아예 없다."""
    monkeypatch.setattr(kis_client, "get_account_balance",
                        lambda token: _balance(1_000_000_000.0))

    selected = kis_client.select_buy_candidates("tok", [_candidate("000001")])

    assert len(selected) == 1
    assert _msgs(gates, kis_client.WARN_NEW_BUY_REJECTED) == []


# ── 3. 고장 신호는 묶이지 않는다 ────────────────────────────

def test_lookup_failure_still_alerts_immediately(gates, monkeypatch):
    """기존 보유 확인 실패는 시장 상황이 아니라 고장이다 - 즉시 알린다."""
    monkeypatch.setattr(kis_client, "get_account_balance",
                        lambda token: _balance(1_000_000_000.0))
    monkeypatch.setattr(
        notion_repo, "find_auto_holding_page",
        lambda ticker: (_ for _ in ()).throw(RuntimeError("노션 다운")))

    kis_client.select_buy_candidates("tok", [_candidate("000001")])

    gates["failures"].assert_called_once()
    assert "기존 보유 여부 확인 실패" in gates["failures"].call_args.args[0]
    # 고장 알림이 묶음에 섞여 들어가면 안 된다.
    assert _msgs(gates, kis_client.WARN_NEW_BUY_REJECTED) == []


def test_quote_failure_still_alerts_immediately(gates, monkeypatch):
    """주문 직전 시세 조회 실패도 즉시 알림 유지."""
    monkeypatch.setattr(kis_client, "get_account_balance",
                        lambda token: _balance(1_000_000_000.0))
    monkeypatch.setattr(
        kis_client, "get_price_quote",
        lambda token, ticker: (_ for _ in ()).throw(RuntimeError("시세 다운")))

    kis_client.select_buy_candidates("tok", [_candidate("000001")])

    gates["failures"].assert_called_once()
    assert "최신 시세 조회 실패" in gates["failures"].call_args.args[0]


# ── 묶음 포맷 단위 검증 ─────────────────────────────────────

def test_summary_format_orders_by_count(monkeypatch):
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(kis_client, "_notify_warning_throttled",
                        lambda k, m: sent.append((k, m)))

    kis_client._notify_new_buy_rejections(
        [("현금 부족", "가"), ("상관군 캡", "나"), ("현금 부족", "다"),
         ("현금 부족", "라")],
        207_738.0,
    )

    key, body = sent[0]
    assert key == kis_client.WARN_NEW_BUY_REJECTED
    # 건수 많은 사유가 먼저 - 지금 무엇이 가장 많이 막는지가 먼저 보여야 한다.
    assert body.index("현금 부족 3건") < body.index("상관군 캡 1건")
    assert "거절 4건" in body and "207,738" in body


def test_summary_not_sent_when_empty(monkeypatch):
    sent = []
    monkeypatch.setattr(kis_client, "_notify_warning_throttled",
                        lambda k, m: sent.append((k, m)))
    kis_client._notify_new_buy_rejections([], 0.0)
    assert sent == []
