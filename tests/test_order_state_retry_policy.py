"""검토 항목 #5 검증 - 주문 상태별 재시도 구분과 중복 방지.

실제 노션에 쓰지 않는다 - notion_repo.*는 전부 monkeypatch로 대체한다.

이 파일이 확인하는 최소 상태 구분(place_market_buy_order/_check_order_allowed
기준):
  - 주문 전송 "전" 조회 실패(has_order_today/count_* 예외)         -> 일시적,
    다음 회차(조회가 정상화되면) 재평가 가능. fail-closed로 이번 회차만 막음.
  - 명확한 거부(rt_cd != "0")                                      -> "거부"
    기록, has_order_today 차단 대상에서 제외(다음 회차 재시도 가능하나
    MAX_REJECTIONS_PER_STOCK 누적 시 별도로 재시도 중단).
  - 전송 후 응답 시간초과 등 접수 여부 불명(_send_market_buy 예외)   -> "실패"
    기록, has_order_today 차단 대상(재전송 금지).
  - 접수·미체결/부분체결·완전체결(rt_cd == "0")                     -> "성공"
    기록, has_order_today 차단 대상(중복 진입 방지) - 이 코드베이스는
    미체결/부분체결/완전체결을 노션 상태로 구분하지 않고 "성공"(주문
    전송 접수) 하나로 취급한다. 취소 주문 API 자체가 없어 "취소" 상태는
    해당 없음.
"""

from __future__ import annotations

from datetime import date, datetime
from unittest import mock
from zoneinfo import ZoneInfo

import pytest

import kis_client
import notion_repo

KST = ZoneInfo("Asia/Seoul")
TODAY = date(2026, 9, 16)


# ── _check_order_allowed(): 재시도 허용/차단 판단 ──────────────

def test_allowed_when_no_prior_order_and_no_rejection_streak(monkeypatch):
    # 이 시나리오는 정상 신규매수 통과 판정을 보려는 것이라, 운영 보류
    # 설정(MAX_ORDERS_PER_DAY=0)에 의존하지 않도록 정상값(3)으로 고정한다.
    monkeypatch.setattr(kis_client, "MAX_ORDERS_PER_DAY", 3)
    monkeypatch.setattr(notion_repo, "has_order_today", lambda *a, **k: False)
    monkeypatch.setattr(notion_repo, "count_rejected_orders_today", lambda *a, **k: 0)
    monkeypatch.setattr(notion_repo, "count_orders_by_status_today",
                        lambda *a, **k: {"성공": 0, "주문중": 0})
    assert kis_client._check_order_allowed("005930") is None


def test_blocked_when_prior_order_recorded_today(monkeypatch):
    # has_order_today가 True인 경우 = 오늘 상태가 성공/주문중/실패 중
    # 하나라도 이미 있다는 뜻 - 셋 다 재전송을 막아야 한다는 동일 정책.
    monkeypatch.setattr(notion_repo, "has_order_today", lambda *a, **k: True)
    reason = kis_client._check_order_allowed("005930")
    assert reason is not None
    assert "이미 주문" in reason


def test_transient_lookup_failure_is_fail_closed_but_not_permanent(monkeypatch):
    # 1회차: 노션 조회 자체가 일시적으로 실패 - 이번 회차는 막되(fail-closed),
    # 다음 회차에 조회가 정상화되면 재평가할 수 있어야 한다(영구 차단 아님).
    # 이 시나리오는 정상 신규매수 재평가 통과를 보려는 것이라, 운영 보류
    # 설정(MAX_ORDERS_PER_DAY=0)에 의존하지 않도록 정상값(3)으로 고정한다.
    monkeypatch.setattr(kis_client, "MAX_ORDERS_PER_DAY", 3)
    monkeypatch.setattr(notion_repo, "has_order_today",
                        mock.Mock(side_effect=RuntimeError("타임아웃")))
    monkeypatch.setattr(kis_client, "_notify_warning_throttled", lambda k, m: None)
    reason1 = kis_client._check_order_allowed("005930")
    assert reason1 is not None and "조회 실패" in reason1

    monkeypatch.setattr(notion_repo, "has_order_today", lambda *a, **k: False)
    monkeypatch.setattr(notion_repo, "count_rejected_orders_today", lambda *a, **k: 0)
    monkeypatch.setattr(notion_repo, "count_orders_by_status_today",
                        lambda *a, **k: {"성공": 0, "주문중": 0})
    reason2 = kis_client._check_order_allowed("005930")
    assert reason2 is None, "조회가 정상화되면 같은 회차 안에서도 다시 통과해야 한다"


def test_repeated_explicit_rejections_stop_retry_even_though_not_blocked_by_has_order_today(
    monkeypatch,
):
    # "거부"는 has_order_today의 차단 대상이 아니지만(체결 가능성이
    # 사실상 없어 그날 다시 시도할 수 있어야 함), 같은 종목이 오늘
    # MAX_REJECTIONS_PER_STOCK번 넘게 거부되면 구조적 문제로 보고
    # 별도 정책으로 재시도를 막는다.
    monkeypatch.setattr(notion_repo, "has_order_today", lambda *a, **k: False)
    monkeypatch.setattr(notion_repo, "count_rejected_orders_today",
                        lambda *a, **k: kis_client.MAX_REJECTIONS_PER_STOCK)
    monkeypatch.setattr(kis_client, "_notify_warning_throttled", lambda k, m: None)
    reason = kis_client._check_order_allowed("005930")
    assert reason is not None and "거부" in reason and "재시도 중단" in reason


