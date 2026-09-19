from copy import deepcopy
from datetime import datetime, timedelta
import importlib
import subprocess
import sys
from unittest.mock import Mock
import pytest
import breakout_outbox as j
import breakout_tracker as b

AT = datetime(2026, 9, 18, 10, tzinfo=b.KST)


class Backend:
    def __init__(self):
        self.rows = {}
        self.calls = []
        self.fail = set()
        self.lose_response = False

    def list_tracks(self, **kwargs):
        return [deepcopy(v) for v in self.rows.values()]

    def find_event(self, event_id):
        return next((v for v in self.rows.values() if v['event_id'] == event_id), None)

    def create(self, row):
        self.calls.append(row['ticker'])
        if row['ticker'] in self.fail:
            raise TimeoutError('unknown POST result')
        key = 'page-' + row['event_id']
        self.rows[key] = dict(row, key=key)
        if self.lose_response:
            raise TimeoutError('saved, response lost')
        return key

    def update(self, key, fields):
        if fields['ticker'] in self.fail:
            raise TimeoutError('patch failed')
        self.rows[key].update(fields)


def observe(ticker='000390', now=AT, price=105):
    return j.append('record_breakout', [dict(ticker=ticker, name='dummy', high20=100,
                                            price=price, atr20=5), now], {})


def test_read_failure_retry_preserves_earliest(monkeypatch):
    backend = Backend()
    observe()
    with monkeypatch.context() as m:
        m.setattr(backend, 'list_tracks', Mock(side_effect=TimeoutError('read')))
        with pytest.raises(TimeoutError):
            j.prepare(backend, set(), '1')
    observe(now=AT + timedelta(days=1), price=130)
    j.prepare(backend, set(), '2')
    j.deliver(backend, '2')
    row = next(iter(backend.rows.values()))
    assert (row['first_detected_at'], row['first_price'], row['first_atr']) == (AT, 105, 5)
    assert not j.load()['events']


def test_restart_restores_journal():
    ident = observe()
    importlib.reload(j)
    assert j.load()['events'][0]['id'] == ident
    assert j.load()['events'][0]['args'][1] == AT
    output = subprocess.check_output([sys.executable, '-c',
        "import breakout_outbox as j; e=j.load()['events'][0]; print(e['id']); print(e['args'][1].isoformat())"], text=True)
    assert ident in output and AT.isoformat() in output


def test_saved_response_lost_never_creates_twice():
    backend = Backend()
    backend.lose_response = True
    observe()
    j.prepare(backend, set(), '1')
    checkpoint = deepcopy(j.load())
    assert j.deliver(backend, '1') == 1
    # Simulate final git push failure: next runner restores pre-delivery checkpoint.
    j.save(checkpoint)
    j.prepare(backend, set(), '2')
    assert j.deliver(backend, '2') == 0
    assert len(backend.calls) == len(backend.rows) == 1
    assert not j.load()['events']


def test_ambiguous_not_found_is_not_resent():
    backend = Backend()
    backend.fail.add('000390')
    observe()
    j.prepare(backend, set(), '1')
    j.deliver(backend, '1')
    backend.fail.clear()
    j.deliver(backend, '2')
    assert len(backend.calls) == 1
    assert j.load()['plans'][0]['state'] == 'ambiguous'
    assert len(j.load()['events']) == 1


def test_partial_success_keeps_only_failed_events():
    backend = Backend()
    good, bad = observe('000001'), observe('000002')
    backend.fail.add('000002')
    j.prepare(backend, set(), '1')
    j.deliver(backend, '1')
    assert [e['id'] for e in j.load()['events']] == [bad]
    assert backend.calls == ['000001', '000002']


