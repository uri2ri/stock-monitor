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

CHART_DAY_OPTIONS = (20, 30, 60)   # 차트에 그릴 거래일 선택지
CHART_DAYS_DEFAULT = 30            # 신호가 20일·10일 기준이라 60일은 과하다
CHART_HEIGHT = 400                 # 모바일 기준 고정 높이
CHART_PAD = 0.05                   # y축 여백 (표시 구간 최저~최고가 ±5%)
CHASE_ATR_MULT = 0.5     # 돌파 후 이만큼 더 진행했으면 추격 구간으로 본다

# ── AI 의견 설정 ────────────────────────────────────────────
AI_MODEL = "gemini-2.5-flash"
# Gemini 2.5는 사고(thinking) 토큰도 max_output_tokens에 포함된다. 사고를 완전히
# 끄면 손익분기 RR·ATR 비중 같은 계산이 흔들리므로, 사고에 512를 떼어주고
# 본문 몫으로 1500 남짓을 남긴다.
AI_MAX_OUTPUT_TOKENS = 2048
AI_THINKING_BUDGET = 512
AI_MAX_CALLS = 10        # 세션당 호출 한도

AI_SYSTEM_PROMPT = """너는 터틀 트레이딩 규칙을 기준으로 종목 지표를
해석하는 분석가다. 매수나 매도를 권하지 않는다.
숫자가 무슨 뜻인지 설명하는 것이 유일한 역할이다.

아래 네 항목으로만 답한다. 소제목을 그대로 쓴다.

■ 지표 해석
- TE가 양수/음수인 것이 터틀 관점에서 뜻하는 바
- 승률에 대응하는 손익분기 RR = (1-승률)/승률 을
  계산해서, 실제 RR이 그보다 높은지 낮은지 명시
- ATR이 현재가의 몇 %인지 계산하고,
  변동성이 큰 편인지 작은 편인지
3~4문장.

■ 현재 국면
- 20일 고가 대비 위치 → 터틀 진입 신호 관점에서 해석
- 10일 저가 대비 여유 → 청산 신호까지의 거리
- 최근 60일 흐름이 상승/하락/횡보 중 무엇인지
2~3문장.

■ 사업 맥락
- 최근 3개월 뉴스와 공시 중 실적·사업 관련만
- 주가가 올랐다/내렸다는 기사는 제외
- 확인 안 되면 "확인된 정보 없음"이라고만 쓴다
- 출처를 함께 표기
3~5문장.

■ 진입 체크리스트
아래 세 항목에 O/X와 한 줄 근거:
1. 상승 추세인가
2. 20일 고가를 돌파했는가
3. 진입 근거로 삼을 사업 논리가 있는가
   (가격이 싸다는 건 근거가 아니다)

[금지]
- "매수 추천", "지금이 기회", "저점 매수",
  "비중 확대" 같은 표현
- 목표주가 제시
- 다른 종목 언급이나 추천
- 계산되지 않은 값을 추측으로 채우기
- 네 항목 외의 내용"""


def read_secret(name: str) -> str:
    """st.secrets 조회. 설정 파일 자체가 없으면 빈 문자열."""
    try:
        return str(st.secrets[name]).strip()
    except Exception:
        return ""


def build_ai_user_message(
    name: str,
    code: str,
    price: float,
    atr: float,
    sig: core.TrendSignal,
    edge,                 # core.Edge | None
    pos: core.Position,
    stop_loss: float,
) -> str:
    """core.py가 계산한 값을 그대로 넘긴다. AI가 다시 계산하지 않게 한다."""
    if edge is None:
        te = rr = win_rate = avg_win = avg_loss = "계산 불가"
    else:
        te = f"{edge.te:+.3f}%"
        rr = f"{edge.rr:.2f}" if edge.rr is not None else "N/A"
        win_rate = f"{edge.win_rate:.1f}%"
        avg_win = f"{edge.avg_win:+.2f}%"
        avg_loss = f"{edge.avg_loss:+.2f}%"

    return (
        f"종목: {name} ({code})\n"
        f"현재가: {price:,.0f}원\n"
        f"ATR(20, wilder): {atr:,.0f}원\n"
        f"20일 고가: {sig.high_20_prev:,.0f}원 / "
        f"10일 저가: {sig.low_10:,.0f}원\n"
        f"돌파 여부: {'돌파' if sig.breakout else '미돌파'}\n"
        f"TE: {te} / RR: {rr} / 승률: {win_rate}\n"
        f"평균수익: {avg_win} / 평균손실: {avg_loss}\n"
        f"1유닛: {pos.unit_shares:,}주 / 손절선: {stop_loss:,.0f}원\n"
        f"1회 리스크액: {pos.risk_amount:,.0f}원"
    )


