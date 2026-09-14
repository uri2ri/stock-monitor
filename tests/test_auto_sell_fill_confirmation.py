"""자동매도 체결 확인 버그 회귀 테스트.

문제: place_market_sell_order()는 주문 API 응답(rt_cd=0)만 보고
status="sent"를 돌려주는데, 이건 "주문 접수"이지 "전량 체결"이 아니다.
예전 run_auto_sell()은 "sent"만 보고 곧바로 보유종목 점검표를 청산
처리하고, 체결가 조회가 실패하거나 미체결/부분체결이어도 판정 시점
가격으로 매매일지를 확정 기록했다 - 미체결·부분체결인데도 노션에서는
청산된 것처럼 보여 잔여 보유 감시가 빠지고 손익 기록이 틀릴 수 있었다.

수정 후 흐름(_check_sell_fill/_write_ledger_and_close/_finalize_pending_sell):
  - "성공"(주문 접수)과 "전량 체결"을 분리한다 - 접수만으로는 아무것도
    확정하지 않는다.
  - 전량 체결(filled_qty >= requested_qty) + 유효한 체결가(avg_price > 0)를
    모두 확인해야만 매매일지를 기록하고, 그게 성공해야만 점검표를
    청산 처리한다(순서 고정 - 반대로 하면 청산 처리 후 매매일지 기록
    실패 시 다음 실행이 이 행을 더는 못 찾는다).
  - 매매일지는 ledger_exists_for_holding_today()로 중복 생성을 막는다.
  - 미체결·부분체결·조회 실패는 노션(자동주문 기록 DB)에 이미 남아있는
    주문 기록을 다음 실행이 fetch_latest_sell_order_today()로 다시 읽어
    이어서 확인한다 - 로컬 변수가 아니라 노션이 실행 간 상태를 잇는다.

실 네트워크(KIS·노션·카카오)는 전혀 부르지 않는다 - notion_repo.*와
kis_client의 KIS 호출 지점을 모두 monkeypatch로 대체한다.
"""

from __future__ import annotations

from datetime import date
from unittest import mock

import pytest

import core
import kis_client
import notion_repo

TODAY = date(2026, 9, 14)


def _holding(ticker: str = "005930", name: str = "삼성전자") -> core.HoldingInput:
    return core.HoldingInput(
        ticker=ticker, name=name, market="KOSPI", buy_price=10_000.0, shares=10,
        managed_by=notion_repo.MANAGED_AUTO,
        entry_atr=300.0, units=1,
        checked_date=TODAY, recent_verdict="손절", prev_stop_loss=10_500.0,
    )


def _no_dup_ledger(monkeypatch, exists: bool = False) -> None:
    monkeypatch.setattr(notion_repo, "ledger_exists_for_holding_today",
                        lambda *a, **k: exists)


# ── _check_sell_fill(): 접수 vs 체결 분리 ──────────────────────

def test_zero_fill_does_not_close_or_record_ledger(monkeypatch):
    """접수 성공, 체결 0주 -> 청산 처리·확정 원장 기록 없음."""
    _no_dup_ledger(monkeypatch)
    monkeypatch.setattr(kis_client, "get_order_execution", lambda *a, **k: None)
    close_mock = mock.Mock()
    ledger_mock = mock.Mock()
    monkeypatch.setattr(notion_repo, "close_auto_holding", close_mock)
    monkeypatch.setattr(notion_repo, "create_ledger_record", ledger_mock)

    kis_client._check_sell_fill(
        "token", "page-1", _holding(), order_no="ORD1", requested_qty=10,
        reason="손절 (장중 10,000 ≤ 손절선 10,500)", today=TODAY,
    )

    close_mock.assert_not_called()
    ledger_mock.assert_not_called()


