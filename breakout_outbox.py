"""Durable, single-writer observation journal. No network in append().

Notion has no idempotent create. A previous ambiguous create is LOOKED UP, never
resent automatically. Workflow must push a prepared journal before deliver().
"""
from __future__ import annotations
from contextlib import contextmanager
from copy import deepcopy
from datetime import date, datetime
import fcntl
import json
import logging
import os
from pathlib import Path
import tempfile
import time
from uuid import uuid4

log = logging.getLogger(__name__)
OPERATIONS = {'record_breakout', '_set_outcome', 'record_fill', 'close_track'}
MAX_EVENTS = 10000
MAX_BATCH = 100


def path():
    return Path(os.environ.get('BREAKOUT_OUTBOX_PATH', 'data/breakout_outbox.json'))


def encode(value):
    if isinstance(value, datetime):
        return {'__datetime__': value.isoformat()}
    if isinstance(value, date):
        return {'__date__': value.isoformat()}
    raise TypeError(type(value).__name__)


def decode(value):
    if set(value) == {'__datetime__'}:
        return datetime.fromisoformat(value['__datetime__'])
    if set(value) == {'__date__'}:
        return date.fromisoformat(value['__date__'])
    return value


@contextmanager
def locked():
    file = path()
    file.parent.mkdir(parents=True, exist_ok=True)
    with file.with_suffix('.lock').open('a') as lock:
        # Never block a trading call waiting for another process.
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def load():
    if not path().exists():
        return {'version': 1, 'events': [], 'plans': []}
    data = json.loads(path().read_text(), object_hook=decode)
    if data.get('version') != 1 or not isinstance(data.get('events'), list) or not isinstance(data.get('plans'), list):
        raise ValueError('Invalid outbox; preserve file and stop')
    return data


