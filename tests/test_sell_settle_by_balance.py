"""잔고 관측과 주문별 체결 증빙을 혼동하지 않는다."""
import pytest
import kis_client as k
import notion_repo as n
from tests.test_sell_confirmation import env

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
