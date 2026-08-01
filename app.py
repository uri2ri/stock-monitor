"""
app.py – 신규 종목 조사용 Streamlit 앱

계산은 전부 core.py에 있다. 이 파일은 입력을 받아 core를 호출하고
결과를 그리기만 한다. 계산식을 여기서 다시 구현하지 않는다.

보유종목·매수단가는 다루지 않는다 (신규 종목 조사 전용).

로컬 실행:
    streamlit run app.py
"""

from __future__ import annotations

from datetime import date

import plotly.graph_objects as go
import streamlit as st
from pykrx import stock as krx

import core
import screener

TAB_ANALYSIS = "종목 분석"
TAB_BREAKOUT = "오늘의 돌파"

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

SECTOR_SYSTEM_PROMPT = """너는 돌파 스크리너를 통과한 종목 목록을 보고
어느 업종에 몰렸는지, 그 배경에 어떤 사업적 사실이 있었는지만
설명하는 분석가다. 매수나 매도를 권하지 않는다.

아래 두 항목으로만 답한다. 소제목을 그대로 쓴다.

■ 섹터 분포
어느 업종에 돌파가 몰렸는지, 그 업종에 최근 어떤 사업적 이슈가
있었는지. 웹 검색으로 확인한다. 확인 안 되면 "확인된 정보 없음".
3~4문장.

■ 개별 배경
목록 상위 5종목 각각 왜 올랐는지 한 줄씩.
공시·수주·실적 같은 사업 이유만 쓴다.
확인 안 되면 "확인된 정보 없음".

[금지]
- 어느 종목을 사라고 권하는 표현
- 목록에 없는 종목 언급
- 목표주가 제시
- 순위 매기기나 "가장 유망한" 같은 표현
- "급등", "테마 부각" 같은 표현
- 두 항목 외의 내용"""


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
        f"직전 20일 고가: {sig.high_20_prev:,.0f}원 / "
        f"청산선(직전 10일 저가): {sig.low_10_prev:,.0f}원\n"
        f"돌파 여부: {'돌파' if sig.breakout else '미돌파'}\n"
        f"TE: {te} / RR: {rr} / 승률: {win_rate}\n"
        f"평균수익: {avg_win} / 평균손실: {avg_loss}\n"
        f"1유닛: {pos.unit_shares:,}주 / 손절선: {stop_loss:,.0f}원\n"
        f"1회 리스크액: {pos.risk_amount:,.0f}원"
    )


# ── 상관군 유닛 카운터 ──────────────────────────────────────
MAX_UNITS_STOCK = 4       # 종목당
MAX_UNITS_GROUP = 6       # 상관군당
MAX_UNITS_TOTAL = 12      # 전체


@st.cache_data(ttl=300, show_spinner=False)
def load_corr_units(capital: int) -> tuple[dict[str, float], float, list[str]]:
    """노션 보유 종목의 상관군별 누적 유닛수.

    유닛은 진입 시점 기준이므로 '진입시 ATR'을 우선 쓰고, 없으면 노션의
    ATR 칸(매일 갱신되는 값)으로 대신한다. 둘 다 없으면 셀 수 없으므로
    건너뛰고 그 사실을 함께 돌려준다.

    Returns:
        (상관군별 유닛수, 전체 유닛수, 셀 수 없어 건너뛴 종목명)
    """
    import os

    for key in ("NOTION_TOKEN", "NOTION_DB_ID"):
        value = read_secret(key)
        if value:
            os.environ[key] = value

    from notion_repo import fetch_holdings

    groups: dict[str, float] = {}
    total = 0.0
    skipped: list[str] = []

    for _, inp in fetch_holdings():
        atr = inp.entry_atr or inp.notion_atr
        if not atr:
            skipped.append(f"{inp.name}(ATR 없음)")
            continue
        if not inp.shares:
            skipped.append(f"{inp.name}(보유수량 없음)")
            continue
        unit_shares = core.calc_position(atr, capital).unit_shares
        if unit_shares <= 0:
            # 1ATR이 계좌 1%보다 커서 1유닛이 0주 — 계좌 규모에 안 맞는 종목
            skipped.append(f"{inp.name}(1유닛 0주)")
            continue
        units = inp.shares / unit_shares
        total += units
        group = (inp.corr_group or "").strip()
        if group:
            groups[group] = groups.get(group, 0.0) + units

    return groups, total, skipped


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


