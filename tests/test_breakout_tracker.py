from datetime import date, datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock
import pandas as pd
import pytest
import breakout_tracker as b
import notion_repo as n

NOW = datetime(2026, 9, 18, 10, tzinfo=b.KST)
HIT = dict(ticker='000390', name='가상종목', high20=100, price=105, atr20=5)
ROW = dict(ticker='000390', close=110, high20=100, high20_next=115,
           atr20=10, scan_date='20260918')

@pytest.fixture(autouse=True)
def store():
    old = b.set_store(b.MemoryStore())
    b._pending.clear()
    b._sent_tickers.clear()
    b._held_tickers.clear()
    yield b.get_store()
    b.set_store(old)
    b._pending.clear()
    b._sent_tickers.clear()
    b._held_tickers.clear()

def start():
    return b.record_breakout(HIT, NOW)

def test_first_values_and_timezone_preserved(store):
    key = start()
    b.record_breakout({**HIT, 'price': 130, 'atr20': 20}, NOW + timedelta(days=1))
    assert len(store.rows) == 1
    assert store.rows[key]['first_price'] == 105
    assert store.rows[key]['first_atr'] == 5
    assert store.rows[key]['first_detected_at'].utcoffset() == timedelta(hours=9)

@pytest.mark.parametrize('bad', [None, 0, -1, float('nan'), float('inf'), '', True])
@pytest.mark.parametrize('field', ['high20', 'price', 'atr20'])
def test_invalid_first_values(store, field, bad):
    assert b.record_breakout({**HIT, field: bad}, NOW) is None
    assert not store.rows

def test_same_process_concurrent_first_observation(store):
    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(lambda _: start(), range(20)))
    assert len(store.rows) == 1

def test_order_identity_and_new_episode(store):
    key = start()
    b.record_order_sent('000390', 'old', NOW, order_day=NOW.date())
    assert not b.record_fill('000390', 1, 105, NOW, order_no='wrong', order_day=NOW.date())
    assert not b.record_fill('000390', 1, 105, NOW, order_no='old', order_day=date(2026, 9, 17))
    assert b.record_fill('000390', 1, 105, NOW, order_no='old', order_day=NOW.date())
    assert store.rows[key]['status'] == b.STATUS_CLOSED
    new = b.record_breakout(HIT, NOW + timedelta(hours=1))
    assert not b.record_fill('000390', 1, 105, NOW + timedelta(hours=2), order_no='old', order_day=NOW.date())
    assert store.rows[new]['status'] == b.STATUS_ACTIVE

def test_unknown_and_sent_never_become_confirmed_no_entry(store):
    key = start()
    b.record_unknown('000390', '전송 결과 불명', NOW)
    b.record_no_entry('000390', '현금 부족', NOW)
    assert store.rows[key]['outcome'] == b.OUTCOME_UNKNOWN
    assert not b.is_confirmed_no_entry(store.rows[key])
    b.record_order_sent('000390', '123', NOW)
    b.record_no_entry('000390', '상한 0', NOW)
    assert store.rows[key]['outcome'] == b.OUTCOME_ORDER_SENT

def test_no_entry_reasons_separate_user_memo(store):
    key = start()
    store.rows[key]['user_memo'] = '사용자 관망'
    b.record_no_entry('000390', '현금 부족', NOW, b.OUTCOME_CASH)
    assert store.rows[key]['outcome'] == b.OUTCOME_CASH
    b.record_no_entry('000390', '일일 상한 0', NOW, b.OUTCOME_CAP)
    assert store.rows[key]['outcome'] == b.OUTCOME_CAP
    assert store.rows[key]['user_memo'] == '사용자 관망'

def test_outcome_category_comes_from_caller_not_reason_text(store):
    """증권사 거부 메시지에 '상한가'가 섞여도 상한제한으로 둔갑하지 않는다."""
    key = start()
    b.record_no_entry('000390', '주문 거부: 상한가 초과 주문입니다', NOW)
    assert store.rows[key]['outcome'] == b.OUTCOME_NOT_ORDERED
    assert store.rows[key]['outcome_reason'] == '주문 거부: 상한가 초과 주문입니다'