def test_durable_append_failure_is_explicit_and_does_not_raise(monkeypatch, caplog):
    backend = b.NotionStore()
    monkeypatch.setattr(backend, 'configured', lambda: True)
    previous = b.set_store(backend)
    monkeypatch.setattr(j, 'save', Mock(side_effect=OSError('disk full')))
    try:
        b.record_breakout(dict(ticker='000390', name='dummy', high20=100, price=105, atr20=5), AT)
        assert '이벤트 유실 가능' in caplog.text
    finally:
        b.set_store(previous)


def test_corrupt_journal_is_not_overwritten():
    # Use a valid JSON object of an unsupported version without touching production.
    j.save({'version': 999, 'events': [], 'plans': []})
    before = j.path().read_bytes()
    with pytest.raises(ValueError):
        observe()
    assert j.path().read_bytes() == before


def test_outcome_without_observation_is_retained():
    ident = j.append('_set_outcome', ['000390', b.OUTCOME_ORDER_SENT, 'sent', AT],
                     {'order_no': '123', 'order_day': AT.date()})
    backend = Backend()
    j.prepare(backend, set(), '1')
    assert [e['id'] for e in j.load()['events']] == [ident]
    assert not backend.calls


def restart_process():
    subprocess.check_call([sys.executable, '-c',
        "import breakout_outbox as j; j.save(j.load())"])
    importlib.reload(j)


def test_prepare_rotates_past_100_pending_events_after_restart():
    backend = Backend()
    pending = [j.append('_set_outcome', [f'{i:06}', b.OUTCOME_ORDER_SENT, 'sent', AT], {})
               for i in range(100)]
    good = observe('999999')
    original = deepcopy(j.load()['events'])
    j.prepare(backend, set(), '1')
    assert not j.load()['plans']
    restart_process()
    j.prepare(backend, set(), '2')
    assert j.load()['plans'][0]['events'] == [good]
    j.deliver(backend, '2')
    assert [e['id'] for e in j.load()['events']] == pending
    assert j.load()['events'] == original[:100]
    assert backend.calls == ['999999']


def test_deliver_rotates_past_20_ambiguous_plans_after_restart():
    backend = Backend()
    for i in range(21):
        observe(f'{i:06}')
    j.prepare(backend, set(), 'owner')
    data = j.load()
    for plan in data['plans'][:20]:
        plan['state'] = 'ambiguous'
    j.save(data)
    original = deepcopy(data['plans'][:20])
    j.deliver(backend, 'owner')
    assert not backend.calls
    restart_process()
    j.deliver(backend, 'owner')
    assert backend.calls == ['000020']
    assert j.load()['plans'] == original
    restart_process()
    j.deliver(backend, 'next-owner')
    assert backend.calls == ['000020']


def test_rotated_followup_cannot_overtake_first_observation():
    backend = Backend()
    first = observe('000001')
    followup = observe('000001', now=AT + timedelta(days=1), price=130)
    data = j.load()
    data['prepare_cursor'] = first
    j.save(data)
    # Starts at the followup, but must preserve the earliest price/date.
    j.prepare(backend, {'000001'}, '1')
    assert not j.load()['plans']
    assert [e['id'] for e in j.load()['events']] == [first, followup]
    restart_process()
    j.prepare(backend, set(), '2')
    j.deliver(backend, '2')
    row = next(iter(backend.rows.values()))
    assert (row['first_detected_at'], row['first_price']) == (AT, 105)
    j.prepare(backend, set(), '3')
    j.deliver(backend, '3')
    assert not j.load()['events']
    assert backend.calls == ['000001']


def test_rotated_plan_cannot_overtake_pending_same_ticker():
    backend = Backend()
    observe()
    j.prepare(backend, set(), 'owner')
    data = j.load()
    first = data['plans'][0]
    first['state'] = 'ambiguous'
    later = deepcopy(first)
    later.update(id='later', state='prepared')
    later['record']['event_id'] = 'later-event'
    data['plans'].append(later)
    data['deliver_cursor'] = first['id']
    j.save(data)
    restart_process()
    j.deliver(backend, 'owner')
    assert not backend.calls
    assert len(j.load()['plans']) == 2