def request_ai_opinion(
    user_message: str, system_prompt: str = AI_SYSTEM_PROMPT,
) -> str:
    """Gemini API 호출. 구글 검색 도구를 붙여 최근 뉴스·공시를 찾게 한다.

    Args:
        user_message: core.py / screener.py가 계산한 값
        system_prompt: 종목 의견(기본)과 섹터 해설이 같은 호출부를 쓴다

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
            system_instruction=system_prompt,
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

    ATR이 0으로 반올림되면(저가·저변동 종목) ATR 배수를 낼 수 없다.
    돌파 판정 자체는 가격 비교라 그대로 두고 배수 표기만 뺀다 — core가
    atr<=0에서 주수·진행표를 비우고 추정하지 않는 것과 같은 태도다.
    """
    gap = sig.current_price - sig.high_20_prev
    mult = f", {gap / atr:.1f}×ATR" if atr > 0 else ""

    if atr > 0 and gap > CHASE_ATR_MULT * atr:
        return "추격금지", (
            f"직전 20일 고가보다 {gap:,.0f}원 위 "
            f"({gap / atr:.1f}×ATR) — 돌파 시점에서 이미 벗어남"
        )
    if sig.breakout:
        return "진입가능", (
            f"직전 20일 고가 {sig.high_20_prev:,.0f} 돌파 "
            f"(+{gap:,.0f}원{mult})"
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

    # y축 범위 – 가격 데이터 기준. 신호선은 표시 구간 바로 앞 봉에서 나올
    # 수도 있으므로(예: 20일 표시 + 직전 20일 고가) 범위에 함께 넣는다.
    low_min = min(float(d["저가"].min()), sig.low_10_prev)
    high_max = max(float(d["고가"].max()), sig.high_20_prev)
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

    # 수평선 – 라벨은 배경 없이 선과 같은 색 글자로.
    # 판정에 실제로 쓰는 값(당일 제외)을 그린다. 당일을 포함한 high_20/low_10을
    # 그리면 돌파 당일에 선이 종가 위로 올라가, 화면은 "돌파"인데 차트는
    # 미돌파로 보이는 모순이 생긴다.
    lines = [
        (sig.high_20_prev, palette["high"],
         f"직전 20일 고가 {sig.high_20_prev:,.0f}", "top right"),
        (sig.low_10_prev, palette["low"],
         f"직전 10일 저가 {sig.low_10_prev:,.0f}", "bottom left"),
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

    st.divider()
    st.subheader("💰 계좌")
    # 종목을 바꿔도 값이 남도록 session_state로 유지한다.
    # value=와 key=를 함께 주면 Streamlit이 경고하므로 초기값만 넣어둔다.
    st.session_state.setdefault("capital", core.DEFAULT_CAPITAL)
    capital = st.number_input(
        "계좌 금액 (원)",
        min_value=1_000_000, max_value=100_000_000_000,
        step=1_000_000, format="%d", key="capital",
    )
    st.caption(f"1회 리스크 한도: {capital * core.RISK_PER_TRADE:,.0f}원 (1%)")


# ── [종목 분석] 화면 ────────────────────────────────────────

def render_analysis(capital: float, ai_unlocked: bool) -> None:
    """[종목 분석] 화면. 종목코드 하나를 받아 전체 리포트를 그린다.

    탭으로 나뉘면서 함수가 됐다. 예전에는 st.stop()으로 화면을 끊었는데,
    탭 안에서 그러면 다른 탭까지 같이 멈추므로 return으로 바꿨다.
    """
    col_code, col_btn = st.columns([4, 1])
    with col_code:
        # key를 준 이유: 돌파 탭에서 종목을 고르면 콜백이 이 칸을 채운다
        code = st.text_input(
            "종목코드 (6자리)", placeholder="예: 240550", max_chars=6,
            key="code_input",
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
            return
        st.session_state["query"] = entered

    if "query" not in st.session_state:
        return

    # 계좌 금액은 사이드바 값을 그대로 쓴다 – 바꾸면 재조회 없이 즉시 반영된다
    code = st.session_state["query"]

    with st.spinner("시세 조회 중…"):
        try:
            df = load_ohlcv(code)
            # ATR을 정수로 반올림한 뒤 모든 하위 계산을 수행한다 (지표·진행표·
            # 노션 기록값이 항상 같아야 하므로).
            atr = core.round_atr(core.calc_atr(df))
            price = core.latest_close(df)
            sig = core.trend_signals(df)
            pos = core.calc_position(atr, capital)
            stop_loss = int(core.calc_stop(price, atr))
        except Exception as e:
            st.error(f"조회 실패: {e}")
            return

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
    c4.metric("직전 10일 저가 (청산선)", f"{sig.low_10_prev:,.0f}원")
    c5.metric("청산선까지 거리", f"{sig.dist_to_exit_pct:.1f}%",
              delta=f"{price - sig.low_10_prev:+,.0f}원")
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
    st.markdown(f"### ■ 기대값 (종목 변동성 기준, 최근 {core.EDGE_DAYS}일)")

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
        st.caption("⚠️ 내 실제 매매 성적이 아님 — 종목의 최근 등락 분포입니다.")
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
    # 1N 움직였을 때의 손익이 아니라, 손절선에 닿았을 때 실제로 잃는 금액.
    # 유닛 진행표 1U 행의 손실액과 같은 값이어야 한다.
    unit_loss = pos.unit_shares * 2 * atr
    p4.metric("1유닛 손절 시", f"-{unit_loss:,.0f}원",
              delta=f"계좌 {unit_loss / capital * 100:.2f}%" if capital else None,
              delta_color="inverse")

    with p2:
        st.caption(f"└ 계좌 1% ÷ 1ATR (터틀 원본)")

    if pos.unit_shares == 0:
        st.warning(
            f"**진입 불가 — 1주도 리스크 한도 초과**  \n"
            f"1ATR {atr:,.0f}원이 계좌 1%({capital * core.RISK_PER_TRADE:,.0f}원)보다 "
            "큽니다. 이 종목은 현재 계좌 규모에 맞지 않습니다."
        )

    # ── 상관군 유닛 카운터 ──────────────────────────────────────
    st.markdown("### ■ 상관군 유닛")
    st.caption(
        f"종목당 {MAX_UNITS_STOCK}유닛 · 상관군당 {MAX_UNITS_GROUP}유닛 · "
        f"전체 {MAX_UNITS_TOTAL}유닛 상한"
    )

    # 노션 조회가 실패해도 이 섹션만 비고 나머지 화면은 그대로 남는다
    try:
        corr_groups, corr_total, corr_skipped = load_corr_units(int(capital))
    except Exception as e:
        st.info(f"보유 현황을 불러오지 못해 상관군 집계를 건너뜁니다. ({e})")
    else:
        known = sorted(corr_groups)
        pick_col, info_col = st.columns([1, 2])
        with pick_col:
            choice = st.selectbox(
                "이 종목의 상관군", known + ["(직접 입력)", "(없음)"],
                index=len(known) if known else 0,
            )
            group = (
                st.text_input("상관군 이름", key="corr_manual").strip()
                if choice == "(직접 입력)"
                else ("" if choice == "(없음)" else choice)
            )

        with info_col:
            used = corr_groups.get(group, 0.0) if group else 0.0
            after = used + 1

            if group:
                st.metric(
                    f"{group} 상관군",
                    f"현재 {used:g}유닛 / {MAX_UNITS_GROUP}",
                    delta=f"1유닛 진입 시 {after:g}/{MAX_UNITS_GROUP}",
                    delta_color="off",
                )
                if used >= MAX_UNITS_GROUP:
                    st.error(f"상관군 상한 도달 — 이 종목은 진입 불가")
                elif after >= MAX_UNITS_GROUP:
                    st.warning(
                        f"이 종목 1유닛 진입 시 {after:g}/{MAX_UNITS_GROUP}, "
                        "이후 추가 불가"
                    )
            else:
                st.caption("상관군을 고르면 누적 유닛을 확인할 수 있습니다.")

            total_after = corr_total + 1
            if corr_total >= MAX_UNITS_TOTAL:
                st.error(f"전체 {corr_total:g}/{MAX_UNITS_TOTAL}유닛 — 상한 도달")
            elif total_after > MAX_UNITS_TOTAL:
                st.warning(f"전체 {corr_total:g}/{MAX_UNITS_TOTAL}유닛 — 1유닛 더 넣으면 초과")
            else:
                st.caption(f"전체 {corr_total:g}/{MAX_UNITS_TOTAL}유닛")

        if corr_skipped:
            st.caption("유닛을 셀 수 없어 제외: " + ", ".join(corr_skipped))

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


# ── [오늘의 돌파] 화면 ──────────────────────────────────────

# 스캔 기준일이 이만큼 지나면 "최신 결과 없음"으로 안내한다.
# 연휴가 끼면 마지막 거래일이 4일 전일 수 있어(예: 금요일 스캔 → 화요일
# 아침에 확인) 그보다 넉넉히 잡았다. 앱에서 휴장일을 계산하지는 않는다.
BREAKOUT_STALE_DAYS = 5


def _open_analysis(ticker: str) -> None:
    """돌파 목록에서 고른 종목을 [종목 분석] 탭에서 연다.

    on_click 콜백으로 부른다. 콜백은 스크립트가 다시 돌기 전에 실행되므로
    위젯 key("tab", "code_input")를 여기서 바꿀 수 있다. 렌더링 중에
    같은 값을 바꾸면 Streamlit이 예외를 던진다.
    """
    st.session_state["query"] = ticker
    st.session_state["code_input"] = ticker
    st.session_state["tab"] = TAB_ANALYSIS


def build_sector_user_message(result: dict) -> str:
    """스크리너가 낸 값만 넘긴다. AI가 새 종목을 만들어내지 않게 한다."""
    stocks = result["stocks"]

    counts: dict[str, int] = {}
    for s in stocks:
        counts[s["sector"]] = counts.get(s["sector"], 0) + 1
    dist = "\n".join(
        f"- {sector} {n}종목"
        for sector, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    )

    rows = "\n".join(
        f"- {s['sector']} | {s['name']}({s['ticker']}) | "
        f"거래량 {s['vol_mult']:.1f}배"
        for s in stocks
    )

    return (
        f"스캔 기준일: {result['scan_date']}\n"
        f"총 {result['total_scanned']:,}종목 중 {result['passed']}종목이 "
        "20일 고가 돌파 + 거래량 증가 조건을 통과했다.\n\n"
        f"업종별 분포:\n{dist}\n\n"
        f"종목 목록 (거래량 배수 내림차순):\n{rows}"
    )


def _breakout_freshness(result: dict) -> tuple[bool, str]:
    """(최신인가, 안내 문구). 날짜를 못 읽으면 최신이 아닌 것으로 본다."""
    shown = f"{result['scan_date']} 스캔 · 파일 {result['_file']} " \
            f"(저장 {result['_mtime'].replace('T', ' ')})"
    try:
        scanned = date.fromisoformat(result["scan_date"])
    except Exception:
        return False, f"스캔 기준일을 읽을 수 없습니다. {shown}"

    age = (date.today() - scanned).days
    if age > BREAKOUT_STALE_DAYS:
        return False, f"{age}일 전 결과입니다. {shown}"
    return True, shown


def render_breakout(capital: float, ai_unlocked: bool) -> None:
    """[오늘의 돌파] 화면. screener.py가 저장한 JSON을 읽어 보여준다.

    여기서는 계산하지 않는다. 조건을 완화하지도 않는다 — 통과 종목이
    없으면 없다고만 쓴다.
    """
    result = screener.latest_result()
    if result is None:
        st.info(
            "**최신 스캔 결과 없음** — `data/`에 `breakout_*.json`이 없습니다.  \n"
            "장 마감 후 스크리너가 돌면 생깁니다 "
            "(GitHub Actions 평일 16:30 KST)."
        )
        return

    fresh, shown = _breakout_freshness(result)
    st.subheader(f"스캔 기준일 {result['scan_date']}")
    if fresh:
        st.caption(shown)
    else:
        st.warning(f"**최신 스캔 결과 없음** — {shown}")

    stocks = result["stocks"]
    m1, m2, m3 = st.columns(3)
    m1.metric("스캔 종목", f"{result['total_scanned']:,}종목")
    m2.metric("조건 통과", f"{result['passed']}종목")
    m3.metric("표시", f"{len(stocks)}종목")
    st.caption(
        f"총 {result['total_scanned']:,}종목 중 {result['passed']}종목 통과 · "
        f"조회 경로: {result.get('source', '알 수 없음')}"
    )

    # 스캔이 무엇을 못 했는지 숨기지 않는다 (관리종목 필터 생략 등)
    for note in result.get("notes", []):
        st.caption(f"· {note}")

    if not stocks:
        st.info("오늘 조건을 통과한 종목이 없습니다.")
        return

    # ── 업종별 분포 ────────────────────────────────────────
    counts: dict[str, int] = {}
    for s in stocks:
        counts[s["sector"]] = counts.get(s["sector"], 0) + 1
    order = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))

    st.markdown("### ■ 업종 분포")
    st.caption("종목 수가 많은 업종부터 — 어느 섹터에 돌파가 몰렸는지 봅니다.")
    st.write(" · ".join(f"**{sector}** {n}" for sector, n in order))

    # ── 표 (업종별로 묶고, 종목 수 많은 업종을 위로) ────────
    rank = {sector: i for i, (sector, _) in enumerate(order)}
    rows = sorted(
        stocks, key=lambda s: (rank[s["sector"]], -s["vol_mult"])
    )

    st.markdown("### ■ 통과 종목")
    st.dataframe(
        {
            "종목명": [s["name"] for s in rows],
            "업종": [s["sector"] for s in rows],
            "현재가": [f"{s['price']:,.0f}" for s in rows],
            "거래량배수": [f"{s['vol_mult']:.1f}배" for s in rows],
            "ATR%": [f"{s['atr_pct']:.1f}%" for s in rows],
            "1유닛 주수": [f"{s['unit_shares']:,}주" for s in rows],
            "손절선": [f"{s['stop_loss']:,}" for s in rows],
        },
        hide_index=True, width="stretch",
    )
    st.caption(
        f"ATR%는 현재가 대비입니다. 1유닛·손절선은 스캔 시점 계좌 "
        f"{result['capital']:,.0f}원 기준이라, 사이드바 금액과 다르면 "
        "[종목 분석] 탭에서 다시 계산됩니다."
    )

    # ── 종목 선택 → [종목 분석] 탭 ─────────────────────────
    st.markdown("### ■ 종목 열기")
    pick_col, btn_col = st.columns([3, 1])
    with pick_col:
        labels = {
            f"{s['name']} ({s['ticker']}) · {s['sector']}": s["ticker"]
            for s in rows
        }
        chosen = st.selectbox("리포트를 볼 종목", list(labels))
    with btn_col:
        st.write("")
        st.button(
            "종목 분석에서 열기", type="primary", width="stretch",
            on_click=_open_analysis, args=(labels[chosen],),
        )

    # ── 섹터 해설 (AI) ─────────────────────────────────────
    st.markdown("### ■ 섹터 해설")

    ai_cache = st.session_state.setdefault("ai_cache", {})
    ai_calls = st.session_state.setdefault("ai_calls", 0)
    # 같은 스캔 결과면 다시 부르지 않는다. 종목 구성이 캐시 키다.
    ai_key = ("sector", result["scan_date"], tuple(s["ticker"] for s in rows))
    quota_left = AI_MAX_CALLS - ai_calls

    if ai_key in ai_cache:
        st.caption("이미 조회한 스캔입니다. 저장된 결과를 보여줍니다.")
    elif not ai_unlocked:
        st.info("섹터 해설은 비밀번호 입력 후 사용 가능")
    elif quota_left <= 0:
        st.warning(
            f"이번 세션 호출 한도({AI_MAX_CALLS}회)를 모두 사용했습니다. "
            "새로고침하면 초기화됩니다."
        )
    else:
        st.caption(
            f"버튼을 누를 때만 호출합니다. "
            f"(AI 의견과 함께 쓰는 한도, 남은 호출 {quota_left}회)"
        )

    if st.button(
        "섹터 해설 보기",
        disabled=(not ai_unlocked) or quota_left <= 0,
        help=None if ai_unlocked else "섹터 해설은 비밀번호 입력 후 사용 가능",
    ):
        if ai_key in ai_cache:
            st.caption("캐시된 결과입니다. (API를 다시 호출하지 않았습니다)")
        else:
            with st.spinner("AI가 업종별 최근 이슈를 확인하는 중…"):
                try:
                    ai_cache[ai_key] = request_ai_opinion(
                        build_sector_user_message(result),
                        system_prompt=SECTOR_SYSTEM_PROMPT,
                    )
                    st.session_state["ai_calls"] = ai_calls + 1
                except Exception as e:
                    # 실패해도 위쪽 목록은 그대로 남는다
                    st.error(f"섹터 해설 실패: {e}")

    if ai_key in ai_cache:
        st.markdown(ai_cache[ai_key])

    st.caption(
        "조건을 통과한 목록입니다. 진입 여부와 수량은 노션에 정해둔 "
        "리스크 한도를 따르세요."
    )


# ── 탭 ──────────────────────────────────────────────────────

# st.tabs는 코드에서 다른 탭으로 넘길 수 없다. 돌파 목록에서 종목을
# 고르면 [종목 분석]으로 넘어가야 하므로, 상태를 가진 segmented_control을
# 탭 막대로 쓴다.
st.session_state.setdefault("tab", TAB_ANALYSIS)
_tab = st.segmented_control(
    "화면", (TAB_ANALYSIS, TAB_BREAKOUT), key="tab", label_visibility="collapsed",
)

# 선택을 해제하면 None이 온다. 그때는 직전 탭을 유지한다.
if _tab == TAB_BREAKOUT:
    render_breakout(capital, ai_unlocked)
else:
    render_analysis(capital, ai_unlocked)
