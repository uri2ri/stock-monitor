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


def deliver(backend, owner):
    # Model the remote checkpoint independently of the local journal.
    def checkpoint():
        backend.checkpoint = deepcopy(j.load())
    return j.deliver(backend, owner, checkpoint=checkpoint)


def test_read_failure_retry_preserves_earliest(monkeypatch):
    backend = Backend()
    observe()
    with monkeypatch.context() as m:
        m.setattr(backend, 'list_tracks', Mock(side_effect=TimeoutError('read')))
        with pytest.raises(TimeoutError):
            j.prepare(backend, set(), '1')
    observe(now=AT + timedelta(days=1), price=130)
    j.prepare(backend, set(), '2')
    deliver(backend, '2')
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
    assert deliver(backend, '1') == 1
    # Simulate final git push failure: next runner restores pre-delivery checkpoint.
    j.save(backend.checkpoint)
    j.prepare(backend, set(), '2')
    assert deliver(backend, '2') == 0
    assert len(backend.calls) == len(backend.rows) == 1
    assert not j.load()['events']


def test_ambiguous_not_found_is_not_resent():
    backend = Backend()
    backend.fail.add('000390')
    observe()
    j.prepare(backend, set(), '1')
    deliver(backend, '1')
    backend.fail.clear()
    deliver(backend, '2')
    assert len(backend.calls) == 1
    assert j.load()['plans'][0]['state'] == 'ambiguous'
    assert len(j.load()['events']) == 1


def test_partial_success_keeps_only_failed_events():
    backend = Backend()
    good, bad = observe('000001'), observe('000002')
    backend.fail.add('000002')
    j.prepare(backend, set(), '1')
    deliver(backend, '1')
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
    deliver(backend, '2')
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
    deliver(backend, 'owner')
    assert not backend.calls
    restart_process()
    deliver(backend, 'next-owner')
    assert backend.calls == ['000020']
    assert j.load()['plans'] == original
    restart_process()
    deliver(backend, 'third-owner')
    assert backend.calls == ['000020']


def test_rotated_followup_cannot_overtake_first_observation(monkeypatch):
    backend = Backend()
    first = observe('000001')
    followup = observe('000001', now=AT + timedelta(days=1), price=130)
    data = j.load()
    data['prepare_cursor'] = first
    j.save(data)
    # Starts at the followup, but must preserve the earliest price/date.
    with monkeypatch.context() as m:
        m.setattr(b, 'record_breakout', Mock(side_effect=TimeoutError('replay')))
        j.prepare(backend, set(), '1')
    assert not j.load()['plans']
    assert [e['id'] for e in j.load()['events']] == [first, followup]
    restart_process()
    j.prepare(backend, set(), '2')
    deliver(backend, '2')
    row = next(iter(backend.rows.values()))
    assert (row['first_detected_at'], row['first_price']) == (AT, 105)
    j.prepare(backend, set(), '3')
    deliver(backend, '3')
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
    deliver(backend, 'owner')
    assert not backend.calls
    assert len(j.load()['plans']) == 2


def test_queued_plan_requires_remote_checkpoint():
    backend = Backend()
    observe()
    j.prepare(backend, set(), 'old')
    j.deliver(backend, 'new')
    assert not backend.calls
    assert j.load()['plans'][0]['state'] == 'queued'


@pytest.mark.parametrize('remote_saved', [False, True])
def test_checkpoint_failure_never_posts_and_remote_restart_is_safe(remote_saved):
    backend = Backend()
    observe()
    j.prepare(backend, set(), 'old')
    remote = deepcopy(j.load())

    def failing_checkpoint():
        nonlocal remote
        if remote_saved:
            remote = deepcopy(j.load())
        raise TimeoutError('push response lost')

    j.deliver(backend, 'new', checkpoint=failing_checkpoint)
    assert not backend.calls
    j.save(remote)
    restart_process()
    deliver(backend, 'third')
    assert len(backend.calls) == (0 if remote_saved else 1)


def test_lost_post_and_failed_final_push_never_resends_even_same_owner():
    backend = Backend()
    backend.fail.add('000390')
    observe()
    j.prepare(backend, set(), 'owner')
    deliver(backend, 'owner')
    assert backend.checkpoint['plans'][0]['state'] == 'creating'
    j.save(backend.checkpoint)
    restart_process()
    backend.fail.clear()
    deliver(backend, 'owner')
    deliver(backend, 'next-owner')
    assert backend.calls == ['000390']


def test_legacy_prepared_from_previous_owner_stays_ambiguous():
    backend = Backend()
    observe()
    j.prepare(backend, set(), 'old')
    data = j.load()
    data['plans'][0]['state'] = 'prepared'
    j.save(data)
    deliver(backend, 'new')
    assert not backend.calls
    assert j.load()['plans'][0]['state'] == 'ambiguous'


def test_worker_checkpoint_orders_commit_before_push(monkeypatch, tmp_path):
    from tools import breakout_worker as worker
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('BREAKOUT_OUTBOX_PATH', str(tmp_path / 'data/breakout_outbox.json'))
    monkeypatch.setenv('GITHUB_REF_NAME', 'test-branch')
    run = Mock()
    monkeypatch.setattr(worker.subprocess, 'run', run)
    worker.checkpoint_create()
    commands = [c.args[0] for c in run.call_args_list]
    assert [c[1] for c in commands] == ['add', 'commit', 'push']
    assert '--only' in commands[1]
    assert commands[2] == ['git', 'push', 'origin', 'HEAD:refs/heads/test-branch']
    assert all(c.kwargs['check'] and c.kwargs['timeout'] <= 30 for c in run.call_args_list)
    run.reset_mock()
    run.side_effect = subprocess.CalledProcessError(1, 'git')
    with pytest.raises(subprocess.CalledProcessError):
        worker.checkpoint_create()
    assert run.call_count == 1


