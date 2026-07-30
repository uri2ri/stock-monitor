"""
app.py – 신규 종목 조사용 Streamlit 앱

계산은 전부 core.py에 있다. 이 파일은 입력을 받아 core를 호출하고
결과를 그리기만 한다. 계산식을 여기서 다시 구현하지 않는다.

보유종목·매수단가는 다루지 않는다 (신규 종목 조사 전용).

로컬 실행:
    streamlit run app.py
"""

from __future__ import annotations

import plotly.graph_objects as go
import streamlit as st
from pykrx import stock as krx

import core

CHART_DAYS = 60          # 차트에 그릴 거래일 수
CHASE_ATR_MULT = 0.5     # 돌파 후 이만큼 더 진행했으면 추격 구간으로 본다

st.set_page_config(page_title="종목 진입 점검", page_icon="📊", layout="wide")


# ── 데이터 조회 (캐시) ──────────────────────────────────────

@st.cache_data(ttl=600, show_spinner=False)
def load_ohlcv(code: str):
    return core.fetch_ohlcv(code)


@st.cache_data(ttl=86_400, show_spinner=False)
def load_name(code: str) -> str:
    try:
        return krx.get_market_ticker_name(code)
    except Exception:
        return code          # 이름 조회 실패는 계산에 영향 없음


# ── 진입 신호 판정 ──────────────────────────────────────────

def entry_state(sig: core.TrendSignal, atr: float) -> tuple[str, str]:
    """터틀 진입 트리거의 현재 상태.

    매매를 권하는 판단이 아니라, 20일 고가 돌파 규칙이 지금
    어디에 있는지를 그대로 옮긴 것이다.

    - 추격금지: 돌파는 했으나 이미 0.5×ATR 넘게 더 진행
    - 진입가능: 직전 20일 고가를 막 넘어선 구간
    - 대기: 아직 돌파 전
    """
    gap = sig.current_price - sig.high_20_prev
    if gap > CHASE_ATR_MULT * atr:
        return "추격금지", (
            f"직전 20일 고가보다 {gap:,.0f}원 위 "
            f"({gap / atr:.1f}×ATR) — 돌파 시점에서 이미 벗어남"
        )
    if sig.breakout:
        return "진입가능", (
            f"직전 20일 고가 {sig.high_20_prev:,.0f} 돌파 "
            f"(+{gap:,.0f}원, {gap / atr:.1f}×ATR)"
        )
    return "대기", (
        f"직전 20일 고가 {sig.high_20_prev:,.0f}까지 "
        f"{-gap:,.0f}원 남음"
    )


# ── 차트 ────────────────────────────────────────────────────

def build_chart(df, sig: core.TrendSignal, stop_loss: float, name: str):
    """최근 60거래일 차트."""
    d = df.tail(CHART_DAYS)
    x = d.index

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=x, y=d["고가"], name="High", mode="lines",
        line=dict(color="#2f9e44", width=1, dash="dot"),
    ))
    fig.add_trace(go.Scatter(
        x=x, y=d["저가"], name="Low", mode="lines",
        line=dict(color="#e03131", width=1, dash="dot"),
    ))
    fig.add_trace(go.Scatter(
        x=x, y=d["종가"], name="종가", mode="lines",
        line=dict(color="#000000", width=2),
    ))

    for value, color, label in (
        (sig.high_20, "#2f9e44", f"20일 고가 {sig.high_20:,.0f}"),
        (sig.low_10, "#e03131", f"10일 저가 {sig.low_10:,.0f}"),
        (stop_loss, "#f76707", f"손절선 {stop_loss:,.0f}"),
    ):
        fig.add_hline(
            y=value, line=dict(color=color, width=1.5, dash="dash"),
            annotation_text=label, annotation_position="right",
            annotation_font=dict(color=color, size=11),
        )

    fig.add_trace(go.Scatter(
        x=[x[-1]], y=[sig.current_price], name="현재 종가", mode="markers",
        marker=dict(color="#000000", size=11, symbol="circle"),
        hovertemplate="현재 종가 %{y:,.0f}<extra></extra>",
    ))

    fig.update_layout(
        title=f"{name} · 최근 {len(d)}거래일",
        height=520, hovermode="x unified",
        margin=dict(l=10, r=110, t=50, b=10),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
        xaxis=dict(title=None, showgrid=False),
        yaxis=dict(title="원", tickformat=",.0f"),
    )
    return fig


