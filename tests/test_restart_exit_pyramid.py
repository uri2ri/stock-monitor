from datetime import date
from unittest.mock import Mock
import pytest
import core
import kis_client as k
import notion_repo as n

@pytest.fixture
def setup(monkeypatch):
    for name, value in [('_auto_trade_configured', True), ('_auto_trade_paused', False),
                        ('_within_trading_hours', True), ('_is_trading_day', True),
                        ('get_access_token', 'dummy')]:
        monkeypatch.setattr(k, name, Mock(return_value=value))
    monkeypatch.setattr(k, 'MAX_ORDERS_PER_DAY', 0)
    monkeypatch.setattr(k, '_today', lambda: date(2026, 9, 11))
    monkeypatch.setattr(k, '_notify_failure', Mock())
    monkeypatch.setattr(k, '_notify_warning_throttled', Mock())
    monkeypatch.setattr(k, '_record_holding_after_buy', Mock())
    monkeypatch.setattr(k, '_record_ledger_after_sell', Mock())
    monkeypatch.setattr(n, 'close_auto_holding', Mock())
    monkeypatch.setattr(k, 'get_mock_account_corr_units', lambda *a: {'groups': {}, 'total_units': 1})
    monkeypatch.setattr(k, '_get_sector_map', lambda: {})
    monkeypatch.setattr(k, 'get_account_balance', lambda *a: {
        'account_size': 10000000, 'available_cash': 5000000,
        'holdings': [{'ticker': '000001', 'qty': 100, 'sellable': 80}]})
    monkeypatch.setattr(k, 'place_market_sell_order', Mock(return_value={'status': 'sent'}))
    monkeypatch.setattr(k, 'place_market_buy_order', Mock(return_value={'status': 'sent'}))
    return core.HoldingInput('000001', 'test', 'KOSPI', 10000, 100,
                             entry_atr=500, prev_stop_loss=9000)

@pytest.mark.parametrize('price, verdict, expected', [(8900, '', True), (10000, '', False),
                                                     (10000, '추세청산', True)])
def test_exit_with_new_buy_cap_zero(monkeypatch, setup, price, verdict, expected):
    setup.recent_verdict = verdict
    setup.checked_date = date(2026, 9, 11)
    monkeypatch.setattr(k, 'get_current_price', lambda *a: price)
    k.run_auto_sell([('page', setup)])
    assert k.place_market_sell_order.called == expected
    if expected:
        assert k.place_market_sell_order.call_args.args[2] == 80

@pytest.mark.parametrize('price, expected', [(10200, False), (10250, True)])
def test_pyramid_with_new_buy_cap_zero(monkeypatch, setup, price, expected):
    monkeypatch.setattr(k, 'get_price_quote', lambda *a: {'price': price, 'market_warned': False})
    k.run_auto_pyramid([setup])
    assert k.place_market_buy_order.called == expected
    if expected:
        assert k.place_market_buy_order.call_args.kwargs['order_type'] == n.ORDER_ADD

def test_no_sell_when_sellable_zero(monkeypatch, setup):
    monkeypatch.setattr(k, 'get_account_balance', lambda *a: {'holdings': [{'ticker': '000001', 'sellable': 0}]})
    monkeypatch.setattr(k, 'get_current_price', Mock(side_effect=AssertionError('No quote needed')))
    k.run_auto_sell([('page', setup)])
    k.place_market_sell_order.assert_not_called()