def test_order_lookup_failure_does_not_close_or_record_or_estimate(monkeypatch):
    """체결 조회 실패 -> 임의 청산·추정 손익 기록 없음."""
    _no_dup_ledger(monkeypatch)
    monkeypatch.setattr(kis_client, "get_order_execution",
                        mock.Mock(side_effect=TimeoutError("응답 없음")))
    monkeypatch.setattr(kis_client, "_notify_warning_throttled", lambda k, m: None)
    close_mock = mock.Mock()
    ledger_mock = mock.Mock()
    monkeypatch.setattr(notion_repo, "close_auto_holding", close_mock)
    monkeypatch.setattr(notion_repo, "create_ledger_record", ledger_mock)

    kis_client._check_sell_fill(
        "token", "page-1", _holding(), order_no="ORD1", requested_qty=10,
        reason="손절 (아침 배치 판정)", today=TODAY,
    )

    close_mock.assert_not_called()
    ledger_mock.assert_not_called()


def test_missing_order_no_does_not_close_or_record(monkeypatch):
    """주문번호를 확인할 수 없으면(접수 여부 불명) 아무것도 확정하지 않는다."""
    close_mock = mock.Mock()
    ledger_mock = mock.Mock()
    monkeypatch.setattr(notion_repo, "close_auto_holding", close_mock)
    monkeypatch.setattr(notion_repo, "create_ledger_record", ledger_mock)
    lookup_mock = mock.Mock()
    monkeypatch.setattr(notion_repo, "ledger_exists_for_holding_today", lookup_mock)

    kis_client._check_sell_fill(
        "token", "page-1", _holding(), order_no="", requested_qty=10,
        reason="손절 (아침 배치 판정)", today=TODAY,
    )

    close_mock.assert_not_called()
    ledger_mock.assert_not_called()
    lookup_mock.assert_not_called()


def test_partial_fill_does_not_finalize(monkeypatch):
    """부분체결 - 최종 확정(청산·원장) 없이 다음 실행을 위해 남겨둔다."""
    _no_dup_ledger(monkeypatch)
    monkeypatch.setattr(kis_client, "get_order_execution",
                        lambda *a, **k: {"filled_qty": 4, "avg_price": 9_900.0})
    close_mock = mock.Mock()
    ledger_mock = mock.Mock()
    monkeypatch.setattr(notion_repo, "close_auto_holding", close_mock)
    monkeypatch.setattr(notion_repo, "create_ledger_record", ledger_mock)

    kis_client._check_sell_fill(
        "token", "page-1", _holding(), order_no="ORD1", requested_qty=10,
        reason="손절 (아침 배치 판정)", today=TODAY,
    )

    close_mock.assert_not_called()
    ledger_mock.assert_not_called()


def test_full_fill_unknown_price_does_not_finalize(monkeypatch):
    """전량 체결이 잡혀도 체결가(avg_price)가 0/무효면 확정 기록하지 않는다."""
    _no_dup_ledger(monkeypatch)
    monkeypatch.setattr(kis_client, "get_order_execution",
                        lambda *a, **k: {"filled_qty": 10, "avg_price": 0.0})
    close_mock = mock.Mock()
    ledger_mock = mock.Mock()
    monkeypatch.setattr(notion_repo, "close_auto_holding", close_mock)
    monkeypatch.setattr(notion_repo, "create_ledger_record", ledger_mock)

    kis_client._check_sell_fill(
        "token", "page-1", _holding(), order_no="ORD1", requested_qty=10,
        reason="손절 (아침 배치 판정)", today=TODAY,
    )

    close_mock.assert_not_called()
    ledger_mock.assert_not_called()


def test_full_fill_valid_price_records_correct_qty_and_price(monkeypatch):
    """전량 체결 + 유효 체결가 -> 올바른 수량·가격으로 청산 기록."""
    _no_dup_ledger(monkeypatch)
    monkeypatch.setattr(kis_client, "get_order_execution",
                        lambda *a, **k: {"filled_qty": 10, "avg_price": 9_950.0})
    monkeypatch.setattr(notion_repo, "fetch_holding_buy_date", lambda pid: None)
    monkeypatch.setattr(notion_repo, "fetch_holding_avg_price", lambda pid: 10_000.0)
    close_mock = mock.Mock()
    ledger_mock = mock.Mock(return_value="ledger-page-1")
    monkeypatch.setattr(notion_repo, "close_auto_holding", close_mock)
    monkeypatch.setattr(notion_repo, "create_ledger_record", ledger_mock)

    kis_client._check_sell_fill(
        "token", "page-1", _holding(), order_no="ORD1", requested_qty=10,
        reason="손절 (아침 배치 판정)", today=TODAY,
    )

    ledger_mock.assert_called_once()
    assert ledger_mock.call_args.kwargs["shares"] == 10
    assert ledger_mock.call_args.kwargs["exit_price"] == 9_950.0
    assert ledger_mock.call_args.kwargs["exit_reason"] == notion_repo.LEDGER_EXIT_STOP
    close_mock.assert_called_once_with("page-1")