# ── 화면 ────────────────────────────────────────────────────

st.title("📊 종목 진입 점검")
st.caption("신규 종목 조사 전용 · 터틀 트레이딩 규칙을 그대로 계산해 보여줍니다.")

col_code, col_cap, col_btn = st.columns([2, 2, 1])
with col_code:
    code = st.text_input("종목코드 (6자리)", placeholder="예: 240550", max_chars=6)
with col_cap:
    capital = st.number_input(
        "총자본 (원)",
        min_value=1_000_000, max_value=100_000_000_000,
        value=core.DEFAULT_CAPITAL, step=1_000_000, format="%d",
    )
with col_btn:
    st.write("")
    st.write("")
    run = st.button("조회", type="primary", width="stretch")

# 조회 결과를 세션에 남긴다. AI 의견 버튼처럼 다른 위젯을 눌러도
# 스크립트가 처음부터 다시 실행되므로, 남겨두지 않으면 화면이 비워진다.
if run:
    entered = (code or "").strip()
    if len(entered) != 6 or not entered.isdigit():
        st.error("종목코드는 숫자 6자리여야 합니다.")
        st.stop()
    st.session_state["query"] = (entered, capital)

if "query" not in st.session_state:
    st.stop()

code, capital = st.session_state["query"]

with st.spinner("시세 조회 중…"):
    try:
        df = load_ohlcv(code)
        atr = core.calc_atr(df)
        price = core.latest_close(df)
        sig = core.trend_signals(df)
        pos = core.calc_position(atr, capital)
        stop_loss = core.calc_stop(price, atr)
    except Exception as e:
        st.error(f"조회 실패: {e}")
        st.stop()

name = load_name(code)
st.subheader(f"{name} ({code})")
st.caption(
    f"{df.index[0]:%Y-%m-%d} ~ {df.index[-1]:%Y-%m-%d} · {len(df)}거래일 · "
    f"ATR은 터틀 원본 방식(Wilder, {core.ATR_PERIOD}일)"
)

# ── 진입 신호 ───────────────────────────────────────────────
st.markdown("### ■ 진입 신호")

state, reason = entry_state(sig, atr)
c1, c2, c3 = st.columns(3)
c1.metric("직전 20일 고가", f"{sig.high_20_prev:,.0f}원")
c2.metric(
    "현재가", f"{price:,.0f}원",
    delta=f"{price - sig.high_20_prev:+,.0f} vs 20일 고가",
)
c3.metric("돌파 여부", "돌파" if sig.breakout else "미돌파")

c4, c5, c6 = st.columns(3)
c4.metric("10일 저가 (청산선)", f"{sig.low_10:,.0f}원")
c5.metric("청산선까지 거리", f"{sig.dist_to_exit_pct:.1f}%",
          delta=f"{price - sig.low_10:+,.0f}원")
c6.metric("청산 트리거", "발생" if sig.exit_triggered else "미발생")

if state == "진입가능":
    st.success(f"판정: {state} — {reason}")
elif state == "대기":
    st.info(f"판정: {state} — {reason}")
else:
    st.warning(f"판정: {state} — {reason}")
st.caption(
    "규칙이 지금 어디에 있는지를 표시한 것입니다. 매매 판단은 본인 몫입니다."
)

# ── 기대값 ──────────────────────────────────────────────────
st.markdown(f"### ■ 기대값 (최근 {core.EDGE_DAYS}일)")

