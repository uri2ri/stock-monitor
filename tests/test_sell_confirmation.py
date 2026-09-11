from datetime import date
from unittest.mock import Mock
import pytest
import core
import kis_client as k
import notion_repo as n

@pytest.fixture
def env(monkeypatch):
    inp = core.HoldingInput('000001', 'test', 'KOSPI', 10000, 100, prev_stop_loss=9000)
    order = dict(page_id='order', ticker=inp.ticker, order_no='123', qty=100,
                 reason='손절 (장중)', order_day='2026-09-11')
    for name in ('_auto_trade_configured', '_within_trading_hours', '_is_trading_day'):
        monkeypatch.setattr(k, name, Mock(return_value=True))
    monkeypatch.setattr(k, '_today', lambda: date(2026, 9, 11))
    monkeypatch.setattr(k, 'get_access_token', Mock(return_value='dummy'))
    monkeypatch.setattr(k, '_notify_warning_throttled', Mock())
    monkeypatch.setattr(k, '_notify_failure', Mock())
    monkeypatch.setattr(k, 'get_order_execution', Mock(return_value=None))
    monkeypatch.setattr(k, '_record_ledger_after_sell', Mock(return_value=True))
    monkeypatch.setattr(k, 'get_account_balance', Mock(return_value={'holdings': []}))
    monkeypatch.setattr(k, 'place_market_sell_order', Mock())
    monkeypatch.setattr(n, 'fetch_unconfirmed_sell_orders', Mock(return_value=[order]))
    monkeypatch.setattr(n, 'close_auto_holding', Mock())
    monkeypatch.setattr(n, 'update_order_record', Mock())
    return inp, order

@pytest.mark.parametrize('fill', [None, {'filled_qty': 40, 'avg_price': 8900}])
def test_unfilled_or_partial_remains_open_without_reselling(env, fill):
    inp, _ = env
    k.get_order_execution.return_value = fill
    k.run_auto_sell([('holding', inp)])
    n.close_auto_holding.assert_not_called()
    k._record_ledger_after_sell.assert_not_called()
    k.place_market_sell_order.assert_not_called()

def test_next_run_full_fill_closes_with_confirmed_price(env):
    inp, _ = env
    k.run_auto_sell([('holding', inp)])
    n.close_auto_holding.assert_not_called()
    k.get_order_execution.return_value = {'filled_qty': 100, 'avg_price': 8870}
    k.run_auto_sell([('holding', inp)])
    n.close_auto_holding.assert_called_once_with('holding')
    assert k._record_ledger_after_sell.call_args.kwargs['ref_price'] == 8870
    assert n.update_order_record.call_args.kwargs['status'] == '성공'
    k.place_market_sell_order.assert_not_called()

@pytest.mark.parametrize('failure', ['execution', 'balance', 'ledger'])
def test_verification_failure_keeps_holding(env, failure):
    inp, _ = env
    k.get_order_execution.return_value = {'filled_qty': 100, 'avg_price': 8870}
    if failure == 'execution':
        k.get_order_execution.side_effect = RuntimeError('offline')
    elif failure == 'balance':
        k.get_account_balance.side_effect = RuntimeError('offline')
    else:
        k._record_ledger_after_sell.return_value = False
    k.run_auto_sell([('holding', inp)])
    n.close_auto_holding.assert_not_called()
    k.place_market_sell_order.assert_not_called()

def test_full_order_fill_but_residual_shares_stays_open(env):
    inp, _ = env
    k.get_order_execution.return_value = {'filled_qty': 100, 'avg_price': 8870}
    k.get_account_balance.return_value = {'holdings': [{'ticker': inp.ticker, 'qty': 20, 'sellable': 0}]}
    k.run_auto_sell([('holding', inp)])
    n.close_auto_holding.assert_not_called()

def test_previous_day_order_uses_original_date(env):
    inp, order = env
    order['order_day'] = '2026-09-10'
    k.run_auto_sell([('holding', inp)])
    assert k.get_order_execution.call_args.kwargs['order_day'] == date(2026, 9, 10)
    k._notify_warning_throttled.assert_called()