# ── 부분체결 -> 다음 실행 전량체결: 중복 없이 한 번만 기록 ─────

def test_partial_then_full_fill_across_runs_finalizes_exactly_once(monkeypatch):
    """1회차 부분체결(미확정) -> 2회차 전량체결 확인 시, 새 주문 없이
    노션에 이미 남은 주문 기록(fetch_latest_sell_order_today)을 이어받아
    확정하고, 매매일지는 정확히 1번만 생성된다."""
    inp = _holding()
    ledger_mock = mock.Mock(return_value="ledger-1")
    close_mock = mock.Mock()
    monkeypatch.setattr(notion_repo, "close_auto_holding", close_mock)
    monkeypatch.setattr(notion_repo, "create_ledger_record", ledger_mock)
    monkeypatch.setattr(notion_repo, "fetch_holding_buy_date", lambda pid: None)
    monkeypatch.setattr(notion_repo, "fetch_holding_avg_price", lambda pid: 10_000.0)

    # 1회차: 방금 낸 주문의 낙관적 확인 - 부분체결(4/10)이라 확정 안 됨.
    ledger_exists = mock.Mock(return_value=False)
    monkeypatch.setattr(notion_repo, "ledger_exists_for_holding_today", ledger_exists)
    monkeypatch.setattr(kis_client, "get_order_execution",
                        lambda *a, **k: {"filled_qty": 4, "avg_price": 9_900.0})
    kis_client._check_sell_fill(
        "token", "page-1", inp, order_no="ORD1", requested_qty=10,
        reason="손절 (아침 배치 판정)", today=TODAY,
    )
    ledger_mock.assert_not_called()
    close_mock.assert_not_called()

    # 2회차: 새 프로세스(컨테이너)라고 가정 - _finalize_pending_sell이
    # 매도가능수량 0인 이 종목의 주문 기록을 노션에서 다시 읽어와 이어서
    # 확인한다. 이번엔 전량 체결.
    fetch_order_mock = mock.Mock(
        return_value={"order_no": "ORD1", "qty": 10.0, "reason": "손절 (아침 배치 판정)"}
    )
    monkeypatch.setattr(notion_repo, "fetch_latest_sell_order_today", fetch_order_mock)
    monkeypatch.setattr(kis_client, "get_order_execution",
                        lambda *a, **k: {"filled_qty": 10, "avg_price": 9_950.0})

    kis_client._finalize_pending_sell("token", "page-1", inp, TODAY)

    fetch_order_mock.assert_called_once_with(inp.ticker, TODAY)
    ledger_mock.assert_called_once()
    assert ledger_mock.call_args.kwargs["shares"] == 10
    close_mock.assert_called_once_with("page-1")


def test_finalize_pending_sell_skips_when_no_order_record_found(monkeypatch):
    """매도가능수량이 0인데 이 시스템이 낸 매도 기록이 없으면(수동 매도 등
    추정) 임의로 청산 처리하지 않는다."""
    monkeypatch.setattr(notion_repo, "fetch_latest_sell_order_today",
                        lambda *a, **k: None)
    close_mock = mock.Mock()
    ledger_mock = mock.Mock()
    monkeypatch.setattr(notion_repo, "close_auto_holding", close_mock)
    monkeypatch.setattr(notion_repo, "create_ledger_record", ledger_mock)

    kis_client._finalize_pending_sell("token", "page-1", _holding(), TODAY)

    close_mock.assert_not_called()
    ledger_mock.assert_not_called()