def test_unknown_category_falls_back_to_not_ordered(store):
    key = start()
    assert b.record_no_entry('000390', '사유', NOW, '체결확인')
    assert store.rows[key]['outcome'] == b.OUTCOME_NOT_ORDERED

def test_day_boundary_rejects_previous_order(store):
    key = b.record_breakout(HIT, datetime(2026, 9, 18, 15, 5, tzinfo=timezone.utc))
    assert store.rows[key]['first_detected_at'].date() == date(2026, 9, 19)
    assert not b.record_order_sent('000390', 'old', NOW, order_day=NOW.date())

def test_daily_uses_first_atr_and_separate_lines(store):
    key = start()
    fields = b.compute_daily(store.rows[key], ROW)
    assert fields['atr_mult'] == 2
    assert fields['change_pct'] == 10
    assert fields['new_breakout'] == b.NEW_BREAKOUT_YES
    assert fields['latest_line'] == 115
    assert fields['dist_to_line'] == 5

@pytest.mark.parametrize('bad', [None, 0, -1, float('nan'), float('inf'), True])
@pytest.mark.parametrize('field', ['close', 'atr20', 'high20', 'high20_next'])
def test_invalid_daily_preserves_previous_price(store, field, bad):
    key = start()
    result = b.compute_daily(store.rows[key], {**ROW, field: bad})
    assert result['refresh_state'] == b.REFRESH_FAIL
    assert 'latest_close' not in result

def test_refresh_once_and_failure_visible(store):
    key = start()
    frame = pd.DataFrame([ROW])
    at = NOW.replace(hour=21)
    assert b.refresh_from_scan(frame, at)['updated'] == 1
    assert b.refresh_from_scan(frame, at)['skipped'] == 1
    assert b.refresh_from_scan(pd.DataFrame(), at)['failed'] == 1
    assert store.rows[key]['latest_close'] == 110
    assert store.rows[key]['refresh_state'] == b.REFRESH_FAIL

def test_unconfirmed_close_and_future_date_rejected(store):
    key = start()
    assert b.refresh_from_scan(pd.DataFrame([ROW]), NOW)['failed'] == 1
    assert 'latest_close' not in store.rows[key]

def test_disabled_and_repository_failure(monkeypatch):
    backend = Mock(configured=Mock(return_value=False))
    b.set_store(backend)
    assert start() is None
    backend.find_active.assert_not_called()
    backend.configured.return_value = True
    backend.find_active.side_effect = RuntimeError('offline')
    assert start() is None
    backend.create.assert_not_called()

def test_no_io_until_flush_and_batched_writes(monkeypatch):
    backend = b.NotionStore()
    monkeypatch.setattr(backend, 'configured', lambda: True)
    read = Mock(return_value=[])
    create = Mock(return_value='new')
    monkeypatch.setattr(backend, 'list_tracks', read)
    monkeypatch.setattr(backend, 'create', create)
    monkeypatch.setattr(n, 'fetch_holdings', Mock(return_value=[]))
    monkeypatch.setenv('BREAKOUT_TRACK_WRITER', 'auto-trade')
    b.set_store(backend)
    start()
    b.record_no_entry('000390', '현금 부족', NOW, b.OUTCOME_CASH)
    b.record_no_entry('000390', '현금 부족', NOW, b.OUTCOME_CASH)
    read.assert_not_called()
    create.assert_not_called()
    b.flush_events()
    read.assert_called_once()
    n.fetch_holdings.assert_called_once()
    create.assert_called_once()
    assert create.call_args[0][0]['outcome'] == b.OUTCOME_CASH

def test_held_stock_not_recreated(monkeypatch):
    backend = b.NotionStore()
    monkeypatch.setattr(backend, 'configured', lambda: True)
    monkeypatch.setattr(backend, 'list_tracks', lambda **kw: [])
    create = Mock()
    monkeypatch.setattr(backend, 'create', create)
    monkeypatch.setattr(n, 'fetch_holdings', lambda: [('p', Mock(ticker='000390'))])
    monkeypatch.setenv('BREAKOUT_TRACK_WRITER', 'auto-trade')
    b.set_store(backend)
    start()
    b.flush_events()
    create.assert_not_called()

