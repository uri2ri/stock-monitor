"""읽기 전용 추적 화면/아침 이메일 요약. 네트워크는 호출자가 담당한다."""
import html
import math
import breakout_tracker as bt


def fmt(value, suffix=''):
    try:
        if value is None or not math.isfinite(float(value)):
            return '—'
        return f'{float(value):,.2f}{suffix}'
    except (ValueError, TypeError):
        return '—'


def table_rows(tracks):
    return [{
        '종목': f"{t.get('name', '')} ({t['ticker']})",
        '최초 감지(KST)': str(t.get('first_detected_at', '미확인')),
        '자동매수 결과': bt.outcome_label(t),
        '최신 종가': fmt(t.get('latest_close')),
        '가격 기준': bt.price_basis_label(t),
        '최초선 대비 %': fmt(t.get('change_pct'), '%'),
        '최초선 대비 ATR': fmt(t.get('atr_mult'), 'N'),
        '다음 돌파선까지': fmt(t.get('dist_to_line'), '원'),
        '추적 상태': t.get('status', '미확인'),
    } for t in tracks]


def report_text(tracks):
    """아침 리포트용 요약. 활성 건이 없으면 빈 문자열(= 섹션 생략)."""
    active = [t for t in tracks if t.get('status') == bt.STATUS_ACTIVE]
    if not active:
        return ''
    counts = bt.summarize(active)
    head = f'돌파 후 미진입 관찰 {counts["total"]}건 (주문 미확정 포함 · 매수 신호 아님)'
    if counts['refresh_failed']:
        head += f' · 가격 갱신 실패 {counts["refresh_failed"]}건'
    lines = [head]
    for t in active[:5]:
        lines.append(f"{t.get('name', t['ticker'])}: {bt.outcome_label(t)} · "
                     f"{fmt(t.get('latest_close'))} / {bt.price_basis_label(t)} · "
                     f"최초선 {fmt(t.get('change_pct'), '%')} / {fmt(t.get('atr_mult'), 'N')}")
    if len(active) > 5:
        lines.append(f'외 {len(active) - 5}건: 웹 추적 화면 참고')
    return '\n'.join(lines)


def report_html(tracks):
    text = report_text(tracks)
    return '<section><h2>돌파 후 미진입</h2><p>' + html.escape(text).replace('\n', '<br>') + '</p></section>' if text else ''


def render(st, tracks):
    st.subheader('돌파 후 미진입')
    st.caption('흐름 관찰용 · 매수 신호가 아닙니다. 주문접수·상태미확인은 미진입 확정이 아닙니다.')
    if not tracks:
        st.info('표시할 추적 기록이 없습니다.')
        return
    st.dataframe(table_rows(tracks), hide_index=True, width='stretch')
    keys = list(range(len(tracks)))
    choice = st.selectbox('추적 상세', keys,
                          format_func=lambda i: f"{tracks[i].get('name', '')} ({tracks[i]['ticker']}) · {tracks[i].get('first_detected_at')}")
    t = tracks[choice]
    st.markdown('#### 최초 기록')
    st.write(f"최초 감지(KST): {t.get('first_detected_at', '미확인')}")
    st.write(f"기준선 {fmt(t.get('first_threshold'))} · 가격 {fmt(t.get('first_price'))} · ATR {fmt(t.get('first_atr'))}")
    st.markdown('#### 현재 상태와 실행 결과')
    st.write(f"{t.get('status', '미확인')} · {bt.outcome_label(t)} · {bt.price_basis_label(t)}")
    st.write(f"최초선 위치: {t.get('level_state', '미확인')} · 당일 새 돌파: {t.get('new_breakout', '미확인')}")
    st.write(f"다음 거래일 기준선: {fmt(t.get('latest_line'))} · 거리 {fmt(t.get('dist_to_line'))}원")
    st.write('결과 사유: ' + (t.get('outcome_reason') or '기록 없음'))
    st.write(f"주문일/번호: {t.get('order_day') or '—'} / {t.get('order_no') or '—'}")
    if t.get('order_no'):
        st.write(f"체결 관측: {t.get('execution_state') or '조회 대기'} · 확인 {fmt(t.get('filled_qty'))}주 · 미확인 잔여 {fmt(t.get('unverified_qty'))}주")
        st.caption('미확인 잔여는 주문수량−확인체결수량입니다. 실제 미체결 주문 존속·취소를 확정하지 않습니다.')
    if t.get('refresh_state') == bt.REFRESH_FAIL:
        st.warning('갱신 실패: ' + (t.get('refresh_note') or '원인 미확인'))
    if t.get('closed_reason'):
        st.write('종료 근거: ' + t['closed_reason'])
    st.markdown('#### 사용자 관망 메모')
    st.write(t.get('user_memo') or '메모 없음')
    st.caption('메모는 자동매수 결과와 별개입니다. 수정·사용자 종료는 별도 Notion 추적 DB에서 합니다.')