def test_finalize_pending_sell_lookup_failure_leaves_holding_untouched(monkeypatch):
    """오늘 주문 기록 조회 자체가 실패해도(접수 여부 불명) 청산 처리하지
    않는다 - 다음 실행에서 다시 시도한다."""
    monkeypatch.setattr(notion_repo, "fetch_latest_sell_order_today",
                        mock.Mock(side_effect=RuntimeError("노션 조회 실패")))
    close_mock = mock.Mock()
    monkeypatch.setattr(notion_repo, "close_auto_holding", close_mock)

    kis_client._finalize_pending_sell("token", "page-1", _holding(), TODAY)

    close_mock.assert_not_called()


# ── 청산 처리·원장 기록 중 한쪽만 실패 -> 다음 실행 복구, 중복 없음 ──

def test_ledger_recorded_but_close_failed_recovers_close_only_next_run(monkeypatch):
    """1회차: 매매일지는 기록됐지만 점검표 청산 처리가 실패. 2회차: 매매일지가
    이미 있으므로 다시 만들지 않고 청산 처리만 재시도한다."""
    inp = _holding()
    ledger_mock = mock.Mock(return_value="ledger-1")
    monkeypatch.setattr(notion_repo, "create_ledger_record", ledger_mock)
    monkeypatch.setattr(notion_repo, "fetch_holding_buy_date", lambda pid: None)
    monkeypatch.setattr(notion_repo, "fetch_holding_avg_price", lambda pid: 10_000.0)
    monkeypatch.setattr(notion_repo, "ledger_exists_for_holding_today",
                        lambda *a, **k: False)
    monkeypatch.setattr(kis_client, "get_order_execution",
                        lambda *a, **k: {"filled_qty": 10, "avg_price": 9_950.0})

    close_mock_1 = mock.Mock(side_effect=RuntimeError("노션 API 오류"))
    monkeypatch.setattr(notion_repo, "close_auto_holding", close_mock_1)
    monkeypatch.setattr(kis_client, "_notify_failure", lambda msg: None)

    kis_client._check_sell_fill(
        "token", "page-1", inp, order_no="ORD1", requested_qty=10,
        reason="손절 (아침 배치 판정)", today=TODAY,
    )
    ledger_mock.assert_called_once()
    close_mock_1.assert_called_once()

    # 2회차 - 매매일지가 이미 있다(ledger_exists_for_holding_today=True).
    monkeypatch.setattr(notion_repo, "ledger_exists_for_holding_today",
                        lambda *a, **k: True)
    close_mock_2 = mock.Mock()
    monkeypatch.setattr(notion_repo, "close_auto_holding", close_mock_2)

    kis_client._check_sell_fill(
        "token", "page-1", inp, order_no="ORD1", requested_qty=10,
        reason="손절 (아침 배치 판정)", today=TODAY,
    )

    assert ledger_mock.call_count == 1, "매매일지가 중복 생성되면 안 된다"
    close_mock_2.assert_called_once_with("page-1")


def test_ledger_write_failure_blocks_close_this_run(monkeypatch):
    """매매일지 기록이 실패하면 점검표 청산 처리를 시도하지 않는다 - 반대
    순서(청산 먼저)였다면 다음 실행이 이 행을 구분='보유'로 더는 못 찾아
    매매일지 공백이 영영 안 채워진다."""
    _no_dup_ledger(monkeypatch)
    monkeypatch.setattr(kis_client, "get_order_execution",
                        lambda *a, **k: {"filled_qty": 10, "avg_price": 9_950.0})
    monkeypatch.setattr(notion_repo, "fetch_holding_buy_date", lambda pid: None)
    monkeypatch.setattr(notion_repo, "fetch_holding_avg_price", lambda pid: 10_000.0)
    monkeypatch.setattr(notion_repo, "create_ledger_record",
                        mock.Mock(side_effect=RuntimeError("노션 API 오류")))
    monkeypatch.setattr(kis_client, "_notify_warning_throttled", lambda k, m: None)
    close_mock = mock.Mock()
    monkeypatch.setattr(notion_repo, "close_auto_holding", close_mock)

    kis_client._check_sell_fill(
        "token", "page-1", _holding(), order_no="ORD1", requested_qty=10,
        reason="손절 (아침 배치 판정)", today=TODAY,
    )

    close_mock.assert_not_called()


