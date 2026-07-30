"""
app.py – 신규 종목 조사용 Streamlit 앱
core.py의 calc_atr을 그대로 사용한다.
"""

from __future__ import annotations

import math

import streamlit as st
from pykrx import stock as krx

from core import ATR_PERIOD, calc_atr, fetch_ohlcv, latest_close

# ── 페이지 설정 ─────────────────────────────────────────────

st.set_page_config(
    page_title="종목 진입 점검",
    page_icon="📊",
    layout="centered",
)

# ── 커스텀 CSS ──────────────────────────────────────────────

st.markdown(
    """
    <style>
    /* 메트릭 카드 */
    div[data-testid="stMetric"] {
        background: linear-gradient(135deg, #1e293b 0%, #0f172a 100%);
        border: 1px solid #334155;
        border-radius: 12px;
        padding: 16px 20px;
    }
    div[data-testid="stMetric"] label {
        color: #94a3b8 !important;
        font-size: 0.85rem !important;
    }
    div[data-testid="stMetric"] div[data-testid="stMetricValue"] {
        color: #f1f5f9 !important;
        font-size: 1.6rem !important;
        font-weight: 700 !important;
    }

    /* 체크리스트 컨테이너 */
    .checklist-pass {
        background: #064e3b;
        border-left: 4px solid #10b981;
        border-radius: 6px;
        padding: 10px 16px;
        margin: 6px 0;
        color: #d1fae5;
    }
    .checklist-fail {
        background: #451a03;
        border-left: 4px solid #f59e0b;
        border-radius: 6px;
        padding: 10px 16px;
        margin: 6px 0;
        color: #fef3c7;
    }
    .checklist-error {
        background: #450a0a;
        border-left: 4px solid #ef4444;
        border-radius: 6px;
        padding: 10px 16px;
        margin: 6px 0;
        color: #fecaca;
    }

    /* 헤더 영역 */
    .app-header {
        text-align: center;
        padding: 1rem 0 0.5rem 0;
    }
    .app-header h1 {
        font-size: 1.8rem;
        background: linear-gradient(90deg, #60a5fa, #a78bfa);
        -webkit-background-clip: text;
        -webkit-text-fill-color: transparent;
        margin-bottom: 0.2rem;
    }
    .app-header p {
        color: #64748b;
        font-size: 0.95rem;
    }

    /* 구분선 */
    .section-divider {
        border: none;
        border-top: 1px solid #1e293b;
        margin: 1.5rem 0;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

# ── 헤더 ────────────────────────────────────────────────────

st.markdown(
    """
    <div class="app-header">
        <h1>📊 종목 진입 점검</h1>
        <p>신규 종목 조사 전용 · 터틀 트레이딩 기반</p>
    </div>
    """,
    unsafe_allow_html=True,
)

# ── 입력 ────────────────────────────────────────────────────

col_left, col_right = st.columns(2)

with col_left:
    ticker = st.text_input(
        "종목코드 (6자리)",
        placeholder="예: 009150",
        max_chars=6,
        help="KRX 종목코드 6자리를 입력하세요.",
    )

with col_right:
    total_capital = st.number_input(
        "총자산 (원)",
        min_value=1_000_000,
        max_value=100_000_000_000,
        value=100_000_000,
        step=10_000_000,
        format="%d",
        help="포지션 사이징 계산에 사용됩니다.",
    )

run_btn = st.button("조회", type="primary", use_container_width=True)

# ── 조회 로직 ───────────────────────────────────────────────

if run_btn and ticker:
    ticker = ticker.strip()

    if len(ticker) != 6 or not ticker.isdigit():
        st.error("종목코드는 숫자 6자리여야 합니다.")
        st.stop()

    with st.spinner("시세 조회 중…"):
        try:
            _df = fetch_ohlcv(ticker)
            atr = calc_atr(_df)
            current_price = latest_close(_df)
        except Exception as e:
            st.error(f"조회 실패: {e}")
            st.stop()

    # ── 기본 계산 ───────────────────────────────────────────
    unit_shares = math.floor((total_capital * 0.01) / atr) if atr > 0 else 0
    stop_loss = current_price - (2 * atr)
    risk_per_unit = unit_shares * atr  # 1회 리스크액
    unit_invest = current_price * unit_shares  # 1유닛 투자금

    # 종목명 조회
    try:
        stock_name = krx.get_market_ticker_name(ticker)
    except Exception:
        stock_name = ticker

    # ── 결과 표시 ───────────────────────────────────────────
    st.markdown('<hr class="section-divider">', unsafe_allow_html=True)
    st.subheader(f"{stock_name} ({ticker})")

    m1, m2, m3 = st.columns(3)
    m1.metric("현재가", f"{current_price:,.0f}원")
    m2.metric(f"ATR ({ATR_PERIOD}일)", f"{atr:,.0f}원")
    m3.metric("1유닛 주수", f"{unit_shares:,}주")

    m4, m5 = st.columns(2)
    m4.metric("손절선", f"{stop_loss:,.0f}원")
    m5.metric("1회 리스크액", f"{risk_per_unit:,.0f}원")

    # ── 부가 정보 ───────────────────────────────────────────
    stop_pct = ((current_price - stop_loss) / current_price * 100) if current_price > 0 else 0
    atr_pct = (atr / current_price * 100) if current_price > 0 else 0
    invest_pct = (unit_invest / total_capital * 100) if total_capital > 0 else 0

    with st.expander("상세 수치", expanded=False):
        detail_col1, detail_col2 = st.columns(2)
        with detail_col1:
            st.markdown(f"- **손절폭**: 현재가 대비 {stop_pct:.1f}%")
            st.markdown(f"- **ATR 비율**: 현재가 대비 {atr_pct:.1f}%")
            st.markdown(f"- **리스크 비율**: 총자산의 {risk_per_unit / total_capital * 100:.2f}%")
        with detail_col2:
            st.markdown(f"- **1유닛 투자금**: {unit_invest:,.0f}원")
            st.markdown(f"- **투자 비중**: 총자산의 {invest_pct:.1f}%")
            st.markdown(f"- **2×ATR**: {2 * atr:,.0f}원")

    # ── 진입 체크리스트 (6항목) ──────────────────────────────
    st.markdown('<hr class="section-divider">', unsafe_allow_html=True)
    st.subheader("진입 체크리스트")

    # 유동성 검사: 최근 20일 평균 거래대금
    try:
        from datetime import date, timedelta

        end = date.today()
        start = end - timedelta(days=60)
        vol_df = krx.get_market_ohlcv(
            start.strftime("%Y%m%d"),
            end.strftime("%Y%m%d"),
            ticker,
        )
        avg_trade_val = (vol_df["종가"] * vol_df["거래량"]).tail(20).mean()
    except Exception:
        avg_trade_val = None

    checks: list[tuple[str, bool | None, str]] = [
        (
            "ATR 산출",
            atr > 0,
            f"최근 {ATR_PERIOD}거래일 TR 평균 정상 산출"
            if atr > 0
            else "데이터 부족으로 ATR 산출 불가",
        ),
        (
            "최소 1주 진입 가능",
            unit_shares >= 1,
            f"1유닛 = {unit_shares:,}주"
            if unit_shares >= 1
            else f"ATR({atr:,.0f}) 대비 총자산 부족",
        ),
        (
            "손절폭 적정 (≤15%)",
            stop_pct <= 15,
            f"현재가 대비 {stop_pct:.1f}%"
            + (" — 양호" if stop_pct <= 15 else " — 과대, 진입 재고"),
        ),
        (
            "유동성 (20일 평균 거래대금 ≥ 1억)",
            (avg_trade_val >= 100_000_000) if avg_trade_val is not None else None,
            f"평균 {avg_trade_val / 1e8:.1f}억원"
            if avg_trade_val is not None
            else "거래대금 조회 실패",
        ),
        (
            "집중 리스크 (1유닛 투자금 ≤ 총자산 10%)",
            invest_pct <= 10,
            f"총자산의 {invest_pct:.1f}%"
            + (" — 양호" if invest_pct <= 10 else " — 비중 과다"),
        ),
        (
            "변동성 범위 (ATR/현재가 1~8%)",
            1 <= atr_pct <= 8,
            f"ATR 비율 {atr_pct:.1f}%"
            + (
                " — 적정"
                if 1 <= atr_pct <= 8
                else (" — 변동성 과소" if atr_pct < 1 else " — 변동성 과대")
            ),
        ),
    ]

    pass_count = 0
    for label, passed, detail in checks:
        if passed is None:
            css_class = "checklist-error"
            icon = "⚠️"
        elif passed:
            css_class = "checklist-pass"
            icon = "✅"
            pass_count += 1
        else:
            css_class = "checklist-fail"
            icon = "⚠️"

        st.markdown(
            f'<div class="{css_class}">{icon} <b>{label}</b> — {detail}</div>',
            unsafe_allow_html=True,
        )

    # 종합
    st.markdown('<hr class="section-divider">', unsafe_allow_html=True)
    total_checks = len(checks)
    if pass_count == total_checks:
        st.success(f"✅ 전 항목 통과 ({pass_count}/{total_checks})")
    elif pass_count >= 4:
        st.warning(f"⚠️ {pass_count}/{total_checks} 통과 — 미통과 항목 확인 필요")
    else:
        st.error(f"❌ {pass_count}/{total_checks} 통과 — 진입 재고 권장")

elif run_btn and not ticker:
    st.warning("종목코드를 입력하세요.")

# ── 푸터 ────────────────────────────────────────────────────

st.markdown(
    """
    <hr class="section-divider">
    <p style="text-align:center; color:#475569; font-size:0.8rem;">
        터틀 트레이딩 포지션 사이징 기반 · 투자 판단은 본인 책임
    </p>
    """,
    unsafe_allow_html=True,
)
