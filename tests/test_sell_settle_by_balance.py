"""잔고 관측과 주문별 체결 증빙을 혼동하지 않는다."""
import pytest
import kis_client as k
import notion_repo as n
from unittest.mock import Mock
from tests.test_sell_confirmation import env
REAL_LEDGER = k._record_ledger_after_sell

@pytest.mark.parametrize('fill', [None, {'filled_qty': 5, 'avg_price': 112000}])
def test_empty_balance_never_proves_order_fill(env, fill):
    inp, order = env
    order['order_day'] = '2020-01-01'
    k.get_order_execution.return_value = fill
    k.get_account_balance.return_value = {'holdings': []}
    k._reconcile_sell('token', 'holding', inp, order)
    k._record_ledger_after_sell.assert_not_called()
    n.close_auto_holding.assert_not_called()
    n.update_order_record.assert_not_called()
    assert '잔고 없음 확인' in k._notify_warning_throttled.call_args[0][1]

def test_evidence_arrives_after_empty_balance(env):
    inp, order = env
    order['order_day'] = '2020-01-01'
    k.get_order_execution.return_value = None
    k.get_account_balance.return_value = {'holdings': []}
    k._reconcile_sell('token', 'holding', inp, order)
    k.get_order_execution.return_value = {'filled_qty': order['qty'], 'avg_price': 8870}
    k._reconcile_sell('token', 'holding', inp, order)
    k._record_ledger_after_sell.assert_called_once()
    n.close_auto_holding.assert_called_once()
    n.update_order_record.assert_called_once()

def test_balance_error_keeps_unconfirmed(env):
    inp, order = env
    order['order_day'] = '2020-01-01'
    k.get_order_execution.return_value = None
    k.get_account_balance.side_effect = TimeoutError('offline')
    k._reconcile_sell('token', 'holding', inp, order)
    k._record_ledger_after_sell.assert_not_called()
    n.update_order_record.assert_not_called()

@pytest.mark.parametrize('failure', ['ledger', 'close', 'order'])
def test_partial_accounting_failure_recovers_without_duplicate(env, monkeypatch, failure):
    inp, order = env
    journal = []
    state = {'closed': False, 'failed': False}
    k.get_order_execution.return_value = {'filled_qty': 100, 'avg_price': 8870}
    monkeypatch.setattr(k, '_record_ledger_after_sell', REAL_LEDGER)
    monkeypatch.setattr(n, 'fetch_holding_buy_date', lambda _: None)
    monkeypatch.setattr(n, 'fetch_holding_avg_price', lambda _: None)
    monkeypatch.setattr(n, 'has_ledger_for_holding', lambda _: bool(journal))
    monkeypatch.setattr(n, 'ledger_matches_sell', lambda *a: bool(journal))
    def step(name):
        if failure == name and not state['failed']:
            state['failed'] = True
            raise RuntimeError('simulated ' + name)
    def create(**kwargs):
        step('ledger')
        journal.append(kwargs)
    def close(_):
        step('close')
        state['closed'] = True
    def update(*a, **kw):
        step('order')
    monkeypatch.setattr(n, 'create_ledger_record', create)
    monkeypatch.setattr(n, 'close_auto_holding', close)
    monkeypatch.setattr(n, 'update_order_record', Mock(side_effect=update))
    monkeypatch.setattr(n, 'holding_identity', lambda _: {'ticker': inp.ticker, 'status': '청산' if state['closed'] else '보유'})
    k._reconcile_sell('token', 'holding', inp, order)
    if state['closed']:
        assert k._recover_closed_sell('token', order)
    else:
        k._reconcile_sell('token', 'holding', inp, order)
    assert len(journal) == 1
    assert journal[0]['exit_price'] == 8870
    assert state['closed']
    assert n.update_order_record.call_args.kwargs['status'] == '성공'

def test_ledger_never_falls_back_to_reference_price(env, monkeypatch):
    inp, order = env
    create = Mock()
    monkeypatch.setattr(n, 'create_ledger_record', create)
    kwargs = dict(qty=100, ref_price=99999, reason='손절', order_no='123', today=k._today())
    assert not REAL_LEDGER('token', 'holding', inp, **kwargs)
    assert not REAL_LEDGER('token', 'holding', inp, **kwargs, estimated_price=True)
    create.assert_not_called()