# ── run_auto_sell() 통합: 미체결 종목은 새 주문 없이 계속 추적된다 ──

def _stub_gates(monkeypatch) -> None:
    monkeypatch.setattr(kis_client, "_auto_trade_configured", lambda: True)
    monkeypatch.setattr(kis_client, "_within_trading_hours", lambda *a, **k: True)
    monkeypatch.setattr(kis_client, "_is_trading_day", lambda *a, **k: True)
    monkeypatch.setattr(kis_client, "get_access_token", lambda: "token")
    monkeypatch.setattr(kis_client, "_today", lambda: TODAY)


def test_run_auto_sell_pending_ticker_does_not_resend_order(monkeypatch):
    """매도가능수량이 0(직전 매도 미확인)인 종목은 새 매도 주문을 내지
    않고 체결 확인만 한다 - 중복 주문 방지."""
    _stub_gates(monkeypatch)
    inp = _holding()
    monkeypatch.setattr(
        kis_client, "get_account_balance",
        lambda token: {"account_size": 1.0, "available_cash": 1.0,
                       "holdings": [{"ticker": inp.ticker, "qty": 10, "sellable": 0}]},
    )
    place_mock = mock.Mock()
    monkeypatch.setattr(kis_client, "place_market_sell_order", place_mock)
    finalize_mock = mock.Mock()
    monkeypatch.setattr(kis_client, "_finalize_pending_sell", finalize_mock)

    kis_client.run_auto_sell(holdings=[("page-1", inp)])

    place_mock.assert_not_called()
    finalize_mock.assert_called_once()
    assert finalize_mock.call_args.args[1] == "page-1"


def test_run_auto_sell_full_fill_confirmed_inline(monkeypatch):
    """매도 가능한 종목이 조건을 충족해 주문을 내고, 같은 회차에 전량
    체결까지 확인되면 청산·원장 기록이 이뤄진다."""
    _stub_gates(monkeypatch)
    inp = _holding()
    monkeypatch.setattr(
        kis_client, "get_account_balance",
        lambda token: {"account_size": 1.0, "available_cash": 1.0,
                       "holdings": [{"ticker": inp.ticker, "qty": 10, "sellable": 10}]},
    )
    monkeypatch.setattr(kis_client, "get_current_price", lambda token, ticker: "10000")
    monkeypatch.setattr(
        kis_client, "place_market_sell_order",
        lambda *a, **k: {"status": "sent", "order_no": "ORD1"},
    )
    monkeypatch.setattr(kis_client, "time",
                        mock.Mock(sleep=lambda s: None))
    monkeypatch.setattr(kis_client, "get_order_execution",
                        lambda *a, **k: {"filled_qty": 10, "avg_price": 9_950.0})
    monkeypatch.setattr(notion_repo, "ledger_exists_for_holding_today",
                        lambda *a, **k: False)
    monkeypatch.setattr(notion_repo, "fetch_holding_buy_date", lambda pid: None)
    monkeypatch.setattr(notion_repo, "fetch_holding_avg_price", lambda pid: 10_000.0)
    ledger_mock = mock.Mock(return_value="ledger-1")
    close_mock = mock.Mock()
    monkeypatch.setattr(notion_repo, "create_ledger_record", ledger_mock)
    monkeypatch.setattr(notion_repo, "close_auto_holding", close_mock)

    kis_client.run_auto_sell(holdings=[("page-1", inp)])

    ledger_mock.assert_called_once()
    assert ledger_mock.call_args.kwargs["shares"] == 10
    close_mock.assert_called_once_with("page-1")


