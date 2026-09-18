"""Synthetic UI preview; never loads .env or operational records."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from datetime import date, datetime
import streamlit as st
import breakout_tracker as b
from breakout_view import render

st.set_page_config(page_title='돌파 추적 — 더미 화면', layout='wide')
st.warning('화면 검증용 가상 데이터입니다. 실제 SP삼화/GS 이력이 아닙니다.')
tracks = [dict(key='demo-1', ticker='DEMO01', name='가상 관찰종목 A',
    first_detected_at=datetime(2026, 9, 17, 10, 15, tzinfo=b.KST),
    first_threshold=10000, first_price=10200, first_atr=400,
    latest_close=11200, latest_date=date(2026, 9, 18), latest_line=11500,
    change_pct=12, atr_mult=3, dist_to_line=300,
    status=b.STATUS_ACTIVE, outcome=b.OUTCOME_CASH,
    outcome_reason='현금 부족(가상 실행 결과)', user_memo='사용자 관망 메모 예시',
    level_state=b.LEVEL_ABOVE, new_breakout=b.NEW_BREAKOUT_NO,
    refresh_state=b.REFRESH_OK),
    dict(key='demo-2', ticker='DEMO02', name='가상 주문종목 B',
    first_detected_at=datetime(2026, 9, 18, 9, 10, tzinfo=b.KST),
    first_threshold=20000, first_price=20100, first_atr=600,
    status=b.STATUS_ACTIVE, outcome=b.OUTCOME_ORDER_SENT,
    order_no='DEMO-ORDER', order_day=date(2026, 9, 18),
    outcome_reason='접수 확인, 체결 증빙 대기',
    refresh_state=b.REFRESH_FAIL, refresh_note='가상 갱신 실패')]
render(st, tracks)