def test_duplicate_active_is_not_arbitrarily_selected(store):
    key = start()
    store.rows['duplicate'] = dict(store.rows[key])
    assert not b.record_no_entry('000390', '현금 부족', NOW)
    assert store.rows[key]['outcome'] == b.OUTCOME_NONE

def test_notion_initial_fields_and_memo_immutable(monkeypatch):
    patch = Mock()
    monkeypatch.setattr(n.requests, 'patch', patch)
    monkeypatch.setattr(n, '_headers', lambda: {})
    n.update_breakout_track('p', {'first_price': 999, 'first_atr': 77, 'user_memo': 'overwrite'})
    patch.assert_not_called()

def test_missing_scan_is_not_reported_as_a_per_ticker_cause(store):
    """스캔 파일이 통째로 없는 건 '이 종목이 스캔에 없다'와 다른 사건이다."""
    key = start()
    at = NOW.replace(hour=21)
    assert b.refresh_from_scan(None, at)['failed'] == 1
    note = store.rows[key]['refresh_note']
    assert '야간 스캔 결과 없음' in note
    assert '유동성 컷' not in note
    assert 'latest_close' not in store.rows[key]

def test_single_ticker_missing_keeps_its_own_cause(store):
    key = start()
    at = NOW.replace(hour=21)
    other = pd.DataFrame([{**ROW, 'ticker': '005930'}])
    assert b.refresh_from_scan(other, at)['failed'] == 1
    assert '이 종목이 없습니다' in store.rows[key]['refresh_note']

def test_price_basis_never_reads_as_live_price(store):
    key = start()
    b.refresh_from_scan(pd.DataFrame([ROW]), NOW.replace(hour=21))
    assert b.price_basis_label(store.rows[key]) == '2026-09-18 종가'
    store.rows[key]['refresh_state'] = b.REFRESH_FAIL
    assert '갱신 실패' in b.price_basis_label(store.rows[key])
    assert b.price_basis_label({}) == '가격 미갱신'

def test_held_ticker_is_not_retracked_after_close(monkeypatch):
    """체결로 닫힌 뒤 같은 종목이 다시 감지돼도 미진입으로 되살아나지 않는다."""
    backend = b.NotionStore()
    closed = dict(ticker='000390', name='가상종목', key='old',
                  first_detected_at=NOW, first_threshold=100, first_price=105,
                  first_atr=5, status=b.STATUS_CLOSED, outcome=b.OUTCOME_FILLED,
                  closed_reason=b.CLOSE_FILLED)
    monkeypatch.setattr(backend, 'configured', lambda: True)
    monkeypatch.setattr(backend, 'list_tracks', lambda **kw: [dict(closed)])
    create, update = Mock(), Mock()
    monkeypatch.setattr(backend, 'create', create)
    monkeypatch.setattr(backend, 'update', update)
    monkeypatch.setattr(n, 'fetch_holdings', lambda: [('p', Mock(ticker='000390'))])
    monkeypatch.setenv('BREAKOUT_TRACK_WRITER', 'auto-trade')
    b.set_store(backend)

    b.record_breakout(HIT, NOW + timedelta(days=3))
    b.flush_events()

    create.assert_not_called()
    update.assert_not_called()

def test_writer_guard_blocks_unexpected_processes(monkeypatch):
    """지정한 단일 writer가 아니면 아무것도 쓰지 않는다 (중복 생성 방지)."""
    backend = b.NotionStore()
    monkeypatch.setattr(backend, 'configured', lambda: True)
    create = Mock()
    monkeypatch.setattr(backend, 'create', create)
    monkeypatch.setattr(backend, 'list_tracks', Mock(return_value=[]))
    monkeypatch.delenv('BREAKOUT_TRACK_WRITER', raising=False)
    b.set_store(backend)
    start()
    b.flush_events()
    create.assert_not_called()
    backend.list_tracks.assert_not_called()

def test_naive_datetime_is_rejected(store):
    start()
    with pytest.raises(ValueError):
        b.local_time(datetime(2026, 9, 18, 10))