def _grounding_sources(candidate) -> list[str]:
    """Google 검색 그라운딩에 실제로 쓰인 출처 (제목 + 링크)."""
    meta = getattr(candidate, "grounding_metadata", None)
    if meta is None or not meta.grounding_chunks:
        return []

    sources, seen = [], set()
    for chunk in meta.grounding_chunks:
        web = getattr(chunk, "web", None)
        if web is None or not web.uri or web.uri in seen:
            continue
        seen.add(web.uri)
        sources.append(f"- [{web.title or web.domain or web.uri}]({web.uri})")
    return sources


def request_ai_opinion(user_message: str) -> str:
    """Gemini API 호출. 구글 검색 도구를 붙여 최근 뉴스·공시를 찾게 한다.

    Raises:
        RuntimeError: 키 미설정 / 차단 / 빈 응답
        google.genai 예외: 호출 실패는 그대로 전파 (호출부에서 처리)
    """
    # 미설치 환경에서도 나머지 화면은 뜨도록 지연 import
    from google import genai
    from google.genai import types

    api_key = read_secret("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY가 설정되지 않았습니다.")

    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model=AI_MODEL,
        contents=user_message,
        config=types.GenerateContentConfig(
            system_instruction=AI_SYSTEM_PROMPT,
            max_output_tokens=AI_MAX_OUTPUT_TOKENS,
            thinking_config=types.ThinkingConfig(
                thinking_budget=AI_THINKING_BUDGET
            ),
            tools=[types.Tool(google_search=types.GoogleSearch())],
        ),
    )

    if not response.candidates:
        blocked = getattr(response, "prompt_feedback", None)
        raise RuntimeError(f"응답이 차단되었습니다. ({blocked})")

    candidate = response.candidates[0]
    finish = getattr(candidate.finish_reason, "name", str(candidate.finish_reason))

    text = (response.text or "").strip()
    if not text:
        # 사고 토큰만 쓰고 본문이 안 나온 경우가 여기에 해당한다
        raise RuntimeError(f"빈 응답을 받았습니다. (종료 사유: {finish})")

    if finish == "MAX_TOKENS":
        text += (
            f"\n\n_(응답이 {AI_MAX_OUTPUT_TOKENS} 토큰에서 잘렸습니다.)_"
        )
    elif finish not in ("STOP", "FINISH_REASON_UNSPECIFIED"):
        text += f"\n\n_(종료 사유: {finish})_"

    sources = _grounding_sources(candidate)
    if sources:
        text += "\n\n**검색 출처**\n" + "\n".join(sources)
    return text

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

CHART_BAND = "rgba(128,128,128,0.18)"    # 고가–저가 음영 밴드
CHART_SIGNAL_BAND = "rgba(128,128,128,0.10)"   # 신호 계산 구간(최근 20거래일)
CHART_GRID = "rgba(128,128,128,0.2)"     # 격자선
CHART_HIGH = "#4ade80"                   # 20일 고가
CHART_LOW = "#f87171"                    # 10일 저가
CHART_STOP = "#fb923c"                   # 손절선


def chart_palette(theme_type: str | None) -> dict[str, str]:
    """차트 색.

    수평선·음영은 양쪽 테마에서 그대로 보이는 값을 쓰고, 종가 실선과
    글자만 테마 대비색으로 바꾼다 (다크=흰색, 라이트=검정).
    """
    dark = theme_type == "dark"
    return {
        "template": "plotly_dark" if dark else "plotly_white",
        "close": "#ffffff" if dark else "#000000",
        "text": "#e9ecef" if dark else "#212529",
        "band": CHART_BAND,
        "signal_band": CHART_SIGNAL_BAND,
        "grid": CHART_GRID,
        "high": CHART_HIGH,
        "low": CHART_LOW,
        "stop": CHART_STOP,
    }


def current_theme() -> str | None:
    """실행 중인 세션의 테마. 알 수 없으면 None."""
    try:
        return st.context.theme.type
    except Exception:
        return None


