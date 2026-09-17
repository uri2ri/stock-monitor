"""체결 조회의 주문번호 대조 회귀 테스트.

배경: 2026-09-17 GS 매도(주문번호 0000006229)가 증권사에서는 체결됐는데
`_reconcile_sell`이 매 회차 "매도 미체결/부분체결"로 판정해 노션 행이
`주문중`으로 남았다. 기존 테스트는 `get_order_execution` 자체를 통째로
Mock해서 대조 로직을 한 번도 실행하지 않았다 - 주문 응답 `ODNO`와 조회
응답 `odno`에 같은 문자열을 넣어두면 어떤 표기 차이도 드러나지 않는다.

여기서는 `get_order_execution`을 **실제로 실행**하고 `requests.get`만
합성 응답으로 바꾼다. 네트워크·자격증명은 helpers로 차단한다.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import Mock

import pytest
import requests

import core
import kis_client as k
import notion_repo as n
from helpers import block_external_http, scrub_credential_env


ORDER_NO = "0000006229"     # 노션 '주문번호' (주문 응답 ODNO, 0 패딩 10자리)


@pytest.fixture
def kis_env(monkeypatch):
    """실 자격증명·실 HTTP를 막고 더미 KIS 환경만 넣는다."""
    scrub_credential_env(monkeypatch)
    block_external_http(monkeypatch)
    monkeypatch.setenv("KIS_APP_KEY", "dummy-key")
    monkeypatch.setenv("KIS_APP_SECRET", "dummy-secret")
    monkeypatch.setenv("KIS_ACCOUNT", "12345678-01")


def _row(odno: str, *, qty: int = 13, avg: str = "112460",
         pdno: str = "078930", side: str = "01") -> dict:
    """inquire-daily-ccld output1 한 행 (쓰는 칸만)."""
    return {
        "ord_dt": "20260917", "odno": odno, "pdno": pdno,
        "sll_buy_dvsn_cd": side, "ord_qty": "13",
        "tot_ccld_qty": str(qty), "avg_prvs": avg, "rmn_qty": "0",
    }


_UNSET = object()   # output1=None("null 응답")과 "생략"을 구분하는 센티널


def _respond(monkeypatch, rows, *, rt_cd="0", tr_cont="", output1=_UNSET):
    """requests.get이 돌려줄 합성 응답을 심고, 전달된 params를 캡처한다."""
    captured = {}

    class _Resp:
        status_code = 200
        headers = {"tr_cont": tr_cont}

        def raise_for_status(self):
            pass

        def json(self):
            body = {"rt_cd": rt_cd, "msg1": "정상처리 되었습니다."}
            body["output1"] = rows if output1 is _UNSET else output1
            return body

    def _get(url, **kwargs):
        captured["url"] = url
        captured["params"] = kwargs.get("params", {})
        return _Resp()

    monkeypatch.setattr(requests, "get", _get)
    return captured


# ── 순수 대조 함수 ──────────────────────────────────────────

@pytest.mark.parametrize("resp_odno, wanted, expected", [
    # 0 패딩 표기 차이만 흡수한다
    ("0000006229", "0000006229", True),    # 정확일치 (기존 동작)
    ("6229",       "0000006229", True),    # 조회 응답이 패딩을 뗀 경우
    ("0000006229", "6229",       True),    # 반대 방향
    ("0000006229", " 6229 ",     True),    # 공백은 무시
    # 빈 값은 어느 쪽이든 매칭 금지
    ("",           "0000006229", False),
    (None,         "0000006229", False),
    ("0000006229", "",           False),
    ("",           "",           False),
    # 0뿐인 값은 유효한 주문번호가 아니다 - 정규화하면 양쪽 다 ""가 되어
    # 서로 같아져버리므로 명시적으로 막는다
    ("0000000000", "0000006229", False),
    ("0000000000", "0000000000", False),
    ("0",          "0",          False),
    ("0",          "0000000000", False),
    # 다른 주문을 끌어오지 않는다 (접미사 일치 금지)
    ("16229",      "0000006229", False),
    ("62290",      "0000006229", False),
    ("0000006228", "0000006229", False),
    # 숫자가 아니면 원문 정확일치만
    ("ORD123",     "ORD123",     True),
    ("ORD123",     "ORD124",     False),
    ("6229",       "ORD6229",    False),
])
def test_same_order_no(resp_odno, wanted, expected):
    assert k._same_order_no(resp_odno, wanted) is expected


# ── get_order_execution 전체 경로 ───────────────────────────

def test_exact_match_still_returns_fill(kis_env, monkeypatch):
    """기존 동작 회귀 - 표기가 같으면 지금까지와 똑같이 체결을 돌려준다."""
    _respond(monkeypatch, [_row(ORDER_NO)])
    assert k.get_order_execution("tok", ORDER_NO) == {
        "filled_qty": 13, "avg_price": 112460.0}


def test_padding_difference_is_matched(kis_env, monkeypatch):
    """GS 건의 가설 재현 - 응답이 패딩을 떼고 와도 체결로 인식해야 한다."""
    _respond(monkeypatch, [_row("6229")])
    assert k.get_order_execution("tok", ORDER_NO) == {
        "filled_qty": 13, "avg_price": 112460.0}


def test_other_order_is_not_matched(kis_env, monkeypatch):
    """다른 주문만 있는 응답에서 체결을 지어내지 않는다."""
    _respond(monkeypatch, [_row("16229"), _row("0000006228")])
    assert k.get_order_execution("tok", ORDER_NO) is None


def test_blank_odno_rows_are_not_matched(kis_env, monkeypatch):
    _respond(monkeypatch, [_row(""), _row("0000000000")])
    assert k.get_order_execution("tok", ORDER_NO) is None


def test_duplicate_matches_raise(kis_env, monkeypatch):
    """같은 주문번호가 두 행이면 넘겨짚지 않고 보류시킨다."""
    _respond(monkeypatch, [_row(ORDER_NO), _row("6229", qty=7)])
    with pytest.raises(RuntimeError, match="여러 건"):
        k.get_order_execution("tok", ORDER_NO)


def test_found_but_unfilled_returns_none(kis_env, monkeypatch):
    """찾았는데 체결수량 0 - 정상적인 미체결이라 None."""
    _respond(monkeypatch, [_row("6229", qty=0)])
    assert k.get_order_execution("tok", ORDER_NO) is None


def test_not_found_is_logged_distinctly(kis_env, monkeypatch, caplog):
    """'못 찾음'과 '찾았는데 미체결'을 로그로 구분한다."""
    _respond(monkeypatch, [_row("16229")])
    with caplog.at_level("INFO", logger=k.logger.name):
        assert k.get_order_execution("tok", ORDER_NO) is None
    assert "주문번호 미발견" in caplog.text

    caplog.clear()
    _respond(monkeypatch, [_row("6229", qty=0)])
    with caplog.at_level("INFO", logger=k.logger.name):
        assert k.get_order_execution("tok", ORDER_NO) is None
    assert "주문번호 미발견" not in caplog.text


def test_order_day_drives_query_range(kis_env, monkeypatch):
    """전날 주문도 그 날짜로 조회한다 (복구가 날짜에 막히지 않는 근거)."""
    captured = _respond(monkeypatch, [_row("6229")])
    k.get_order_execution("tok", ORDER_NO, order_day=date(2026, 9, 16))
    assert captured["params"]["INQR_STRT_DT"] == "20260916"
    assert captured["params"]["INQR_END_DT"] == "20260916"


# ── 기존 오류 처리 회귀 (대조 변경으로 깨지지 않아야 함) ──────

def test_error_response_still_raises(kis_env, monkeypatch):
    _respond(monkeypatch, [], rt_cd="1")
    with pytest.raises(RuntimeError, match="체결 조회 응답 오류"):
        k.get_order_execution("tok", ORDER_NO)


@pytest.mark.parametrize("bad", [None, {}, "x"])
def test_invalid_output1_still_raises(kis_env, monkeypatch, bad):
    _respond(monkeypatch, [], output1=bad)
    with pytest.raises(RuntimeError, match="유효하지 않습니다"):
        k.get_order_execution("tok", ORDER_NO)


@pytest.mark.parametrize("tr_cont", ["F", "M"])
def test_continuation_still_raises(kis_env, monkeypatch, tr_cont):
    _respond(monkeypatch, [_row(ORDER_NO)], tr_cont=tr_cont)
    with pytest.raises(RuntimeError, match="연속조회"):
        k.get_order_execution("tok", ORDER_NO)


# ── 호출부 회귀: _reconcile_sell ────────────────────────────

@pytest.fixture
def sell_env(monkeypatch, kis_env):
    """GS 건과 같은 모양의 미확인 매도 1건. get_order_execution은 Mock하지
    않는다 - 실제 대조 로직을 타야 이 회귀의 의미가 있다."""
    inp = core.HoldingInput('078930', 'GS', 'KOSPI', 120600, 13,
                            prev_stop_loss=110592)
    order = dict(page_id='order-page', ticker='078930', order_no=ORDER_NO,
                 qty=13, holding_page_id='holding-page',
                 account_type=k.ACCOUNT_TYPE, reason='추세청산 (아침 배치 판정)',
                 order_day='2026-09-17')
    monkeypatch.setattr(k, '_today', lambda: date(2026, 9, 17))
    monkeypatch.setattr(k, '_notify_warning_throttled', Mock())
    monkeypatch.setattr(k, 'get_account_balance',
                        Mock(return_value={'holdings': []}))
    monkeypatch.setattr(k, '_record_ledger_after_sell', Mock(return_value=True))
    monkeypatch.setattr(n, 'close_auto_holding', Mock())
    monkeypatch.setattr(n, 'update_order_record', Mock())
    monkeypatch.setattr(n, 'holding_identity',
                        Mock(return_value={'ticker': '078930', 'status': '보유'}))
    return inp, order


def test_reconcile_closes_when_response_drops_padding(sell_env, monkeypatch):
    """GS 시나리오: 응답 표기가 달라도 전량 체결이면 청산까지 진행된다."""
    inp, order = sell_env
    _respond(monkeypatch, [_row("6229")])
    k._reconcile_sell('tok', 'holding-page', inp, order)
    n.close_auto_holding.assert_called_once_with('holding-page')
    assert n.update_order_record.call_args.kwargs['status'] == '성공'
    assert k._record_ledger_after_sell.call_args.kwargs['qty'] == 13


def test_reconcile_holds_when_order_absent(sell_env, monkeypatch):
    """주문번호를 못 찾으면 보유를 유지한다 (없는 체결을 지어내지 않는다)."""
    inp, order = sell_env
    _respond(monkeypatch, [_row("16229")])
    k._reconcile_sell('tok', 'holding-page', inp, order)
    n.close_auto_holding.assert_not_called()
    n.update_order_record.assert_not_called()


def test_reconcile_recovers_previous_day_order(sell_env, monkeypatch):
    """전날 주문도 전량 체결·잔고 0이면 복구된다 (날짜가 복구를 막지 않는다)."""
    inp, order = sell_env
    order['order_day'] = '2026-09-16'
    captured = _respond(monkeypatch, [_row("6229")])
    k._reconcile_sell('tok', 'holding-page', inp, order)
    assert captured["params"]["INQR_STRT_DT"] == "20260916"
    n.close_auto_holding.assert_called_once_with('holding-page')
    assert n.update_order_record.call_args.kwargs['status'] == '성공'


def test_reconcile_escalates_previous_day_when_still_unconfirmed(sell_env, monkeypatch):
    """전날 주문이 여전히 미확인이면 경고로 승격된다 (기존 동작 회귀)."""
    inp, order = sell_env
    order['order_day'] = '2026-09-16'
    _respond(monkeypatch, [_row("16229")])
    k._reconcile_sell('tok', 'holding-page', inp, order)
    n.close_auto_holding.assert_not_called()
    assert '이전 거래일 매도 미완료' in k._notify_warning_throttled.call_args[0][1]