def test_run_auto_sell_zero_fill_leaves_holding_open_no_dup_next_call(monkeypatch):
    """접수 성공, 체결 0주 -> 이번 회차엔 청산도 원장 기록도 없고, 매도
    가능수량이 여전히 0인 다음 회차도 새 주문을 내지 않는다(중복 방지)."""
    _stub_gates(monkeypatch)
    inp = _holding()
    monkeypatch.setattr(kis_client, "get_current_price", lambda token, ticker: "10000")
    monkeypatch.setattr(
        kis_client, "place_market_sell_order",
        lambda *a, **k: {"status": "sent", "order_no": "ORD1"},
    )
    monkeypatch.setattr(kis_client, "time", mock.Mock(sleep=lambda s: None))
    monkeypatch.setattr(notion_repo, "ledger_exists_for_holding_today",
                        lambda *a, **k: False)
    ledger_mock = mock.Mock()
    close_mock = mock.Mock()
    monkeypatch.setattr(notion_repo, "create_ledger_record", ledger_mock)
    monkeypatch.setattr(notion_repo, "close_auto_holding", close_mock)

    # 1회차: 매도가능수량 10 -> 매도 주문을 낸다. 체결 확인은 0주.
    monkeypatch.setattr(
        kis_client, "get_account_balance",
        lambda token: {"account_size": 1.0, "available_cash": 1.0,
                       "holdings": [{"ticker": inp.ticker, "qty": 10, "sellable": 10}]},
    )
    monkeypatch.setattr(kis_client, "get_order_execution", lambda *a, **k: None)
    place_mock = mock.Mock(return_value={"status": "sent", "order_no": "ORD1"})
    monkeypatch.setattr(kis_client, "place_market_sell_order", place_mock)

    kis_client.run_auto_sell(holdings=[("page-1", inp)])

    ledger_mock.assert_not_called()
    close_mock.assert_not_called()
    assert place_mock.call_count == 1

    # 2회차: 아직 미체결이라 매도가능수량은 여전히 0 - 새 주문을 내면 안 된다.
    monkeypatch.setattr(
        kis_client, "get_account_balance",
        lambda token: {"account_size": 1.0, "available_cash": 1.0,
                       "holdings": [{"ticker": inp.ticker, "qty": 10, "sellable": 0}]},
    )
    monkeypatch.setattr(notion_repo, "fetch_latest_sell_order_today",
                        lambda *a, **k: {"order_no": "ORD1", "qty": 10.0,
                                         "reason": "손절 (아침 배치 판정)"})

    kis_client.run_auto_sell(holdings=[("page-1", inp)])

    assert place_mock.call_count == 1, "미체결 상태에서는 같은 종목을 다시 주문하면 안 된다"
    ledger_mock.assert_not_called()
    close_mock.assert_not_called()


# ── 기존 신규매수·추가매수 경로 무변경 확인 ────────────────────

def test_new_buy_order_flow_unaffected(monkeypatch):
    """place_market_buy_order()는 이번 수정 대상이 아니다 - 접수(sent)
    즉시 반환하는 기존 동작 그대로여야 한다."""
    monkeypatch.setattr(kis_client, "_check_order_allowed", lambda *a, **k: None)
    monkeypatch.setattr(kis_client, "_create_pending_record", lambda **k: "page-1")
    monkeypatch.setattr(
        kis_client, "_send_market_buy",
        lambda *a, **k: {"rt_cd": "0", "output": {"ODNO": "ORD9"}},
    )
    monkeypatch.setattr(kis_client, "_update_order_record", mock.Mock())

    result = kis_client.place_market_buy_order("token", "005930", 10, name="삼성전자")

    assert result == {"status": "sent", "order_no": "ORD9"}


def test_pyramid_add_path_does_not_use_sell_fill_helpers(monkeypatch):
    """추가매수(run_auto_pyramid) 경로는 place_market_buy_order를 그대로
    재사용할 뿐, 이번에 추가한 매도 체결 확인 헬퍼를 전혀 부르지 않는다."""
    check_fill_mock = mock.Mock()
    finalize_mock = mock.Mock()
    monkeypatch.setattr(kis_client, "_check_sell_fill", check_fill_mock)
    monkeypatch.setattr(kis_client, "_finalize_pending_sell", finalize_mock)
    monkeypatch.setattr(kis_client, "_auto_trade_configured", lambda: True)
    monkeypatch.setattr(kis_client, "_auto_trade_paused", lambda: False)
    monkeypatch.setattr(kis_client, "_within_trading_hours", lambda *a, **k: True)
    monkeypatch.setattr(kis_client, "_is_trading_day", lambda *a, **k: True)

    kis_client.run_auto_pyramid(holdings=[])

    check_fill_mock.assert_not_called()
    finalize_mock.assert_not_called()