def save(data):
    file = path()
    file.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, default=encode, ensure_ascii=False, allow_nan=False, indent=2)
    fd, temporary = tempfile.mkstemp(prefix=file.name + '.', dir=file.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, file)
        directory = os.open(file.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def append(operation, args, kwargs):
    if operation not in OPERATIONS:
        raise ValueError('Unsupported observation event')
    with locked():
        data = load()
        if len(data['events']) >= MAX_EVENTS:
            raise RuntimeError('Outbox full; event not preserved')
        event = {'id': uuid4().hex, 'operation': operation,
                 'args': deepcopy(args), 'kwargs': deepcopy(kwargs)}
        data['events'].append(event)
        save(data)
        return event['id']


def prepare(backend, holdings, owner):
    """Project oldest events; make durable plans but perform NO external writes."""
    import breakout_tracker as b
    with locked():
        data = load()
        planned = {ident for plan in data['plans'] for ident in plan['events']}
        blocked_tickers = {plan['record']['ticker'] for plan in data['plans']}
        events = [e for e in data['events'] if e['id'] not in planned]
        if not events:
            return
        rows = backend.list_tracks(include_closed=True)
        memory = b.MemoryStore()
        memory.rows = {r['key']: deepcopy(r) for r in rows}
        original = deepcopy(memory.rows)
        previous = b.set_store(memory)
        b._held_tickers = set(holdings)
        # Sent events refer to a new buy executed before holdings were read.
        b._sent_tickers = {e['args'][0] for e in events
                           if e['operation'] == '_set_outcome' and e['args'][1] == b.OUTCOME_ORDER_SENT}
        groups, acknowledged = {}, set()
        try:
            predecessors = _predecessors(events, _event_ticker)
            handled = set()
            for event in _rotated(data, 'prepare_cursor', events)[:MAX_BATCH]:
                data['prepare_cursor'] = event['id']
                op, args, kwargs = event['operation'], event['args'], event['kwargs']
                ticker = args[0]['ticker'] if op == 'record_breakout' else args[0]
                if ticker in blocked_tickers or (predecessors[event['id']] is not None
                                                and predecessors[event['id']] not in handled):
                    continue
                before = deepcopy(memory.rows)
                b._capture_event_id = event['id']
                try:
                    b._strict_replay = True
                    getattr(b, op)(*args, **kwargs)
                except Exception as exc:
                    memory.rows = before
                    blocked_tickers.add(ticker)
                    log.warning('Observation replay pending %s: %s', event['id'], exc)
                    continue
                changed = [key for key, row in memory.rows.items() if before.get(key) != row]
                if not changed:
                    # Verified no-op; do not drop events attached to a pending write.
                    active = memory.find_active(ticker)
                    if not active:
                        # No target is not a successful save. This can be a held
                        # ticker or an orphan outcome whose observation was lost.
                        # Retain the evidence instead of inventing a new episode.
                        blocked_tickers.add(ticker)
                        log.warning('Observation target unresolved; retained %s', event['id'])
                        continue
                    if active and active['key'] in groups:
                        groups[active['key']].append(event['id'])
                    else:
                        acknowledged.add(event['id'])
                for key in changed:
                    groups.setdefault(key, []).append(event['id'])
                handled.add(event['id'])
            for key, ids in groups.items():
                row = memory.rows[key]
                data['plans'].append({'id': uuid4().hex, 'owner': owner, 'events': ids,
                    'record': row, 'page_id': key if key in original else None,
                    'fields': {k: v for k, v in row.items() if original.get(key, {}).get(k) != v},
                    'state': 'queued'})
            data['events'] = [e for e in data['events'] if e['id'] not in acknowledged]
            save(data)
        finally:
            b.set_store(previous)
            b._held_tickers = set()
            b._sent_tickers = set()
            b._capture_event_id = None
            b._strict_replay = False


def _event_ticker(event):
    return event['args'][0]['ticker'] if event['operation'] == 'record_breakout' else event['args'][0]


def _predecessors(items, ticker):
    """Keep journal order authoritative even when the scan starts mid-queue."""
    previous, result = {}, {}
    for item in items:
        symbol = ticker(item)
        result[item['id']] = previous.get(symbol)
        previous[symbol] = item['id']
    return result


def _rotated(data, cursor, items):
    # IDs survive removals/appends. A removed cursor resumes at the oldest item.
    for index, item in enumerate(items):
        if item['id'] == data.get(cursor):
            return items[index + 1:] + items[:index + 1]
    return items


def deliver(backend, owner, checkpoint=None):
    """Caller has pushed this prepared journal. Bounded attempts, per-plan ACK.

    A prior run's create is never reissued. An empty lookup is NOT proof that
    an earlier POST failed. Leave such a plan pending for manual confirmation.
    """
    with locked():
        data = load()
        started = time.monotonic()
        completed, acknowledged = set(), set()
        predecessors = _predecessors(data['plans'], lambda p: p['record']['ticker'])
        for plan in _rotated(data, 'deliver_cursor', data['plans'])[:20]:
            if time.monotonic() - started > 60:
                break
            data['deliver_cursor'] = plan['id']
            save(data)  # Persist scan progress before a slow/ambiguous request.
            if predecessors[plan['id']] is not None and predecessors[plan['id']] not in completed:
                continue
            try:
                row = plan['record']
                if plan['page_id']:
                    found = backend.find_event(row.get('event_id'))
                    if not found or found.get('key') != plan['page_id'] or found.get('ticker') != row['ticker']:
                        raise ValueError('Patch observation identity mismatch')
                    fields = plan.get('fields', row)
                    if found.get('status') == '종료' and row.get('status') != '종료':
                        raise ValueError('Closed observation cannot be reopened by a delayed patch')
                    backend.update(plan['page_id'], fields)
                else:
                    found = backend.find_event(row['event_id'])
                    if found:
                        # Never overwrite a newer state on an ambiguous create replay.
                        if (found.get('ticker') != row['ticker']
                                or found.get('first_detected_at') != row['first_detected_at']):
                            raise ValueError('Observation identity mismatch')
                    elif plan['state'] == 'queued' or (plan['state'] == 'prepared' and plan['owner'] == owner):
                        if checkpoint is None:
                            raise RuntimeError('Durable pre-POST checkpoint is required')
                        plan['state'] = 'creating'
                        plan['owner'] = owner
                        save(data)  # local write-ahead before external POST
                        checkpoint()  # Must reach durable remote storage BEFORE POST.
                        backend.create(row)
                    else:
                        plan['state'] = 'ambiguous'
                        raise RuntimeError('Create not found; do not resend an ambiguous POST')
                completed.add(plan['id'])
                acknowledged.update(plan['events'])
            except Exception as exc:
                log.warning('Observation delivery pending %s: %s', plan['id'], exc)
            # Save successes even if another ticker fails later.
            data['plans'] = [p for p in data['plans'] if p['id'] not in completed]
            data['events'] = [e for e in data['events'] if e['id'] not in acknowledged]
            save(data)
        return len(data['plans'])