def build_chart(
    df,
    sig: core.TrendSignal,
    stop_loss: float,
    palette: dict,
    days: int = CHART_DAYS_DEFAULT,
) -> tuple[go.Figure, bool]:
    """표시 구간 `days` 거래일 차트.

    y축은 표시 구간의 가격 데이터(최저가~최고가 ±5%)로 잡는다. 손절선이
    그 범위 밖이면 선을 그리지 않는다 — 손절선 하나 때문에 y축이 늘어나
    가격 움직임이 눌리는 것을 막기 위해서다 (호출부가 캡션으로 알린다).

    Returns:
        (figure, 손절선을 차트에 그렸는지 여부)
    """
    d = df.tail(days)
    x = d.index

    # y축 범위 – 가격 데이터 기준
    low_min = float(d["저가"].min())
    high_max = float(d["고가"].max())
    y_low = low_min * (1 - CHART_PAD)
    y_high = high_max * (1 + CHART_PAD)

    fig = go.Figure()

    # 고가–저가 밴드: 저가를 먼저 깔고 고가에서 그 사이를 채운다
    fig.add_trace(go.Scatter(
        x=x, y=d["저가"], mode="lines", line=dict(width=0),
        hovertemplate="저가 %{y:,.0f}원<extra></extra>",
        showlegend=False, name="저가",
    ))
    fig.add_trace(go.Scatter(
        x=x, y=d["고가"], mode="lines", line=dict(width=0),
        fill="tonexty", fillcolor=palette["band"],
        hovertemplate="고가 %{y:,.0f}원<extra></extra>",
        name="고가–저가 범위",
    ))
    fig.add_trace(go.Scatter(
        x=x, y=d["종가"], name="종가", mode="lines",
        line=dict(color=palette["close"], width=2),
        hovertemplate="종가 %{y:,.0f}원<extra></extra>",
    ))
    fig.add_trace(go.Scatter(
        x=[x[-1]], y=[sig.current_price], name="현재 종가", mode="markers",
        marker=dict(color=palette["close"], size=10, symbol="circle"),
        hovertemplate="현재 종가 %{y:,.0f}원<extra></extra>",
    ))

    # 신호 계산에 쓰인 최근 20거래일 – 어디를 보고 판정했는지 표시
    signal_start = x[-min(core.HIGH_PERIOD, len(d))]
    fig.add_vrect(
        x0=signal_start, x1=x[-1],
        fillcolor=palette["signal_band"], line_width=0, layer="below",
        annotation_text=f"신호 구간 {core.HIGH_PERIOD}일",
        annotation_position="top left",
        annotation_font=dict(color=palette["text"], size=10),
    )

    # 수평선 – 라벨은 배경 없이 선과 같은 색 글자로
    lines = [
        (sig.high_20, palette["high"], f"20일 고가 {sig.high_20:,.0f}", "top right"),
        (sig.low_10, palette["low"], f"10일 저가 {sig.low_10:,.0f}", "bottom left"),
    ]
    stop_in_range = y_low <= stop_loss <= y_high
    if stop_in_range:
        # 라벨을 가운데에 둔다. 손절선이 20일 고가나 10일 저가에 가까울 때
        # 좌·우 끝에 두면 그쪽 라벨과 겹쳐 읽을 수 없게 된다.
        lines.append(
            (stop_loss, palette["stop"], f"손절선 {stop_loss:,.0f}", "bottom")
        )

    for value, color, label, position in lines:
        fig.add_hline(
            y=value, line=dict(color=color, width=1.5, dash="dash"),
            annotation_text=label, annotation_position=position,
            annotation_font=dict(color=color, size=11),
        )

    fig.update_layout(
        template=palette["template"],
        height=CHART_HEIGHT, hovermode="x unified",
        margin=dict(l=8, r=8, t=8, b=8),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color=palette["text"], size=12),
        legend=dict(
            orientation="h", yanchor="top", y=-0.12, x=0,
            bgcolor="rgba(0,0,0,0)",
        ),
        xaxis=dict(title=None, showgrid=False,
                   gridcolor=palette["grid"], linecolor=palette["grid"],
                   tickfont=dict(size=11)),
        yaxis=dict(title=None, tickformat=",.0f", range=[y_low, y_high],
                   gridcolor=palette["grid"], zeroline=False,
                   tickfont=dict(size=11)),
    )
    return fig, stop_in_range


# ── 화면 ────────────────────────────────────────────────────

st.title("📊 종목 진입 점검")
st.caption("신규 종목 조사 전용 · 터틀 트레이딩 규칙을 그대로 계산해 보여줍니다.")