try:
    edge = core.calc_edge(df)
    e1, e2, e3, e4, e5 = st.columns(5)
    e1.metric("TE (기대값)", f"{edge.te:+.3f}%")
    e2.metric("RR (손익비)", f"{edge.rr:.2f}" if edge.rr is not None else "N/A")
    e3.metric("승률", f"{edge.win_rate:.1f}%", delta=f"{edge.win_days}일")
    e4.metric("평균수익", f"{edge.avg_win:+.2f}%")
    e5.metric("평균손실", f"{edge.avg_loss:+.2f}%")
    st.caption(
        f"{edge.days}일 중 상승 {edge.win_days} · 하락 {edge.loss_days} · "
        f"보합 {edge.flat_days} (보합은 승·패 어느 쪽에도 넣지 않으므로 "
        f"승률 {edge.win_rate:.1f}% + 패율 {edge.loss_rate:.1f}%가 "
        f"100%가 되지 않을 수 있습니다)"
    )
except ValueError as e:
    st.warning(f"기대값 계산 실패: {e}")

# ── 포지션 ──────────────────────────────────────────────────
st.markdown("### ■ 포지션")

p1, p2, p3, p4 = st.columns(4)
p1.metric(f"ATR ({core.ATR_PERIOD}일)", f"{atr:,.0f}원",
          delta=f"현재가의 {atr / price * 100:.1f}%" if price else None)
p2.metric("1유닛 주수", f"{pos.unit_shares:,}주")
p3.metric("손절선 (진입 시)", f"{stop_loss:,.0f}원",
          delta=f"-{2 * atr:,.0f}원 (2×ATR)")
p4.metric(f"1회 리스크액 ({core.RISK_PER_TRADE:.0%})",
          f"{pos.risk_amount:,.0f}원")

if pos.unit_shares == 0:
    st.warning(
        f"총자본 {capital:,.0f}원으로는 1유닛(ATR {atr:,.0f}원 기준)을 "
        "1주도 만들 수 없습니다."
    )

# ── 유닛 진행표 ─────────────────────────────────────────────
st.markdown("### ■ 유닛 진행표")

steps = core.build_pyramid(price, atr, pos.unit_shares, capital)
if not steps:
    st.info("1유닛 주수가 0이라 진행표를 만들 수 없습니다.")
else:
    st.dataframe(
        {
            "단계": [f"{s.unit}U" for s in steps],
            "매수가": [f"{s.buy_price:,.0f}" for s in steps],
            "손절선": [f"{s.stop_loss:,.0f}" for s in steps],
            "누적 주수": [f"{s.cum_shares:,}" for s in steps],
            "누적 투입": [f"{s.cum_cost:,.0f}" for s in steps],
            "누적 투입비중": [f"{s.cum_weight_pct:.1f}%" for s in steps],
            "손절 시 총손실": [f"{s.loss_if_stopped:,.0f}" for s in steps],
            "총자본 대비": [f"{s.loss_pct:.2f}%" for s in steps],
        },
        hide_index=True, width="stretch",
    )
    st.caption(
        f"현재가를 1U 진입가로 놓고 {core.PYRAMID_ATR_STEP}×ATR "
        f"({core.PYRAMID_ATR_STEP * atr:,.0f}원)마다 1유닛씩 "
        f"최대 {core.MAX_UNITS}유닛까지 쌓은 계획표입니다. "
        "추가매수할 때마다 전체 유닛의 손절선이 (마지막 매수가 − 2×ATR)로 "
        "함께 올라갑니다."
    )

# ── 차트 ────────────────────────────────────────────────────
st.markdown("### ■ 차트")
# st.plotly_chart는 컨테이너 폭을 기본으로 채운다 (width 인자 없음).
st.plotly_chart(build_chart(df, sig, stop_loss, name))

# ── AI 의견 ─────────────────────────────────────────────────
st.markdown("### ■ AI 의견")
if st.button("AI 의견 보기"):
    st.info("아직 연결되지 않았습니다. (버튼만 준비된 상태)")
else:
    st.caption("버튼을 누를 때만 호출합니다.")
