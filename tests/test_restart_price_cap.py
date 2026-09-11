from unittest.mock import Mock
import pytest
import kis_client as k
import notion_repo as n

@pytest.mark.parametrize('fresh, allowed', [(150000, True), (150100, False)])
def test_latest_quote_price_cap(monkeypatch, fresh, allowed):
    monkeypatch.setattr(k, 'MAX_ORDERS_PER_DAY', 3)
    monkeypatch.setattr(k, 'get_account_balance', lambda _: {
        'account_size': 10000000, 'available_cash': 10000000, 'holdings': []})
    monkeypatch.setattr(k, 'get_mock_account_corr_units', lambda *a: {'groups': {}, 'total_units': 0})
    monkeypatch.setattr(n, 'count_success_orders_today', lambda *a: 0)
    monkeypatch.setattr(k, '_notify_failure', Mock())
    monkeypatch.setattr(k, 'get_price_quote', lambda *a: {'price': fresh, 'market_warned': False})
    candidate = dict(ticker='000001', name='test', price=150000,
                     high20=149900, atr20=5000, gap_atr=0.02, sector='test')
    assert bool(k.select_buy_candidates('dummy', [candidate])) == allowed