def test_single_rejection_below_streak_cap_does_not_block_retry(monkeypatch):
    # 이 시나리오는 정상 신규매수 재시도 허용을 보려는 것이라, 운영 보류
    # 설정(MAX_ORDERS_PER_DAY=0)에 의존하지 않도록 정상값(3)으로 고정한다.
    monkeypatch.setattr(kis_client, "MAX_ORDERS_PER_DAY", 3)
    monkeypatch.setattr(notion_repo, "has_order_today", lambda *a, **k: False)
    monkeypatch.setattr(notion_repo, "count_rejected_orders_today",
                        lambda *a, **k: kis_client.MAX_REJECTIONS_PER_STOCK - 1)
    monkeypatch.setattr(notion_repo, "count_orders_by_status_today",
                        lambda *a, **k: {"성공": 0, "주문중": 0})
    assert kis_client._check_order_allowed("005930") is None


def test_daily_cap_counts_success_and_pending_together(monkeypatch):
    monkeypatch.setattr(notion_repo, "has_order_today", lambda *a, **k: False)
    monkeypatch.setattr(notion_repo, "count_rejected_orders_today", lambda *a, **k: 0)
    monkeypatch.setattr(
        notion_repo, "count_orders_by_status_today",
        lambda *a, **k: {"성공": 1, "주문중": kis_client.MAX_ORDERS_PER_DAY - 1},
    )
    reason = kis_client._check_order_allowed("005930")
    assert reason is not None and "일일 상한" in reason


# ── place_market_buy_order(): 전송 결과별 노션 기록 ────────────

def _allow_order(monkeypatch):
    monkeypatch.setattr(kis_client, "_check_order_allowed", lambda *a, **k: None)


def test_ambiguous_send_failure_records_status_failed_not_rejected(monkeypatch):
    # 전송 자체가 예외(타임아웃 등)로 끝나면 접수 여부를 모른다 - "거부"가
    # 아니라 "실패"로 기록해 다음 회차 has_order_today가 재전송을 막게 한다.
    _allow_order(monkeypatch)
    monkeypatch.setattr(kis_client, "_create_pending_record", lambda **k: "page-1")
    monkeypatch.setattr(kis_client, "_send_market_buy",
                        mock.Mock(side_effect=TimeoutError("응답 없음")))
    monkeypatch.setattr(kis_client, "_notify_failure", lambda msg: None)
    update_mock = mock.Mock()
    monkeypatch.setattr(kis_client, "_update_order_record", update_mock)

    result = kis_client.place_market_buy_order("token", "005930", 10, name="삼성전자")

    assert result["status"] == "error"
    update_mock.assert_called_once()
    assert update_mock.call_args.kwargs["status"] == "실패"


def test_explicit_rejection_records_status_rejected(monkeypatch):
    _allow_order(monkeypatch)
    monkeypatch.setattr(kis_client, "_create_pending_record", lambda **k: "page-1")
    monkeypatch.setattr(kis_client, "_send_market_buy",
                        lambda *a, **k: {"rt_cd": "1", "msg1": "주문가능수량 부족"})
    monkeypatch.setattr(kis_client, "_notify_failure", lambda msg: None)
    update_mock = mock.Mock()
    monkeypatch.setattr(kis_client, "_update_order_record", update_mock)

    result = kis_client.place_market_buy_order("token", "005930", 10, name="삼성전자")

    assert result["status"] == "rejected"
    assert update_mock.call_args.kwargs["status"] == "거부"


def test_successful_send_records_status_success_with_order_no(monkeypatch):
    _allow_order(monkeypatch)
    monkeypatch.setattr(kis_client, "_create_pending_record", lambda **k: "page-1")
    monkeypatch.setattr(
        kis_client, "_send_market_buy",
        lambda *a, **k: {"rt_cd": "0", "output": {"ODNO": "ORD123"}},
    )
    update_mock = mock.Mock()
    monkeypatch.setattr(kis_client, "_update_order_record", update_mock)

    result = kis_client.place_market_buy_order("token", "005930", 10, name="삼성전자")

    assert result == {"status": "sent", "order_no": "ORD123"}
    assert update_mock.call_args.kwargs["status"] == "성공"


def test_blocked_before_send_never_calls_kis_api(monkeypatch):
    # _check_order_allowed가 막으면(중복·상한·조회실패 등) 실제 KIS
    # 전송 자체를 시도하지 않는다 - 불명확한 주문의 재전송 자체를 막는
    # 가장 앞단 방어선.
    monkeypatch.setattr(kis_client, "_check_order_allowed",
                        lambda *a, **k: "오늘 이미 주문했습니다")
    monkeypatch.setattr(kis_client, "_notify_failure", lambda msg: None)
    send_mock = mock.Mock()
    monkeypatch.setattr(kis_client, "_send_market_buy", send_mock)

    result = kis_client.place_market_buy_order("token", "005930", 10, name="삼성전자")

    assert result["status"] == "blocked"
    send_mock.assert_not_called()


def test_pending_record_failure_blocks_order_without_sending(monkeypatch):
    # 사전 기록("주문중") 자체가 실패하면 주문을 내지 않는다(fail-closed) -
    # 전송 후 기록이 실패하는 경우와 달리, 기록 없이 나간 주문은 중복
    # 방지 장치가 전혀 없어지므로 더 위험하다.
    _allow_order(monkeypatch)
    monkeypatch.setattr(kis_client, "_create_pending_record",
                        mock.Mock(side_effect=RuntimeError("노션 기록 실패")))
    monkeypatch.setattr(kis_client, "_notify_failure", lambda msg: None)
    send_mock = mock.Mock()
    monkeypatch.setattr(kis_client, "_send_market_buy", send_mock)

    result = kis_client.place_market_buy_order("token", "005930", 10, name="삼성전자")

    assert result["status"] == "blocked"
    send_mock.assert_not_called()
