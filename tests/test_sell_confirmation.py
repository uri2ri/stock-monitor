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


@pytest.mark.parametrize('data, continuation', [
    ({'rt_cd': '1', 'msg1': '조회 오류'}, ''),
    ({'rt_cd': '0', 'output1': []}, 'M'),
])
def test_execution_query_error_raises_instead_of_silent_unfilled(monkeypatch, data, continuation):
    """조회 자체가 실패한 응답을 "미체결"로 조용히 해석하면 당일 내내
    알림 없이 재시도만 하다가 청산 기회를 놓칠 수 있다 - 예외로 올려
    호출자(_reconcile_sell)가 즉시 경고하게 한다."""
    for key in ('KIS_APP_KEY', 'KIS_APP_SECRET'):
        monkeypatch.setenv(key, 'dummy')
    monkeypatch.setenv('KIS_ACCOUNT', '12345678-01')
    response = Mock()
    response.json.return_value = data
    response.headers = {'tr_cont': continuation}
    monkeypatch.setattr(k.requests, 'get', Mock(return_value=response))
    with pytest.raises(RuntimeError):
        k.get_order_execution('dummy', '123', order_day=date(2026, 9, 11))


def test_execution_query_error_surfaces_same_day_notification(env):
    """당일 조회 오류는(이전 거래일 검사를 기다리지 않고) 이번 회차에
    바로 경고해야 한다."""
    inp, _ = env
    k.get_order_execution.side_effect = RuntimeError('체결 조회 응답 오류')
    k.run_auto_sell([('holding', inp)])
    n.close_auto_holding.assert_not_called()
    k._notify_warning_throttled.assert_called()


def test_stale_pending_sell_does_not_block_reentered_position(env, monkeypatch):
    """완전청산(다만 주문 상태 갱신만 실패) 후 같은 종목을 재진입하면, 점검표엔
    새 page_id의 새 행이 생긴다. 주문 DB는 종목코드로만 매칭되므로 과거
    매도 기록이 새 포지션의 매수일보다 앞서면(재진입 이전 주문이면)
    새 포지션의 매도 판정을 막지 않아야 한다."""
    inp, order = env
    order['order_day'] = '2026-09-01'   # 재진입 이전(과거 포지션)의 매도 주문
    monkeypatch.setattr(n, 'fetch_holding_buy_date', Mock(return_value=date(2026, 9, 10)))
    k.run_auto_sell([('new_holding_page', inp)])
    # 과거 주문의 체결 여부는 다시 조회하지 않고(=엮이지 않고), 대신
    # sellable=0(계좌 잔고 없음, env 기본값)이라 그냥 이번 회차는 넘어간다.
    k.get_order_execution.assert_not_called()
    n.close_auto_holding.assert_not_called()
    n.update_order_record.assert_not_called()
    stale_msgs = [c.args[1] for c in k._notify_warning_throttled.call_args_list
                  if '재진입 이전' in c.args[1]]
    assert stale_msgs, "재진입 이전 매도 기록에 대한 별도 알림이 있어야 한다"


def test_same_day_or_later_pending_sell_still_blocks_reentered_position(env, monkeypatch):
    """매수일과 같은 날 이상인 미확인 매도는(재진입 이전인지 확실치 않으므로)
    여전히 안전한 쪽으로 현재 포지션의 주문으로 취급해 막아야 한다."""
    inp, order = env
    order['order_day'] = '2026-09-11'
    monkeypatch.setattr(n, 'fetch_holding_buy_date', Mock(return_value=date(2026, 9, 11)))
    k.get_order_execution.return_value = {'filled_qty': 100, 'avg_price': 8870}
    k.run_auto_sell([('new_holding_page', inp)])
    n.close_auto_holding.assert_called_once_with('new_holding_page')


def test_no_pending_sell_skips_entry_date_lookup(env, monkeypatch):
    """미확인 매도가 없는 종목까지 매 회차 매수일을 조회하면, 절대다수인
    "미확인 매도 없음" 종목에 불필요한 노션 조회가 매번 추가된다."""
    inp, _ = env
    n.fetch_unconfirmed_sell_orders.return_value = []
    fetch_buy_date = Mock(return_value=date(2026, 9, 1))
    monkeypatch.setattr(n, 'fetch_holding_buy_date', fetch_buy_date)
    k.run_auto_sell([('holding', inp)])
    fetch_buy_date.assert_not_called()


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
