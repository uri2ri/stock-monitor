"""체결 조회가 끝내 안 될 때 잔고를 근거로 청산을 확정하는 경로 회귀 테스트.

배경(2026-09-17 GS, 주문번호 0000006229): 시장가 매도가 실제로 체결돼
D+2 가용현금이 매도대금만큼 늘었는데도 일별주문체결조회는 그 주문을 계속
"미체결"로 돌려줬다. 그 결과 점검표는 "보유", 주문은 "주문중"으로 남아
다음 거래일부터 매 회차 "이전 거래일 매도 미완료" 경고만 반복됐다 -
장부에는 청산 기록이 영영 안 남고(승률·R배수 통계 공백) 알림만 쌓였다.

보완: 이전 거래일 주문이 조회로 확인되지 않으면 증권사 잔고를 본다.
그 종목이 한 주도 없으면 주문 수량이 전부 나간 것이므로 청산을 확정한다.
잔고가 남아 있으면 진짜 미체결이므로 기존 경고를 그대로 올린다.

체결가는 알 수 없어 주문 시 참고가(주문가)로 적고, 매매일지 메모와 카톡
양쪽에 추정치임을 남긴다.

실 네트워크·자격증명은 helpers로 차단한다.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import Mock

import pytest

import core
import kis_client as k
import notion_repo as n
from helpers import block_external_http, scrub_credential_env

ORDER_NO = "0000006229"
ORDER_DAY = "2026-09-17"
REF_PRICE = 112400          # 주문 시 참고가 (노션 '주문가')


@pytest.fixture
def env(monkeypatch):
    """GS 건과 같은 모양 - 체결 조회는 끝내 미체결, 오늘은 다음 거래일."""
    scrub_credential_env(monkeypatch)
    block_external_http(monkeypatch)
    inp = core.HoldingInput('078930', 'GS', 'KOSPI', 120600, 13,
                            prev_stop_loss=110592, entry_atr=7154, units=1)
    order = dict(page_id='order-page', ticker='078930', order_no=ORDER_NO,
                 qty=13, price=REF_PRICE, holding_page_id='holding-page',
                 account_type=k.ACCOUNT_TYPE, reason='추세청산 (아침 배치 판정)',
                 order_day=ORDER_DAY)
    monkeypatch.setattr(k, '_today', lambda: date(2026, 9, 18))
    monkeypatch.setattr(k, '_notify_warning_throttled', Mock())
    monkeypatch.setattr(k, 'get_order_execution', Mock(return_value=None))
    monkeypatch.setattr(n, 'holding_identity',
                        Mock(return_value={'ticker': '078930', 'status': '보유'}))
    monkeypatch.setattr(n, 'close_auto_holding', Mock())
    monkeypatch.setattr(n, 'update_order_record', Mock())
    return inp, order


def _balance(monkeypatch, holdings):
    mock = Mock(return_value={'holdings': holdings})
    monkeypatch.setattr(k, 'get_account_balance', mock)
    return mock


def _ledger(monkeypatch, ok=True):
    mock = Mock(return_value=ok)
    monkeypatch.setattr(k, '_record_ledger_after_sell', mock)
    return mock


# ── 잔고가 비었으면 확정 ────────────────────────────────────

def test_settles_when_broker_has_no_shares(env, monkeypatch):
    """조회는 미체결이어도 잔고에 없으면 전량 체결로 확정한다."""
    inp, order = env
    _balance(monkeypatch, [])
    ledger = _ledger(monkeypatch)

    k._reconcile_sell('tok', 'holding-page', inp, order)

    n.close_auto_holding.assert_called_once_with('holding-page')
    assert n.update_order_record.call_args.kwargs['status'] == '성공'
    kwargs = ledger.call_args.kwargs
    assert kwargs['qty'] == 13                      # 주문 수량 전량
    assert kwargs['ref_price'] == float(REF_PRICE)  # 주문 참고가로 추정
    assert kwargs['estimated_price'] is True
    assert kwargs['today'] == date(2026, 9, 17), "청산일은 재처리일이 아니라 주문일"


def test_settlement_notifies_that_price_is_an_estimate(env, monkeypatch):
    """확정했으면 추정 청산가임을 사람에게 알린다 - 조용히 넘어가지 않는다."""
    inp, order = env
    _balance(monkeypatch, [])
    _ledger(monkeypatch)

    k._reconcile_sell('tok', 'holding-page', inp, order)

    msg = k._notify_warning_throttled.call_args[0][1]
    assert '추정치' in msg and '112,400' in msg


def test_partial_fill_with_empty_balance_settles_full_order_qty(env, monkeypatch):
    """부분체결로 보이더라도 잔고가 0이면 주문 수량 전부가 나간 것이다."""
    inp, order = env
    monkeypatch.setattr(k, 'get_order_execution',
                        Mock(return_value={'filled_qty': 5, 'avg_price': 112000}))
    _balance(monkeypatch, [])
    ledger = _ledger(monkeypatch)

    k._reconcile_sell('tok', 'holding-page', inp, order)

    assert ledger.call_args.kwargs['qty'] == 13
    n.close_auto_holding.assert_called_once_with('holding-page')


def test_ledger_failure_leaves_holding_open(env, monkeypatch):
    """매매일지 기록이 실패하면 점검표 청산·주문 갱신도 하지 않는다."""
    inp, order = env
    _balance(monkeypatch, [])
    _ledger(monkeypatch, ok=False)

    k._reconcile_sell('tok', 'holding-page', inp, order)

    n.close_auto_holding.assert_not_called()
    n.update_order_record.assert_not_called()


# ── 잔고가 남아 있으면 기존 경고 유지 ───────────────────────

def test_keeps_manual_check_warning_when_shares_remain(env, monkeypatch):
    """잔고에 남아 있으면 진짜 미체결 - 임의로 청산하지 않는다."""
    inp, order = env
    _balance(monkeypatch, [{'ticker': '078930', 'qty': 13, 'sellable': 13}])
    ledger = _ledger(monkeypatch)

    k._reconcile_sell('tok', 'holding-page', inp, order)

    ledger.assert_not_called()
    n.close_auto_holding.assert_not_called()
    n.update_order_record.assert_not_called()
    assert '이전 거래일 매도 미완료' in k._notify_warning_throttled.call_args[0][1]


def test_holds_when_ref_price_missing(env, monkeypatch):
    """주문 참고가가 없으면 청산가를 지어내지 않고 사람 확인으로 넘긴다."""
    inp, order = env
    order['price'] = None
    balance = _balance(monkeypatch, [])
    ledger = _ledger(monkeypatch)

    k._reconcile_sell('tok', 'holding-page', inp, order)

    balance.assert_not_called(), "가격이 없으면 잔고 조회까지 갈 필요도 없다"
    ledger.assert_not_called()
    n.close_auto_holding.assert_not_called()
    assert '추정 불가' in k._notify_warning_throttled.call_args[0][1]


def test_balance_lookup_failure_does_not_settle(env, monkeypatch):
    """잔고 조회가 실패하면(타임아웃 등) 근거가 없으므로 확정하지 않는다."""
    inp, order = env
    monkeypatch.setattr(k, 'get_account_balance',
                        Mock(side_effect=TimeoutError('read timeout')))
    ledger = _ledger(monkeypatch)

    k._reconcile_sell('tok', 'holding-page', inp, order)

    ledger.assert_not_called()
    n.close_auto_holding.assert_not_called()
    k._notify_warning_throttled.assert_called_once()


# ── 당일 주문에는 쓰지 않는다 ───────────────────────────────

def test_same_day_order_never_settles_by_balance(env, monkeypatch):
    """장중에는 체결 처리가 진행 중일 수 있어 잔고만으로 단정하면 안 된다."""
    inp, order = env
    monkeypatch.setattr(k, '_today', lambda: date(2026, 9, 17))  # 주문 당일
    balance = _balance(monkeypatch, [])
    ledger = _ledger(monkeypatch)

    k._reconcile_sell('tok', 'holding-page', inp, order)

    balance.assert_not_called()
    ledger.assert_not_called()
    n.close_auto_holding.assert_not_called()
    k._notify_warning_throttled.assert_not_called()


# ── 매매일지 메모·재조회 억제 ───────────────────────────────

def test_ledger_memo_marks_estimate_and_skips_requery(env, monkeypatch):
    """추정 확정분은 메모에 그렇게 남기고, 안 되는 체결 조회를 또 부르지 않는다."""
    inp, order = env
    _balance(monkeypatch, [])
    monkeypatch.setattr(n, 'has_ledger_for_holding', Mock(return_value=False))
    monkeypatch.setattr(n, 'fetch_holding_buy_date',
                        Mock(return_value=date(2026, 8, 20)))
    monkeypatch.setattr(n, 'fetch_holding_avg_price', Mock(return_value=None))
    create = Mock(return_value='ledger-page')
    monkeypatch.setattr(n, 'create_ledger_record', create)

    k._reconcile_sell('tok', 'holding-page', inp, order)

    kwargs = create.call_args.kwargs
    assert kwargs['exit_price'] == float(REF_PRICE)
    assert kwargs['exit_date'] == date(2026, 9, 17)
    assert kwargs['shares'] == 13
    assert kwargs['exit_reason'] == n.LEDGER_EXIT_TREND
    assert '추정치' in kwargs['memo'] and ORDER_NO in kwargs['memo']
    # 체결 조회는 _reconcile_sell에서 한 번만 - 기록 단계에서 또 부르지 않는다.
    assert k.get_order_execution.call_count == 1