# 앱이 공개라 AI 호출만 비밀번호로 막는다. 조회·차트·계산은 누구나 쓸 수 있다.
with st.sidebar:
    st.subheader("🔐 접근")
    _app_password = read_secret("APP_PASSWORD")
    _entered = st.text_input(
        "비밀번호", type="password",
        help="AI 의견 기능에만 필요합니다.",
    )
    ai_unlocked = bool(_app_password) and _entered == _app_password

    if ai_unlocked:
        st.success("AI 의견 사용 가능")
    elif not _app_password:
        st.caption("APP_PASSWORD 미설정 — AI 의견을 쓸 수 없습니다.")
    elif _entered:
        st.error("비밀번호가 일치하지 않습니다.")

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
    edge = None          # AI 의견에 "계산 불가"로 넘긴다
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

chart_span, chart_theme = st.columns(2)
with chart_span:
    chart_days = st.radio(
        "표시 기간",
        CHART_DAY_OPTIONS,
        index=CHART_DAY_OPTIONS.index(CHART_DAYS_DEFAULT),
        format_func=lambda d: f"{d}일",
        horizontal=True,
    )
with chart_theme:
    # 자동 감지가 비어 있는 환경(구버전 등)에서 선이 배경에 묻히는 것을
    # 사용자가 직접 되돌릴 수 있게 둔다.
    theme_choice = st.radio(
        "차트 색", ("자동", "밝게", "어둡게"), horizontal=True,
    )

palette = chart_palette(
    {"자동": current_theme(), "밝게": "light", "어둡게": "dark"}[theme_choice]
)

fig, stop_in_range = build_chart(df, sig, stop_loss, palette, chart_days)

# st.plotly_chart는 컨테이너 폭을 기본으로 채운다 (width 인자 없음).
# 모바일에서는 모드바가 범례·라벨 위에 겹쳐 뜨므로 숨긴다.
st.plotly_chart(fig, config={"displayModeBar": False, "displaylogo": False})

st.caption(f"{name} · 최근 {min(len(df), chart_days)}거래일")
if not stop_in_range:
    gap_pct = (stop_loss - price) / price * 100 if price else 0.0
    st.caption(
        f"손절선 {stop_loss:,.0f} "
        f"(현재가 대비 {gap_pct:+.1f}%, 차트 범위 밖)"
    )

# ── AI 의견 ─────────────────────────────────────────────────
st.markdown("### ■ AI 의견")

ai_cache = st.session_state.setdefault("ai_cache", {})
ai_calls = st.session_state.setdefault("ai_calls", 0)
# 총자본이 바뀌면 1유닛·리스크액도 달라지므로 캐시 키에 함께 넣는다.
ai_key = (code, int(capital))
quota_left = AI_MAX_CALLS - ai_calls

if ai_key in ai_cache:
    st.caption("이미 조회한 종목입니다. 저장된 결과를 보여줍니다.")
elif not ai_unlocked:
    st.info("AI 의견은 비밀번호 입력 후 사용 가능")
elif quota_left <= 0:
    st.warning(
        f"이번 세션 호출 한도({AI_MAX_CALLS}회)를 모두 사용했습니다. "
        "새로고침하면 초기화됩니다."
    )
else:
    st.caption(f"버튼을 누를 때만 호출합니다. (남은 호출 {quota_left}회)")

if st.button(
    "AI 의견 보기",
    disabled=(not ai_unlocked) or quota_left <= 0,
    help=None if ai_unlocked else "AI 의견은 비밀번호 입력 후 사용 가능",
):
    if ai_key in ai_cache:
        st.caption("캐시된 결과입니다. (API를 다시 호출하지 않았습니다)")
    else:
        with st.spinner("AI가 지표와 최근 뉴스·공시를 확인하는 중…"):
            try:
                ai_cache[ai_key] = request_ai_opinion(
                    build_ai_user_message(
                        name, code, price, atr, sig, edge, pos, stop_loss
                    )
                )
                st.session_state["ai_calls"] = ai_calls + 1
            except Exception as e:
                # 실패해도 위쪽 리포트는 그대로 유지된다
                st.error(f"AI 의견 실패: {e}")

if ai_key in ai_cache:
    st.markdown(ai_cache[ai_key])
    st.caption(
        "AI 해석입니다. 매매 판단은 노션에 정해둔 규칙과 손절선을 따르세요."
    )
