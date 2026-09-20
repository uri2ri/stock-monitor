"""Bounded post-trade BUY execution observation. No trading/holdings/ledger writes."""
from datetime import date, datetime
import logging
import time
import breakout_tracker as b

log = logging.getLogger(__name__)


def reconcile(backend, query, account_key, now=None):
    now = b.local_time(now)
    rows = backend.list_tracks(include_closed=False)
    eligible = [r for r in rows if r.get('status') == b.STATUS_ACTIVE and r.get('order_no')]
    eligible.sort(key=lambda r: str(r.get('execution_checked_at') or ''))
    result = {'checked': 0, 'closed': 0, 'failed': 0}
    started = time.monotonic()
    for row in eligible[:5]:
        if time.monotonic() - started >= 60:
            break
        fields = {'execution_checked_at': now}
        try:
            first = row.get('first_detected_at')
            if (not row.get('event_id') or not isinstance(first, datetime)
                    or not isinstance(row.get('order_day'), date)
                    or not first.date() <= row['order_day'] <= now.date()
                    or row.get('account_type') != '모의'
                    or row.get('order_side') != '매수'
                    or not account_key or row.get('account_key') != account_key
                    or not b.positive(row.get('order_qty'))
                    or not float(row['order_qty']).is_integer()):
                raise ValueError('추적/계좌/종목/매수/주문일 연결 증빙 부족')
            evidence = query(row)
            result['checked'] += 1
            if evidence.get('state') == 'not_found':
                fields['execution_state'] = '주문미발견 - 체결 여부 미확인'
            else:
                qty = evidence['filled_qty']
                if (not isinstance(qty, int) or isinstance(qty, bool)
                        or not 0 <= qty <= row['order_qty']
                        or qty < (row.get('filled_qty') or 0)
                        or (qty > 0 and not b.positive(evidence.get('avg_price')))):
                    raise ValueError('체결 증빙 수량/가격 불일치')
                fields.update(filled_qty=qty, unverified_qty=row['order_qty'] - qty)
                if qty == row['order_qty']:
                    fields.update(outcome=b.OUTCOME_FILLED, status=b.STATUS_CLOSED,
                                  closed_reason=b.CLOSE_FILLED, closed_at=now.date(),
                                  execution_state='전량체결확인', outcome_at=now,
                                  outcome_reason=f"주문 {row['order_day']} / {row['order_no']} 전량 {qty}주 체결 확인")
                elif qty > 0:
                    fields.update(outcome=b.OUTCOME_PARTIAL, execution_state='잔여 주문 확인 필요',
                                  outcome_reason=f'{qty}주 부분체결, 미확인 잔여 {row["order_qty"] - qty:g}주', outcome_at=now)
                else:
                    fields['execution_state'] = '체결수량0확인 - 주문 존속/취소는 미확인'
                if qty:
                    fields['execution_price'] = evidence['avg_price']
        except Exception as exc:
            result['failed'] += 1
            fields['execution_state'] = '조회실패/증빙불일치 - 이전 체결 관측 유지'
            log.warning('Tracking execution deferred %s: %s', row['key'], exc)
        try:
            backend.update(row['key'], fields)
            if fields.get('status') == b.STATUS_CLOSED:
                result['closed'] += 1
        except Exception as exc:
            result['failed'] += 1
            log.warning('Tracking execution save pending %s: %s', row['key'], exc)
    return result


def run(backend):
    import kis_client as k
    token = None
    def query(row):
        nonlocal token
        if token is None:
            token = k.get_access_token()
        return k.get_order_execution(token, row['order_no'], order_day=row['order_day'],
                                     tracking_identity=row)
    return reconcile(backend, query, k.tracking_account_key())
