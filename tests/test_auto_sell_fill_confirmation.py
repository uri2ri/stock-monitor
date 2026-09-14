"""자동매도 체결 확인 회귀 테스트 (접수/체결 분리 + 날짜를 넘긴 복구).

1차 수정(_check_sell_fill/_write_ledger_and_close)에서 "접수"(rt_cd=0)와
"전량 체결"을 분리했다. 이 파일은 그 위에서 추가 리뷰로 드러난 문제들을
검증한다:

  - 날짜가 바뀌면 복구가 끊김: fetch_latest_sell_order_today()/
    ledger_exists_for_holding_today()/get_order_execution()이 모두 "오늘"
    에만 한정돼 있었다 - 전날 낸 주문은 다음 거래일에 다시 조회가 안 됐다.
    -> fetch_pending_sell_orders(since=매수일)/ledger_exists_for_holding
    (날짜 무관)/get_order_execution(order_date=주문일~오늘)로 교체.
    매매일지의 청산일도 재처리 날짜가 아니라 실제 주문일을 쓴다.
  - 미확정 주문 추적: 매도가능수량이 0일 때만 이전 주문을 확인하던 걸,
    수량과 무관하게 항상 먼저 확인하도록 바꿨다(_reconcile_pending_sell_orders).
    "주문중"/"실패"(접수 여부 불명)도 "주문 없음"으로 취급하지 않고
    신규 매도를 보류한다. 여러 건이 있으면 전부 확인한다(최신 1건만 X).
  - 숫자 유효성: 체결가는 math.isfinite(price) and price>0, 수량은 유효한
    양의 정수만 인정한다(_is_valid_qty) - 수량 누락(None)을 0으로 대체해
    "0 >= None 요청량"처럼 잘못 전량체결로 인정하지 않는다.
  - 개별 주문의 전량체결 == 포지션 전체 청산이 아니다 - 증권사 잔고에
    잔여 보유가 남아 있으면 그 주문 하나만 소화하고 포지션은 닫지 않는다.

실 네트워크(KIS·노션·카카오)는 전혀 부르지 않는다 - notion_repo.*와
kis_client의 KIS 호출 지점을 모두 monkeypatch로 대체한다. 핵심 조회
함수(fetch_pending_sell_orders/ledger_exists_for_holding/get_order_execution)
까지 실제로 대체해 날짜·필터 문제가 다른 mock에 가려지지 않게 한다.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest import mock

import core
import kis_client
import notion_repo

TODAY = date(2026, 9, 15)      # 다음 거래일(오늘 회차)
YESTERDAY = date(2026, 9, 14)  # 전날(주문이 실제로 나간 날)


def _holding(ticker: str = "005930", name: str = "삼성전자") -> core.HoldingInput:
    return core.HoldingInput(
        ticker=ticker, name=name, market="KOSPI", buy_price=10_000.0, shares=10,
        managed_by=notion_repo.MANAGED_AUTO,
        entry_atr=300.0, units=1,
        checked_date=TODAY, recent_verdict="손절", prev_stop_loss=10_500.0,
    )


def _pending_order(*, order_no="ORD1", qty=10.0, reason="손절 (아침 배치 판정)",
                    status="성공", order_date=YESTERDAY) -> dict:
    return {"order_no": order_no, "qty": qty, "reason": reason,
            "status": status, "order_date": order_date}


# ── _is_valid_qty(): 수량 유효성 ────────────────────────────────

def test_is_valid_qty_rejects_none_nan_inf_negative_fraction():
    assert kis_client._is_valid_qty(None) is False
    assert kis_client._is_valid_qty(float("nan")) is False
    assert kis_client._is_valid_qty(float("inf")) is False
    assert kis_client._is_valid_qty(-1) is False
    assert kis_client._is_valid_qty(1.5) is False


def test_is_valid_qty_zero_only_valid_with_allow_zero():
    assert kis_client._is_valid_qty(0) is False
    assert kis_client._is_valid_qty(0, allow_zero=True) is True
    assert kis_client._is_valid_qty(10) is True
    assert kis_client._is_valid_qty(10.0, allow_zero=True) is True


# ── _check_sell_fill(): 체결 상태 확인(노션 쓰기 없음) ───────────

def test_missing_requested_qty_is_waiting_and_never_queries_kis():
    """요청 수량 정보가 없으면(None) 0으로 대체해 전량체결로 오인하지 않고
    KIS 조회 자체를 하지 않는다 - 판단 근거가 없다."""
    execution_mock = mock.Mock()
    with mock.patch.object(kis_client, "get_order_execution", execution_mock), \
         mock.patch.object(notion_repo, "ledger_exists_for_holding",
                            mock.Mock(return_value=False)):
        outcome, filled_qty, avg_price = kis_client._check_sell_fill(
            "token", "page-1", order_no="ORD1", order_date=YESTERDAY,
            today=TODAY, requested_qty=None,
        )
    assert outcome == "waiting"
    execution_mock.assert_not_called()


def test_ledger_exists_short_circuits_before_order_no_check():
    """매매일지가 이미 있으면(중복 방지) 주문번호가 없어도 곧바로 확정
    경로(already_recorded)로 - order_no 유효성보다 이 확인이 먼저다."""
    with mock.patch.object(notion_repo, "ledger_exists_for_holding",
                            mock.Mock(return_value=True)):
        outcome, _, _ = kis_client._check_sell_fill(
            "token", "page-1", order_no="", order_date=YESTERDAY,
            today=TODAY, requested_qty=10,
        )
    assert outcome == "already_recorded"


def test_missing_order_no_is_waiting_when_not_already_recorded():
    with mock.patch.object(notion_repo, "ledger_exists_for_holding",
                            mock.Mock(return_value=False)):
        outcome, _, _ = kis_client._check_sell_fill(
            "token", "page-1", order_no="", order_date=YESTERDAY,
            today=TODAY, requested_qty=10,
        )
    assert outcome == "waiting"


def test_execution_lookup_not_found_is_waiting_not_expired():
    """조회 기간 안에서 주문번호 자체를 못 찾으면(None) "미체결로 만료"가
    아니라 "확인 불가"로 대기한다 - 조회 문제로 놓친 걸 만료로 단정하면
    이미 체결된 걸 못 보고 새로 매도해버릴 위험이 있다."""
    with mock.patch.object(notion_repo, "ledger_exists_for_holding",
                            mock.Mock(return_value=False)), \
         mock.patch.object(kis_client, "get_order_execution",
                            mock.Mock(return_value=None)):
        outcome, filled_qty, _ = kis_client._check_sell_fill(
            "token", "page-1", order_no="ORD1", order_date=YESTERDAY,
            today=TODAY, requested_qty=10,
        )
    assert outcome == "waiting"
    assert filled_qty == 0


def test_execution_lookup_exception_is_waiting():
    with mock.patch.object(notion_repo, "ledger_exists_for_holding",
                            mock.Mock(return_value=False)), \
         mock.patch.object(kis_client, "get_order_execution",
                            mock.Mock(side_effect=TimeoutError("응답 없음"))), \
         mock.patch.object(kis_client, "_notify_warning_throttled", lambda k, m: None):
        outcome, _, _ = kis_client._check_sell_fill(
            "token", "page-1", order_no="ORD1", order_date=YESTERDAY,
            today=TODAY, requested_qty=10,
        )
    assert outcome == "waiting"


def test_same_day_partial_fill_waits_not_expired():
    """당일 주문이 부분체결이면 아직 장중일 수 있어 만료로 단정하지 않는다."""
    with mock.patch.object(notion_repo, "ledger_exists_for_holding",
                            mock.Mock(return_value=False)), \
         mock.patch.object(kis_client, "get_order_execution",
                            mock.Mock(return_value={"filled_qty": 4, "avg_price": 9_900.0})):
        outcome, filled_qty, _ = kis_client._check_sell_fill(
            "token", "page-1", order_no="ORD1", order_date=TODAY,
            today=TODAY, requested_qty=10,
        )
    assert outcome == "waiting"
    assert filled_qty == 4


def test_previous_day_zero_fill_is_confirmed_expired_none():
    """전날 낸 주문이 전량 미체결이면(당일가 주문 - 장마감에 자동 취소)
    다음 거래일엔 더 기다릴 필요 없이 만료로 확정한다."""
    with mock.patch.object(notion_repo, "ledger_exists_for_holding",
                            mock.Mock(return_value=False)), \
         mock.patch.object(kis_client, "get_order_execution",
                            mock.Mock(return_value={"filled_qty": 0, "avg_price": 0.0})):
        outcome, filled_qty, _ = kis_client._check_sell_fill(
            "token", "page-1", order_no="ORD1", order_date=YESTERDAY,
            today=TODAY, requested_qty=10,
        )
    assert outcome == "confirmed_expired_none"
    assert filled_qty == 0


def test_previous_day_partial_fill_is_confirmed_expired_partial():
    with mock.patch.object(notion_repo, "ledger_exists_for_holding",
                            mock.Mock(return_value=False)), \
         mock.patch.object(kis_client, "get_order_execution",
                            mock.Mock(return_value={"filled_qty": 4, "avg_price": 9_900.0})):
        outcome, filled_qty, _ = kis_client._check_sell_fill(
            "token", "page-1", order_no="ORD1", order_date=YESTERDAY,
            today=TODAY, requested_qty=10,
        )
    assert outcome == "confirmed_expired_partial"
    assert filled_qty == 4


def test_invalid_filled_qty_is_waiting():
    with mock.patch.object(notion_repo, "ledger_exists_for_holding",
                            mock.Mock(return_value=False)), \
         mock.patch.object(kis_client, "get_order_execution",
                            mock.Mock(return_value={"filled_qty": float("nan"),
                                                     "avg_price": 9_900.0})):
        outcome, _, _ = kis_client._check_sell_fill(
            "token", "page-1", order_no="ORD1", order_date=YESTERDAY,
            today=TODAY, requested_qty=10,
        )
    assert outcome == "waiting"


def test_full_fill_is_confirmed_full_regardless_of_day():
    with mock.patch.object(notion_repo, "ledger_exists_for_holding",
                            mock.Mock(return_value=False)), \
         mock.patch.object(kis_client, "get_order_execution",
                            mock.Mock(return_value={"filled_qty": 10, "avg_price": 9_950.0})):
        outcome, filled_qty, avg_price = kis_client._check_sell_fill(
            "token", "page-1", order_no="ORD1", order_date=YESTERDAY,
            today=TODAY, requested_qty=10,
        )
    assert outcome == "confirmed_full"
    assert filled_qty == 10
    assert avg_price == 9_950.0


# ── _apply_sell_fill_outcome(): 확정 처리 ────────────────────────

def test_apply_already_recorded_only_retries_close():
    ledger_mock = mock.Mock()
    close_mock = mock.Mock()
    with mock.patch.object(notion_repo, "create_ledger_record", ledger_mock), \
         mock.patch.object(notion_repo, "close_auto_holding", close_mock):
        result = kis_client._apply_sell_fill_outcome(
            "page-1", _holding(), "already_recorded", 0, None,
            reason="손절", order_no="ORD1", exit_date=YESTERDAY, sellable_now=0,
        )
    assert result == "closed"
    ledger_mock.assert_not_called()
    close_mock.assert_called_once_with("page-1")


def test_apply_waiting_writes_nothing():
    ledger_mock = mock.Mock()
    close_mock = mock.Mock()
    with mock.patch.object(notion_repo, "create_ledger_record", ledger_mock), \
         mock.patch.object(notion_repo, "close_auto_holding", close_mock):
        result = kis_client._apply_sell_fill_outcome(
            "page-1", _holding(), "waiting", 4, 9_900.0,
            reason="손절", order_no="ORD1", exit_date=YESTERDAY, sellable_now=6,
        )
    assert result == "waiting"
    ledger_mock.assert_not_called()
    close_mock.assert_not_called()


def test_apply_confirmed_expired_none_releases_without_writes():
    ledger_mock = mock.Mock()
    close_mock = mock.Mock()
    with mock.patch.object(notion_repo, "create_ledger_record", ledger_mock), \
         mock.patch.object(notion_repo, "close_auto_holding", close_mock), \
         mock.patch.object(kis_client, "_notify_warning_throttled", lambda k, m: None):
        result = kis_client._apply_sell_fill_outcome(
            "page-1", _holding(), "confirmed_expired_none", 0, None,
            reason="손절", order_no="ORD1", exit_date=YESTERDAY, sellable_now=10,
        )
    assert result == "released"
    ledger_mock.assert_not_called()
    close_mock.assert_not_called()


def test_apply_confirmed_expired_partial_releases_and_warns_without_ledger():
    """부분체결 후 만료 - 확인된 체결분이 있어도 전량청산 모델이라 매매일지엔
    반영하지 않는다(알려진 한계). 잔여 보유 판단으로는 넘어간다(released)."""
    ledger_mock = mock.Mock()
    close_mock = mock.Mock()
    warn_mock = mock.Mock()
    with mock.patch.object(notion_repo, "create_ledger_record", ledger_mock), \
         mock.patch.object(notion_repo, "close_auto_holding", close_mock), \
         mock.patch.object(kis_client, "_notify_warning_throttled", warn_mock):
        result = kis_client._apply_sell_fill_outcome(
            "page-1", _holding(), "confirmed_expired_partial", 4, 9_900.0,
            reason="손절", order_no="ORD1", exit_date=YESTERDAY, sellable_now=6,
        )
    assert result == "released"
    ledger_mock.assert_not_called()
    close_mock.assert_not_called()
    warn_mock.assert_called_once()


def test_apply_confirmed_full_with_remaining_sellable_does_not_close_position():
    """개별 주문의 전량체결을 곧바로 포지션 전체 청산으로 보지 않는다 -
    증권사 잔고에 잔여 보유가 남아 있으면 이 주문만 소화하고 열어 둔다."""
    ledger_mock = mock.Mock()
    close_mock = mock.Mock()
    with mock.patch.object(notion_repo, "create_ledger_record", ledger_mock), \
         mock.patch.object(notion_repo, "close_auto_holding", close_mock):
        result = kis_client._apply_sell_fill_outcome(
            "page-1", _holding(), "confirmed_full", 10, 9_950.0,
            reason="손절", order_no="ORD1", exit_date=YESTERDAY, sellable_now=5,
        )
    assert result == "released"
    ledger_mock.assert_not_called()
    close_mock.assert_not_called()


def test_apply_confirmed_full_invalid_price_waits():
    for bad_price in (0.0, -1.0, float("nan"), float("inf"), None):
        ledger_mock = mock.Mock()
        close_mock = mock.Mock()
        with mock.patch.object(notion_repo, "create_ledger_record", ledger_mock), \
             mock.patch.object(notion_repo, "close_auto_holding", close_mock):
            result = kis_client._apply_sell_fill_outcome(
                "page-1", _holding(), "confirmed_full", 10, bad_price,
                reason="손절", order_no="ORD1", exit_date=YESTERDAY, sellable_now=None,
            )
        assert result == "waiting", f"가격 {bad_price!r}은 무효 처리돼야 한다"
        ledger_mock.assert_not_called()
        close_mock.assert_not_called()


def test_apply_confirmed_full_valid_price_writes_ledger_with_actual_order_date():
    """청산일은 재처리 날짜(exit_date로 넘긴 실제 주문일)를 쓴다 - "오늘"이
    아니다."""
    inp = _holding()
    ledger_mock = mock.Mock(return_value="ledger-1")
    close_mock = mock.Mock()
    with mock.patch.object(notion_repo, "create_ledger_record", ledger_mock), \
         mock.patch.object(notion_repo, "close_auto_holding", close_mock), \
         mock.patch.object(notion_repo, "fetch_holding_buy_date", lambda pid: None), \
         mock.patch.object(notion_repo, "fetch_holding_avg_price", lambda pid: 10_000.0):
        result = kis_client._apply_sell_fill_outcome(
            "page-1", inp, "confirmed_full", 10, 9_950.0,
            reason="손절 (아침 배치 판정)", order_no="ORD1",
            exit_date=YESTERDAY, sellable_now=0,
        )
    assert result == "closed"
    ledger_mock.assert_called_once()
    assert ledger_mock.call_args.kwargs["exit_date"] == YESTERDAY
    assert ledger_mock.call_args.kwargs["shares"] == 10
    assert ledger_mock.call_args.kwargs["exit_price"] == 9_950.0
    close_mock.assert_called_once_with("page-1")


# ── _reconcile_pending_sell_orders(): 날짜를 넘긴 복구 통합 ──────

def test_recovery_previous_day_full_fill_after_ledger_failure_closes_with_order_date():
    """전날 전량 체결됐지만 원장 기록이 실패해 미확정으로 남은 주문을,
    다음 거래일에 sellable=0으로 복구한다. 청산일은 실제 주문일(전날)."""
    inp = _holding()
    with mock.patch.object(notion_repo, "fetch_holding_buy_date",
                            mock.Mock(return_value=date(2026, 9, 1))), \
         mock.patch.object(notion_repo, "fetch_pending_sell_orders",
                            mock.Mock(return_value=[_pending_order()])) as fetch_mock, \
         mock.patch.object(notion_repo, "ledger_exists_for_holding",
                            mock.Mock(return_value=False)), \
         mock.patch.object(kis_client, "get_order_execution",
                            mock.Mock(return_value={"filled_qty": 10, "avg_price": 9_950.0})), \
         mock.patch.object(notion_repo, "fetch_holding_avg_price", lambda pid: 10_000.0), \
         mock.patch.object(notion_repo, "create_ledger_record",
                            mock.Mock(return_value="ledger-1")) as ledger_mock, \
         mock.patch.object(notion_repo, "close_auto_holding", mock.Mock()) as close_mock:
        result = kis_client._reconcile_pending_sell_orders("token", "page-1", inp, TODAY, 0)

    assert result == "closed"
    fetch_mock.assert_called_once_with(inp.ticker, kis_client.ACCOUNT_TYPE,
                                        since=date(2026, 9, 1))
    ledger_mock.assert_called_once()
    assert ledger_mock.call_args.kwargs["exit_date"] == YESTERDAY, (
        "재처리 회차(TODAY)가 아니라 실제 주문일(YESTERDAY)이 청산일이어야 한다"
    )
    close_mock.assert_called_once_with("page-1")


def test_recovery_previous_day_ledger_success_close_failure_recovers_without_duplicate():
    """전날 원장 기록은 성공했지만 점검표 청산 처리가 실패한 경우, 다음
    거래일엔 매매일지를 다시 만들지 않고 청산 처리만 재시도한다."""
    inp = _holding()
    with mock.patch.object(notion_repo, "fetch_holding_buy_date",
                            mock.Mock(return_value=date(2026, 9, 1))), \
         mock.patch.object(notion_repo, "fetch_pending_sell_orders",
                            mock.Mock(return_value=[_pending_order()])), \
         mock.patch.object(notion_repo, "ledger_exists_for_holding",
                            mock.Mock(return_value=True)) as ledger_exists_mock, \
         mock.patch.object(kis_client, "get_order_execution", mock.Mock()) as execution_mock, \
         mock.patch.object(notion_repo, "create_ledger_record", mock.Mock()) as ledger_mock, \
         mock.patch.object(notion_repo, "close_auto_holding", mock.Mock()) as close_mock:
        result = kis_client._reconcile_pending_sell_orders("token", "page-1", inp, TODAY, 0)

    assert result == "closed"
    ledger_exists_mock.assert_called_once_with("page-1")
    ledger_mock.assert_not_called()
    execution_mock.assert_not_called(), "매매일지가 이미 있으면 체결 재조회조차 필요 없다"
    close_mock.assert_called_once_with("page-1")


def test_recovery_ambiguous_status_blocks_new_sell_without_treating_as_no_order():
    """접수 후 상태 기록 실패("실패") 또는 응답 시간초과로 남은 주문은
    "주문 없음"으로 취급하지 않고, 확인 전까지 신규 매도를 보류한다."""
    inp = _holding()
    for status in ("실패", "주문중"):
        with mock.patch.object(notion_repo, "fetch_holding_buy_date",
                                mock.Mock(return_value=date(2026, 9, 1))), \
             mock.patch.object(
                 notion_repo, "fetch_pending_sell_orders",
                 mock.Mock(return_value=[_pending_order(status=status, order_no="")]),
             ), \
             mock.patch.object(kis_client, "get_order_execution", mock.Mock()) as execution_mock, \
             mock.patch.object(notion_repo, "close_auto_holding", mock.Mock()) as close_mock, \
             mock.patch.object(kis_client, "_notify_warning_throttled", mock.Mock()) as warn_mock:
            result = kis_client._reconcile_pending_sell_orders(
                "token", "page-1", inp, TODAY, 6,
            )
        assert result == "blocked", f"상태={status}는 확인 전까지 보류해야 한다"
        execution_mock.assert_not_called()
        close_mock.assert_not_called()
        warn_mock.assert_called_once()


def test_recovery_pending_exists_but_sellable_positive_still_checks_pending_first():
    """매도가능수량이 양수여도(0이 아니어도) 미확정 주문이 있으면 먼저
    확인한다 - 예전엔 sellable<=0일 때만 확인했다."""
    inp = _holding()
    with mock.patch.object(notion_repo, "fetch_holding_buy_date",
                            mock.Mock(return_value=date(2026, 9, 1))), \
         mock.patch.object(
             notion_repo, "fetch_pending_sell_orders",
             mock.Mock(return_value=[_pending_order(qty=10.0)]),
         ) as fetch_mock, \
         mock.patch.object(notion_repo, "ledger_exists_for_holding",
                            mock.Mock(return_value=False)), \
         mock.patch.object(kis_client, "get_order_execution",
                            mock.Mock(return_value={"filled_qty": 4, "avg_price": 9_900.0})), \
         mock.patch.object(kis_client, "_notify_warning_throttled", lambda k, m: None):
        result = kis_client._reconcile_pending_sell_orders(
            "token", "page-1", inp, TODAY, 6,  # sellable=6 (양수)
        )

    fetch_mock.assert_called_once()
    # 전날 주문이 부분체결(4/10) 후 만료 -> released, 남은 판단은 이어진다.
    assert result == "clear"


def test_recovery_multiple_orders_for_same_position_all_checked_not_just_latest():
    """같은 포지션에 매도 주문이 여러 건 남아 있으면 전부 확인한다(최신
    1건만 보고 나머지를 버리지 않는다)."""
    # ORD_A: 전날 전체(10주) 요청했으나 부분체결(6주)만 되고 잔량은 만료.
    # ORD_B: 그 뒤(같은 전날) 남은 4주에 대해 새로 낸 주문 - 이번엔 전량체결.
    # 매도는 항상 전량이라 이 둘이 이 포지션의 요청량을 나눠 갖는 게 아니라,
    # 각자 "그 시점 전체 잔여"를 요청한 개별 시도다.
    inp = _holding()
    order_a = _pending_order(order_no="ORD_A", qty=10.0, order_date=YESTERDAY)
    order_b = _pending_order(order_no="ORD_B", qty=4.0, order_date=YESTERDAY)

    def _fake_execution(token, order_no, order_date=None):
        if order_no == "ORD_A":
            return {"filled_qty": 6, "avg_price": 9_900.0}
        if order_no == "ORD_B":
            return {"filled_qty": 4, "avg_price": 9_950.0}
        raise AssertionError(f"예상치 못한 주문번호: {order_no}")

    seen_order_nos: list[str] = []

    def _tracking_execution(token, order_no, order_date=None):
        seen_order_nos.append(order_no)
        return _fake_execution(token, order_no, order_date)

    with mock.patch.object(notion_repo, "fetch_holding_buy_date",
                            mock.Mock(return_value=date(2026, 9, 1))), \
         mock.patch.object(notion_repo, "fetch_pending_sell_orders",
                            mock.Mock(return_value=[order_a, order_b])), \
         mock.patch.object(notion_repo, "ledger_exists_for_holding",
                            mock.Mock(return_value=False)), \
         mock.patch.object(kis_client, "get_order_execution", _tracking_execution), \
         mock.patch.object(notion_repo, "fetch_holding_avg_price", lambda pid: 10_000.0), \
         mock.patch.object(kis_client, "_notify_warning_throttled", lambda k, m: None), \
         mock.patch.object(notion_repo, "create_ledger_record",
                            mock.Mock(return_value="ledger-1")) as ledger_mock, \
         mock.patch.object(notion_repo, "close_auto_holding", mock.Mock()) as close_mock:
        result = kis_client._reconcile_pending_sell_orders("token", "page-1", inp, TODAY, 0)

    assert seen_order_nos == ["ORD_A", "ORD_B"], "두 주문 모두 확인해야 한다(최신 1건만 X)"
    assert result == "closed"
    # ORD_A(부분체결 후 만료)는 원장에 반영되지 않고, ORD_B(전량체결)가
    # 최종 청산으로 이어진다.
    ledger_mock.assert_called_once()
    assert ledger_mock.call_args.kwargs["shares"] == 4
    close_mock.assert_called_once_with("page-1")


def test_recovery_buy_date_lookup_failure_falls_back_to_today_only_scope():
    """매수일을 못 구하면(과거 포지션과 섞이지 않게) 조회 범위를 오늘로만
    좁힌다 - 예전(당일 한정) 동작으로 안전하게 후퇴."""
    inp = _holding()
    with mock.patch.object(notion_repo, "fetch_holding_buy_date",
                            mock.Mock(return_value=None)), \
         mock.patch.object(notion_repo, "fetch_pending_sell_orders",
                            mock.Mock(return_value=[])) as fetch_mock:
        result = kis_client._reconcile_pending_sell_orders("token", "page-1", inp, TODAY, 0)

    fetch_mock.assert_called_once_with(inp.ticker, kis_client.ACCOUNT_TYPE, since=TODAY)
    assert result == "clear"


def test_recovery_lookup_failure_blocks_new_sell():
    inp = _holding()
    with mock.patch.object(notion_repo, "fetch_holding_buy_date",
                            mock.Mock(return_value=date(2026, 9, 1))), \
         mock.patch.object(notion_repo, "fetch_pending_sell_orders",
                            mock.Mock(side_effect=RuntimeError("노션 조회 실패"))):
        result = kis_client._reconcile_pending_sell_orders("token", "page-1", inp, TODAY, 5)

    assert result == "blocked"


# ── get_order_execution(): 조회 기간 확장 + 못 찾음/0체결 구분 ───

def test_get_order_execution_queries_from_order_date_to_today(monkeypatch):
    captured = {}

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"output1": []}

    def _fake_get(url, headers, params, timeout):
        captured["params"] = params
        return _Resp()

    monkeypatch.setattr(kis_client, "_today", lambda: TODAY)
    monkeypatch.setattr(kis_client.requests, "get", _fake_get)
    monkeypatch.setenv("KIS_APP_KEY", "k")
    monkeypatch.setenv("KIS_APP_SECRET", "s")
    monkeypatch.setenv("KIS_ACCOUNT", "12345678-01")

    result = kis_client.get_order_execution("token", "ORD1", order_date=YESTERDAY)

    assert result is None
    assert captured["params"]["INQR_STRT_DT"] == YESTERDAY.strftime("%Y%m%d")
    assert captured["params"]["INQR_END_DT"] == TODAY.strftime("%Y%m%d")


def test_get_order_execution_distinguishes_zero_fill_from_not_found(monkeypatch):
    class _Resp:
        def __init__(self, rows):
            self._rows = rows

        def raise_for_status(self):
            pass

        def json(self):
            return {"output1": self._rows}

    monkeypatch.setattr(kis_client, "_today", lambda: TODAY)
    monkeypatch.setenv("KIS_APP_KEY", "k")
    monkeypatch.setenv("KIS_APP_SECRET", "s")
    monkeypatch.setenv("KIS_ACCOUNT", "12345678-01")

    # 찾음, 0체결
    monkeypatch.setattr(
        kis_client.requests, "get",
        lambda *a, **k: _Resp([{"odno": "ORD1", "tot_ccld_qty": "0", "avg_prvs": "0"}]),
    )
    found_zero = kis_client.get_order_execution("token", "ORD1", order_date=YESTERDAY)
    assert found_zero == {"filled_qty": 0, "avg_price": 0.0}

    # 못 찾음
    monkeypatch.setattr(kis_client.requests, "get", lambda *a, **k: _Resp([]))
    not_found = kis_client.get_order_execution("token", "ORD1", order_date=YESTERDAY)
    assert not_found is None


# ── run_auto_sell() 통합 ─────────────────────────────────────────

def _stub_gates(monkeypatch) -> None:
    monkeypatch.setattr(kis_client, "_auto_trade_configured", lambda: True)
    monkeypatch.setattr(kis_client, "_within_trading_hours", lambda *a, **k: True)
    monkeypatch.setattr(kis_client, "_is_trading_day", lambda *a, **k: True)
    monkeypatch.setattr(kis_client, "get_access_token", lambda: "token")
    monkeypatch.setattr(kis_client, "_today", lambda: TODAY)


def test_run_auto_sell_checks_pending_before_new_sell_even_when_sellable_positive(monkeypatch):
    """매도가능수량이 양수라도 미확정 주문이 있으면 그걸 먼저 정리한다.
    이번엔 전날 부분체결 후 만료라 released로 풀리고, 남은 수량(6)에 대해
    이 회차 안에서 새 매도가 나간다."""
    _stub_gates(monkeypatch)
    inp = _holding()
    monkeypatch.setattr(
        kis_client, "get_account_balance",
        lambda token: {"account_size": 1.0, "available_cash": 1.0,
                       "holdings": [{"ticker": inp.ticker, "qty": 6, "sellable": 6}]},
    )
    monkeypatch.setattr(notion_repo, "fetch_holding_buy_date", lambda pid: date(2026, 9, 1))
    monkeypatch.setattr(
        notion_repo, "fetch_pending_sell_orders",
        lambda *a, **k: [_pending_order(qty=10.0)],
    )
    monkeypatch.setattr(notion_repo, "ledger_exists_for_holding", lambda pid: False)
    monkeypatch.setattr(kis_client, "get_order_execution",
                        lambda *a, **k: {"filled_qty": 4, "avg_price": 9_900.0})
    monkeypatch.setattr(kis_client, "_notify_warning_throttled", lambda k, m: None)
    monkeypatch.setattr(kis_client, "get_current_price", lambda token, ticker: "10000")
    place_mock = mock.Mock(return_value={"status": "sent", "order_no": "ORD_NEW"})
    monkeypatch.setattr(kis_client, "place_market_sell_order", place_mock)
    monkeypatch.setattr(kis_client, "time", mock.Mock(sleep=lambda s: None))

    kis_client.run_auto_sell(holdings=[("page-1", inp)])

    place_mock.assert_called_once()
    assert place_mock.call_args.args[2] == 6, "잔여 매도가능수량(6주) 그대로 새 매도를 내야 한다"


def test_run_auto_sell_blocked_by_ambiguous_order_never_places_new_sell(monkeypatch):
    """상태 불명 주문이 있으면 매도가능수량이 양수여도 신규 매도를 내지 않는다."""
    _stub_gates(monkeypatch)
    inp = _holding()
    monkeypatch.setattr(
        kis_client, "get_account_balance",
        lambda token: {"account_size": 1.0, "available_cash": 1.0,
                       "holdings": [{"ticker": inp.ticker, "qty": 6, "sellable": 6}]},
    )
    monkeypatch.setattr(notion_repo, "fetch_holding_buy_date", lambda pid: date(2026, 9, 1))
    monkeypatch.setattr(
        notion_repo, "fetch_pending_sell_orders",
        lambda *a, **k: [_pending_order(status="실패", order_no="")],
    )
    monkeypatch.setattr(kis_client, "_notify_warning_throttled", lambda k, m: None)
    place_mock = mock.Mock()
    monkeypatch.setattr(kis_client, "place_market_sell_order", place_mock)

    kis_client.run_auto_sell(holdings=[("page-1", inp)])

    place_mock.assert_not_called()


def test_run_auto_sell_full_fill_confirmed_inline(monkeypatch):
    """매도 가능한 종목이 조건을 충족해 주문을 내고, 같은 회차에 전량
    체결까지 확인되면 청산·원장 기록이 이뤄진다(기존 당일 경로 유지)."""
    _stub_gates(monkeypatch)
    inp = _holding()
    monkeypatch.setattr(
        kis_client, "get_account_balance",
        lambda token: {"account_size": 1.0, "available_cash": 1.0,
                       "holdings": [{"ticker": inp.ticker, "qty": 10, "sellable": 10}]},
    )
    monkeypatch.setattr(notion_repo, "fetch_holding_buy_date", lambda pid: date(2026, 9, 1))
    monkeypatch.setattr(notion_repo, "fetch_pending_sell_orders", lambda *a, **k: [])
    monkeypatch.setattr(kis_client, "get_current_price", lambda token, ticker: "10000")
    monkeypatch.setattr(
        kis_client, "place_market_sell_order",
        lambda *a, **k: {"status": "sent", "order_no": "ORD1"},
    )
    monkeypatch.setattr(kis_client, "time", mock.Mock(sleep=lambda s: None))
    monkeypatch.setattr(kis_client, "get_order_execution",
                        lambda *a, **k: {"filled_qty": 10, "avg_price": 9_950.0})
    monkeypatch.setattr(notion_repo, "ledger_exists_for_holding", lambda pid: False)
    monkeypatch.setattr(notion_repo, "fetch_holding_avg_price", lambda pid: 10_000.0)
    ledger_mock = mock.Mock(return_value="ledger-1")
    close_mock = mock.Mock()
    monkeypatch.setattr(notion_repo, "create_ledger_record", ledger_mock)
    monkeypatch.setattr(notion_repo, "close_auto_holding", close_mock)

    kis_client.run_auto_sell(holdings=[("page-1", inp)])

    ledger_mock.assert_called_once()
    assert ledger_mock.call_args.kwargs["shares"] == 10
    assert ledger_mock.call_args.kwargs["exit_date"] == TODAY
    close_mock.assert_called_once_with("page-1")


def test_run_auto_sell_zero_fill_leaves_holding_open_no_dup_next_call(monkeypatch):
    """접수 성공, 체결 0주 -> 이번 회차엔 청산도 원장 기록도 없고, 매도
    가능수량이 여전히 0인 다음 회차(같은 날)도 새 주문을 내지 않는다."""
    _stub_gates(monkeypatch)
    inp = _holding()
    monkeypatch.setattr(kis_client, "get_current_price", lambda token, ticker: "10000")
    monkeypatch.setattr(kis_client, "time", mock.Mock(sleep=lambda s: None))
    monkeypatch.setattr(notion_repo, "ledger_exists_for_holding", lambda pid: False)
    monkeypatch.setattr(notion_repo, "fetch_holding_buy_date", lambda pid: date(2026, 9, 1))
    ledger_mock = mock.Mock()
    close_mock = mock.Mock()
    monkeypatch.setattr(notion_repo, "create_ledger_record", ledger_mock)
    monkeypatch.setattr(notion_repo, "close_auto_holding", close_mock)

    # 1회차: 매도가능수량 10, 미확정 주문 없음 -> 매도 주문을 낸다. 체결
    # 확인은 조회 자체에서 주문을 못 찾음(당일이라 여전히 대기).
    monkeypatch.setattr(
        kis_client, "get_account_balance",
        lambda token: {"account_size": 1.0, "available_cash": 1.0,
                       "holdings": [{"ticker": inp.ticker, "qty": 10, "sellable": 10}]},
    )
    monkeypatch.setattr(notion_repo, "fetch_pending_sell_orders", lambda *a, **k: [])
    monkeypatch.setattr(kis_client, "get_order_execution", lambda *a, **k: None)
    place_mock = mock.Mock(return_value={"status": "sent", "order_no": "ORD1"})
    monkeypatch.setattr(kis_client, "place_market_sell_order", place_mock)

    kis_client.run_auto_sell(holdings=[("page-1", inp)])

    ledger_mock.assert_not_called()
    close_mock.assert_not_called()
    assert place_mock.call_count == 1

    # 2회차(같은 날, 아직 미체결) - 매도가능수량 0, 미확정 주문 1건 남음.
    monkeypatch.setattr(
        kis_client, "get_account_balance",
        lambda token: {"account_size": 1.0, "available_cash": 1.0,
                       "holdings": [{"ticker": inp.ticker, "qty": 10, "sellable": 0}]},
    )
    monkeypatch.setattr(
        notion_repo, "fetch_pending_sell_orders",
        lambda *a, **k: [_pending_order(qty=10.0, order_date=TODAY)],
    )

    kis_client.run_auto_sell(holdings=[("page-1", inp)])

    assert place_mock.call_count == 1, "미체결 상태에서는 같은 종목을 다시 주문하면 안 된다"
    ledger_mock.assert_not_called()
    close_mock.assert_not_called()


def test_run_auto_sell_recovers_previous_trading_day_fill_across_process_restart(monkeypatch):
    """run_auto_sell()부터 끝까지 - 전날 회차(별도 프로세스로 간주)가 낸
    매도가 전량 체결됐지만 원장 실패로 미확정이던 걸, 다음 거래일 회차가
    새 프로세스에서 노션 조회만으로 이어받아 복구한다(로컬 상태 없음)."""
    _stub_gates(monkeypatch)
    inp = _holding()
    monkeypatch.setattr(
        kis_client, "get_account_balance",
        lambda token: {"account_size": 1.0, "available_cash": 1.0,
                       "holdings": [{"ticker": inp.ticker, "qty": 10, "sellable": 0}]},
    )
    monkeypatch.setattr(notion_repo, "fetch_holding_buy_date", lambda pid: date(2026, 9, 1))
    monkeypatch.setattr(
        notion_repo, "fetch_pending_sell_orders",
        lambda *a, **k: [_pending_order(order_no="ORD1", qty=10.0, order_date=YESTERDAY)],
    )
    monkeypatch.setattr(notion_repo, "ledger_exists_for_holding", lambda pid: False)
    monkeypatch.setattr(kis_client, "get_order_execution",
                        lambda *a, **k: {"filled_qty": 10, "avg_price": 9_950.0})
    monkeypatch.setattr(notion_repo, "fetch_holding_avg_price", lambda pid: 10_000.0)
    ledger_mock = mock.Mock(return_value="ledger-1")
    close_mock = mock.Mock()
    monkeypatch.setattr(notion_repo, "create_ledger_record", ledger_mock)
    monkeypatch.setattr(notion_repo, "close_auto_holding", close_mock)
    place_mock = mock.Mock()
    monkeypatch.setattr(kis_client, "place_market_sell_order", place_mock)

    kis_client.run_auto_sell(holdings=[("page-1", inp)])

    place_mock.assert_not_called(), "복구만으로 끝나야 한다 - 새 매도를 또 내면 안 된다"
    ledger_mock.assert_called_once()
    assert ledger_mock.call_args.kwargs["exit_date"] == YESTERDAY
    close_mock.assert_called_once_with("page-1")


# ── 워크플로 필수 환경변수 ────────────────────────────────────────

def test_auto_trade_workflow_passes_notion_ledger_db_id_to_watch_step():
    """자동매도 체결 확정(ledger_exists_for_holding/create_ledger_record)은
    NOTION_LEDGER_DB_ID가 필요하다 - 워크플로 "watch" 스텝에 실제로
    전달되는지 확인한다(비밀값은 매핑 문자열 그대로만 확인, 실제 값은
    다루지 않는다)."""
    workflow_path = Path(__file__).resolve().parent.parent / ".github/workflows/auto-trade.yml"
    content = workflow_path.read_text(encoding="utf-8")

    start = content.index('name: "Watch for breakouts and auto-trade"'.replace('"', ""))
    # 다음 스텝("- name:")이 시작되기 전까지만 본다.
    next_step = content.index("\n      - name:", start)
    step_block = content[start:next_step]

    assert "NOTION_LEDGER_DB_ID: ${{ secrets.NOTION_LEDGER_DB_ID }}" in step_block


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
    reconcile_mock = mock.Mock()
    monkeypatch.setattr(kis_client, "_check_sell_fill", check_fill_mock)
    monkeypatch.setattr(kis_client, "_reconcile_pending_sell_orders", reconcile_mock)
    monkeypatch.setattr(kis_client, "_auto_trade_configured", lambda: True)
    monkeypatch.setattr(kis_client, "_auto_trade_paused", lambda: False)
    monkeypatch.setattr(kis_client, "_within_trading_hours", lambda *a, **k: True)
    monkeypatch.setattr(kis_client, "_is_trading_day", lambda *a, **k: True)

    kis_client.run_auto_pyramid(holdings=[])

    check_fill_mock.assert_not_called()
    reconcile_mock.assert_not_called()