def test_unknown_order_number_does_not_resell(env):
    inp, order = env
    order['order_no'] = ''
    k.run_auto_sell([('holding', inp)])
    k.get_order_execution.assert_not_called()
    k.place_market_sell_order.assert_not_called()

def test_pending_query_failure_prevents_new_sell(env):
    inp, _ = env
    n.fetch_unconfirmed_sell_orders.side_effect = RuntimeError('offline')
    k.run_auto_sell([('holding', inp)])
    k.place_market_sell_order.assert_not_called()

def test_sell_acceptance_stays_pending(monkeypatch, env):
    monkeypatch.setattr(n, 'create_order_record', Mock(return_value='order'))
    monkeypatch.setattr(k, '_send_market_sell', Mock(return_value={'rt_cd': '0', 'output': {'ODNO': '123'}}))
    # Exercise the real sender despite the integration fixture's stub.
    # Retrieve the saved implementation captured at collection time.
    result = REAL_SELL('dummy', '000001', 100, reason='손절')
    assert result['status'] == 'sent'
    assert n.update_order_record.call_args.kwargs['status'] == '주문중'

REAL_SELL = k.place_market_sell_order


@pytest.mark.parametrize('data, continuation', [
    ({'rt_cd': '1'}, ''),
    ({'rt_cd': '0', 'output2': [{}]}, ''),
    ({'rt_cd': '0', 'output1': [], 'output2': [{}]}, 'M'),
])
def test_invalid_balance_cannot_confirm_empty(monkeypatch, data, continuation):
    for key in ('KIS_APP_KEY', 'KIS_APP_SECRET'):
        monkeypatch.setenv(key, 'dummy')
    monkeypatch.setenv('KIS_ACCOUNT', '12345678-01')
    response = Mock()
    response.json.return_value = data
    response.headers = {'tr_cont': continuation}
    monkeypatch.setattr(k.requests, 'get', Mock(return_value=response))
    with pytest.raises(RuntimeError):
        k.get_account_balance('dummy')


def test_existing_ledger_prevents_duplicate(monkeypatch):
    monkeypatch.setattr(k.time, 'sleep', Mock())
    monkeypatch.setattr(n, 'fetch_holding_buy_date', Mock(return_value=None))
    monkeypatch.setattr(n, 'fetch_holding_avg_price', Mock(return_value=10000))
    monkeypatch.setattr(n, 'has_ledger_for_holding', Mock(return_value=True))
    monkeypatch.setattr(n, 'create_ledger_record', Mock())
    inp = core.HoldingInput('000001', 'test', 'KOSPI', 10000, 100)
    assert k._record_ledger_after_sell('dummy', 'holding', inp, qty=100,
        ref_price=8900, reason='손절', order_no='123', today=date(2026, 9, 11),
        confirmed_fill={'filled_qty': 100, 'avg_price': 8900})
    n.create_ledger_record.assert_not_called()


def test_close_failure_keeps_order_pending(env):
    inp, _ = env
    k.get_order_execution.return_value = {'filled_qty': 100, 'avg_price': 8870}
    n.close_auto_holding.side_effect = RuntimeError('offline')
    k.run_auto_sell([('holding', inp)])
    n.update_order_record.assert_not_called()


def test_pending_sell_query_paginates_and_keeps_original_day(monkeypatch):
    monkeypatch.setenv('NOTION_ORDERS_DB_ID', 'dummy')
    monkeypatch.setattr(n, '_headers', lambda: {})
    def page(ident):
        return {'id': ident, 'properties': {
            '종목코드': {'rich_text': [{'plain_text': '000001'}]},
            '주문일시': {'date': {'start': '2026-09-10T10:00:00+09:00'}},
        }}
    responses = []
    for body in ({'results': [page('a')], 'has_more': True, 'next_cursor': 'next'},
                 {'results': [page('b')], 'has_more': False}):
        response = Mock()
        response.json.return_value = body
        responses.append(response)
    post = Mock(side_effect=responses)
    monkeypatch.setattr(n.requests, 'post', post)
    rows = n.fetch_unconfirmed_sell_orders('모의')
    assert [r['page_id'] for r in rows] == ['a', 'b']
    assert rows[0]['order_day'] == '2026-09-10'
    assert post.call_args.kwargs['json']['start_cursor'] == 'next'
