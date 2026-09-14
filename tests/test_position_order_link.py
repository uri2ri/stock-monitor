from datetime import date, datetime
from unittest.mock import Mock
import pytest
import kis_client as k
import notion_repo as n
from tests.test_sell_confirmation import env, REAL_SELL

def test_closed_position_recovers_without_open_holdings(env, monkeypatch):
    inp, order = env
    n.holding_identity.return_value = {'ticker': inp.ticker, 'status': '청산'}
    k.get_order_execution.return_value = {'filled_qty': 100, 'avg_price': 8870}
    monkeypatch.setattr(n, 'ledger_matches_sell', Mock(return_value=True))
    k.run_auto_sell([])
    n.update_order_record.assert_called_once()
    n.close_auto_holding.assert_not_called()
    k._record_ledger_after_sell.assert_not_called()

def test_closed_position_needs_order_evidence(env, monkeypatch):
    inp, order = env
    n.holding_identity.return_value = {'ticker': inp.ticker, 'status': '청산'}
    k.get_order_execution.return_value = {'filled_qty': 100, 'avg_price': 8870}
    monkeypatch.setattr(n, 'ledger_matches_sell', Mock(return_value=False))
    k.run_auto_sell([])
    n.update_order_record.assert_not_called()

def test_same_day_old_position_does_not_block_new_stop(env, monkeypatch):
    inp, order = env
    n.holding_identity.return_value = {'ticker': inp.ticker, 'status': '청산'}
    k.get_order_execution.return_value = {'filled_qty': 100, 'avg_price': 8870}
    monkeypatch.setattr(n, 'ledger_matches_sell', Mock(return_value=True))
    k.get_account_balance.return_value = {'holdings': [{'ticker': inp.ticker, 'qty': 100, 'sellable': 100}]}
    monkeypatch.setattr(k, 'get_current_price', lambda *a: 8800)
    k.place_market_sell_order.return_value = {'status': 'blocked'}
    k.run_auto_sell([('new-position', inp)])
    assert k.place_market_sell_order.call_args.kwargs['holding_page_id'] == 'new-position'

@pytest.mark.parametrize('value', [None, 0, -1, 1.5, float('nan'), float('inf'), True])
def test_invalid_sell_quantity_blocks_before_send(env, monkeypatch, value):
    send = Mock()
    monkeypatch.setattr(k, '_send_market_sell', send)
    result = REAL_SELL('dummy', '000001', value, holding_page_id='holding')
    assert result['status'] == 'blocked'
    send.assert_not_called()

def test_relation_required_before_send(env, monkeypatch):
    send = Mock()
    monkeypatch.setattr(k, '_send_market_sell', send)
    assert REAL_SELL('dummy', '000001', 100)['status'] == 'blocked'
    send.assert_not_called()

def test_legacy_order_is_not_guessed_from_date(env):
    inp, order = env
    order['holding_page_id'] = None
    order['order_day'] = '2020-01-01'
    k.run_auto_sell([('holding', inp)])
    k.place_market_sell_order.assert_not_called()
    n.update_order_record.assert_not_called()
    k.get_order_execution.assert_not_called()

def test_relation_persisted_in_preorder(monkeypatch):
    ident = '11111111-1111-4111-8111-111111111111'
    monkeypatch.setenv('NOTION_ORDERS_DB_ID', 'orders')
    monkeypatch.setattr(n, '_headers', lambda: {})
    monkeypatch.setattr(n, 'holding_identity', lambda _: {'ticker': '000001', 'status': '보유'})
    response = Mock()
    response.json.return_value = {'id': 'order'}
    post = Mock(return_value=response)
    monkeypatch.setattr(n.requests, 'post', post)
    n.create_order_record(name='test', ticker='000001', order_no='', qty=100,
        price=8900, status='주문중', reason='손절', account_type=k.ACCOUNT_TYPE,
        when=datetime.now(), side=n.SIDE_SELL, holding_page_id=ident)
    assert post.call_args.kwargs['json']['properties']['보유종목']['relation'] == [{'id': ident}]

def test_pending_sell_without_open_holding_blocks_new_entry(monkeypatch):
    from tests.test_new_buy_skips_already_held import _setup, _candidate
    _setup(monkeypatch, find_page=lambda _: None)
    monkeypatch.setattr(n, 'fetch_unconfirmed_sell_orders', lambda *a: [{'ticker': '000001'}])
    candidate = _candidate('000001')
    assert k.select_buy_candidates('dummy', [candidate]) == []
