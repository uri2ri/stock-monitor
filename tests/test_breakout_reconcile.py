from copy import deepcopy
from datetime import date, datetime, timedelta
from unittest.mock import Mock
import pytest
import breakout_tracker as b
import breakout_reconcile as r
import kis_client as k

NOW = datetime(2026, 9, 19, 10, tzinfo=b.KST)


def pending():
    return dict(key='episode-1', event_id='observation-1', ticker='000390',
                status=b.STATUS_ACTIVE, outcome=b.OUTCOME_ORDER_SENT,
                first_detected_at=NOW - timedelta(days=1),
                order_day=date(2026, 9, 18), order_no='0000001234',
                order_side='매수', account_type='모의', account_key='bound-account', order_qty=10)


class Store:
    def __init__(self, row=None):
        self.row = row or pending()
    def list_tracks(self, **kwargs):
        return [deepcopy(self.row)]
    def update(self, key, fields):
        assert key == self.row['key']
        self.row.update(fields)


def test_later_round_confirms_previously_missing_order():
    store = Store()
    query = Mock(return_value={'state': 'not_found'})
    r.reconcile(store, query, 'bound-account', NOW)
    assert store.row['status'] == b.STATUS_ACTIVE
    assert '주문미발견' in store.row['execution_state']
    query.return_value = {'state': 'full', 'filled_qty': 10, 'avg_price': 12300}
    assert r.reconcile(store, query, 'bound-account', NOW + timedelta(minutes=10))['closed'] == 1
    assert store.row['outcome'] == b.OUTCOME_FILLED


def test_partial_is_not_unentered_or_order_complete():
    store = Store()
    query = Mock(return_value={'state': 'partial', 'filled_qty': 3, 'avg_price': 12300})
    r.reconcile(store, query, 'bound-account', NOW)
    assert store.row['status'] == b.STATUS_ACTIVE
    assert store.row['outcome'] == b.OUTCOME_PARTIAL
    assert store.row['filled_qty'] == 3 and store.row['unverified_qty'] == 7
    assert not b.is_confirmed_no_entry(store.row)
    query.side_effect = TimeoutError('offline')
    r.reconcile(store, query, 'bound-account', NOW)
    assert store.row['filled_qty'] == 3
    assert store.row['outcome'] == b.OUTCOME_PARTIAL
    assert '조회실패' in store.row['execution_state']


@pytest.mark.parametrize('field,value', [('order_day', date(2026, 9, 17)),
    ('account_key', 'other'), ('account_type', '실전'), ('order_side', '매도'),
    ('order_qty', 0), ('event_id', None)])
def test_old_or_mismatched_order_cannot_close_new_episode(field, value):
    row = pending()
    row[field] = value
    store = Store(row)
    query = Mock()
    r.reconcile(store, query, 'bound-account', NOW)
    query.assert_not_called()
    assert store.row['status'] == b.STATUS_ACTIVE


def test_save_failure_retries_on_next_round():
    store = Store()
    query = Mock(return_value={'state': 'full', 'filled_qty': 10, 'avg_price': 100})
    original = store.update
    store.update = Mock(side_effect=OSError('write failed'))
    assert r.reconcile(store, query, 'bound-account', NOW)['closed'] == 0
    store.update = original
    assert r.reconcile(store, query, 'bound-account', NOW)['closed'] == 1


def test_unfilled_is_distinct_from_missing_and_no_cancel_inference():
    store = Store()
    r.reconcile(store, lambda _: {'state': 'unfilled', 'filled_qty': 0, 'avg_price': 0}, 'bound-account', NOW)
    assert '체결수량0확인' in store.row['execution_state']
    assert store.row['status'] == b.STATUS_ACTIVE


@pytest.mark.parametrize('field,value', [('pdno', '000001'), ('sll_buy_dvsn_cd', '01'),
    ('ord_dt', '20260917'), ('ord_qty', '11'), ('tot_ccld_qty', 'nan'), ('avg_prvs', '0')])
def test_execution_row_identity_is_verified(field, value):
    row = dict(pdno='000390', sll_buy_dvsn_cd='02', ord_dt='20260918', ord_qty='10',
               tot_ccld_qty='10', avg_prvs='12300')
    row[field] = value
    with pytest.raises(ValueError):
        k._tracking_execution_row(row, pending(), '20260918')


def test_immediate_partial_does_not_close_memory_track():
    previous = b.set_store(b.MemoryStore())
    try:
        b.get_store().rows['episode-1'] = pending()
        b.record_fill('000390', 3, 100, NOW, order_no='0000001234', order_day=date(2026, 9, 18))
        row = b.get_store().rows['episode-1']
        assert row['status'] == b.STATUS_ACTIVE and row['outcome'] == b.OUTCOME_PARTIAL
    finally:
        b.set_store(previous)


@pytest.mark.parametrize('qty,state', [('0', 'unfilled'), ('3', 'partial'), ('10', 'full')])
def test_real_query_parser_verifies_identity_without_network(monkeypatch, qty, state):
    monkeypatch.setenv('KIS_ACCOUNT', '12345678-01')
    monkeypatch.setenv('KIS_APP_KEY', 'test-key')
    monkeypatch.setenv('KIS_APP_SECRET', 'test-secret')
    expected = pending()
    expected['account_key'] = k.tracking_account_key()
    row = dict(odno='1234', pdno='000390', sll_buy_dvsn_cd='02', ord_dt='20260918',
               ord_qty='10', tot_ccld_qty=qty, avg_prvs='12300' if qty != '0' else '0')
    response = Mock(headers={})
    response.json.return_value = {'rt_cd': '0', 'output1': [row]}
    get = Mock(return_value=response)
    monkeypatch.setattr(k.requests, 'get', get)
    result = k.get_order_execution('dummy', expected['order_no'], order_day=expected['order_day'], tracking_identity=expected)
    assert result['state'] == state
    assert get.call_args.kwargs['params']['INQR_STRT_DT'] == '20260918'
    assert get.call_args.kwargs['params']['INQR_END_DT'] == '20260918'
    row['ord_dt'] = '20260917'
    with pytest.raises(ValueError):
        k.get_order_execution('dummy', expected['order_no'], order_day=expected['order_day'], tracking_identity=expected)


def test_followup_is_bounded_and_never_places_orders(monkeypatch):
    rows = [dict(pending(), key=f'episode-{i}') for i in range(8)]
    store = Mock()
    store.list_tracks.return_value = rows
    query = Mock(return_value={'state': 'not_found'})
    result = r.reconcile(store, query, 'bound-account', NOW)
    assert result['checked'] == query.call_count == 5


def test_worker_runs_followup_even_without_new_events(monkeypatch):
    from tools import breakout_worker as worker
    import sys
    monkeypatch.setattr(sys, 'argv', ['worker', 'deliver'])
    monkeypatch.setenv('GITHUB_RUN_ID', '1')
    monkeypatch.setenv('GITHUB_RUN_ATTEMPT', '1')
    monkeypatch.setattr(worker.tracker, 'is_enabled', lambda: True)
    run = Mock()
    monkeypatch.setattr(r, 'run', run)
    assert worker.main() == 0
    run.assert_called_once()