@pytest.mark.parametrize('reject_push', [False, True])
def test_real_git_checkpoint_and_fresh_checkout(monkeypatch, tmp_path, reject_push):
    """Use only a local bare remote, never GitHub or a production journal."""
    from tools import breakout_worker as worker
    remote, checkout = tmp_path / 'remote.git', tmp_path / 'writer'

    def git(*args, cwd=None):
        return subprocess.check_output(['git', *args], cwd=cwd, stderr=subprocess.PIPE)

    git('init', '--bare', str(remote))
    git('init', '-b', 'test-branch', str(checkout))
    git('config', 'user.name', 'Outbox test', cwd=checkout)
    git('config', 'user.email', 'outbox@example.invalid', cwd=checkout)
    git('remote', 'add', 'origin', str(remote), cwd=checkout)
    monkeypatch.chdir(checkout)
    monkeypatch.setenv('BREAKOUT_OUTBOX_PATH', str(checkout / 'data/breakout_outbox.json'))
    monkeypatch.setenv('GITHUB_REF_NAME', 'test-branch')
    backend = Backend()
    observe()
    j.prepare(backend, set(), 'old-owner')
    git('add', 'data/breakout_outbox.json', cwd=checkout)
    git('commit', '-m', 'initial queued checkpoint', cwd=checkout)
    git('push', 'origin', 'test-branch', cwd=checkout)
    # Other staged files must never leak into the checkpoint commit.
    (checkout / 'unrelated.txt').write_text('unrelated change')
    git('add', 'unrelated.txt', cwd=checkout)
    if reject_push:
        git('config', 'receive.denyNonFastForwards', 'true', cwd=remote)
        # Make the local branch diverge from the remote so the normal push fails.
        git('commit', '--amend', '--only', '-m', 'divergent history', cwd=checkout)

    original_create = backend.create

    def create_after_remote_check(row):
        import json
        saved = json.loads(git('--git-dir', str(remote), 'show',
                               'test-branch:data/breakout_outbox.json'), object_hook=j.decode)
        assert saved['plans'][0]['state'] == 'creating'
        assert saved['plans'][0]['owner'] == 'new-owner'
        assert saved['plans'][0]['record']['first_detected_at'] == AT
        assert b'unrelated.txt' not in git('--git-dir', str(remote), 'ls-tree',
                                           '--name-only', 'test-branch')
        original_create(row)
        raise TimeoutError('response and final result push lost')

    monkeypatch.setattr(backend, 'create', create_after_remote_check)
    j.deliver(backend, 'new-owner', checkpoint=worker.checkpoint_create)
    assert len(backend.calls) == (0 if reject_push else 1)
    fresh = tmp_path / 'fresh'
    git('clone', '--branch', 'test-branch', str(remote), str(fresh))
    monkeypatch.setenv('BREAKOUT_OUTBOX_PATH', str(fresh / 'data/breakout_outbox.json'))
    assert j.load()['plans'][0]['state'] == ('queued' if reject_push else 'creating')
    if not reject_push:
        # Even an empty/delayed lookup cannot authorize another POST.
        backend.rows.clear()
        deliver(backend, 'third-owner')
        assert len(backend.calls) == 1
        assert j.load()['plans'][0]['state'] == 'ambiguous'


def test_held_exclusion_preserves_evidence_and_survives_restart():
    backend = Backend()
    first = observe()
    j.append('_set_outcome', ['000390', b.OUTCOME_CAP, 'cap zero', AT], {})
    original = deepcopy(j.load()['events'])
    j.prepare(backend, {'000390'}, '1')
    assert not j.load()['events'] and not j.load()['plans']
    assert [e['event'] for e in j.load()['excluded_events']] == original
    restart_process()
    j.append('_set_outcome', ['000390', b.OUTCOME_NOT_ORDERED, 'hours', AT], {})
    observe('999999')
    j.prepare(backend, {'000390'}, '2')
    deliver(backend, '2')
    assert backend.calls == ['999999']
    assert len(j.load()['excluded_events']) == 3
    assert j.load()['excluded_events'][0]['event']['id'] == first
    assert not j.load()['events']
    # A later breakout after leaving holdings starts a new episode.
    observe(now=AT + timedelta(days=1), price=130)
    j.prepare(backend, set(), '3')
    deliver(backend, '3')
    assert backend.calls == ['999999', '000390']
    assert next(r for r in backend.rows.values() if r['ticker']=='000390')['first_price'] == 130


@pytest.mark.parametrize('outcome', [b.OUTCOME_UNKNOWN, b.OUTCOME_ORDER_SENT, b.OUTCOME_PARTIAL])
def test_held_uncertain_order_is_never_excluded(outcome):
    backend = Backend()
    observe()
    j.append('_set_outcome', ['000390', outcome, 'uncertain', AT], {})
    j.prepare(backend, {'000390'}, '1')
    assert not j.load()['excluded_events']
    assert j.load()['events']


def test_held_orphan_and_full_archive_remain_pending(monkeypatch):
    backend = Backend()
    orphan = j.append('_set_outcome', ['000390', b.OUTCOME_CAP, 'cap', AT], {})
    j.prepare(backend, {'000390'}, '1')
    assert j.load()['events'][0]['id'] == orphan
    observe('999999')
    monkeypatch.setattr(j, 'MAX_EXCLUDED', 0)
    before = deepcopy(j.load()['events'])
    j.prepare(backend, {'000390', '999999'}, '2')
    assert j.load()['events'] == before
    assert not j.load()['excluded_events']
