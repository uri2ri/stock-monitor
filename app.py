"""
app.py – 신규 종목 조사용 Streamlit 앱

계산은 전부 core.py에 있다. 이 파일은 입력을 받아 core를 호출하고
결과를 그리기만 한다. 계산식을 여기서 다시 구현하지 않는다.

보유종목·매수단가는 다루지 않는다 (신규 종목 조사 전용).

로컬 실행:
    streamlit run app.py
"""

from __future__ import annotations

import html
import io
import logging
import re
import zipfile
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from typing import Optional

import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st
from pykrx import stock as krx

import core
import notion_repo
import scan_all
import screener

TAB_ANALYSIS = "종목 분석"
TAB_BREAKOUT = "오늘의 돌파"
TAB_SCAN = "전종목 스캔"
TAB_FAVORITES = "즐겨찾기"
TAB_CALCULATOR = "계산기"

# [계산기] 오타 방지 경고 임계값 – 계산을 막지는 않고 문구만 띄운다.
CALC_DROP_RATIO = 0.1     # 진입후최고가가 매수가의 이 비율 이하면 오타 의심
CALC_RISE_MULT = 3.0      # 진입후최고가가 매수가의 이 배수 이상이면 오타 의심

CHART_DAY_OPTIONS = (20, 30, 60)   # 차트에 그릴 거래일 선택지
CHART_DAYS_DEFAULT = 30            # 신호가 20일·10일 기준이라 60일은 과하다
CHART_HEIGHT = 400                 # 모바일 기준 고정 높이
CHART_PAD = 0.05                   # y축 여백 (표시 구간 최저~최고가 ±5%)

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
두 부분으로 나눠 쓴다.
(1) 회사 개요·업종: 이 회사가 무슨 사업을 하는지, 어느 업종인지는
    네가 아는 일반 지식으로 1~2문장. 사업 구조는 시의성과 무관하다.
(2) 최근 뉴스·공시: 사용자 메시지의 [최근 N일 뉴스]·[최근 N일 공시]
    목록에 있는 내용만 근거로 쓴다. 실적·사업 관련만 남기고 주가
    등락 기사는 제외한다. 인용할 때 목록의 날짜를 함께 적는다.
    목록이 "확인된 뉴스/공시 없음"이면 그대로 "최근 N일간 확인된
    뉴스·공시 없음"이라고 쓴다. 목록에 없는 내용을 추측하거나
    학습된 지식(오래된 기사 포함)으로 채우지 않는다.
두 부분 합쳐 3~5문장.

■ 진입 체크리스트
아래 네 항목에 O/X와 한 줄 근거:
1. 상승 추세인가
2. 20일 고가를 돌파했는가
3. 진입 근거로 삼을 사업 논리가 있는가
   (가격이 싸다는 건 근거가 아니다)
4. 추격 구간이 아닌가
   (사용자 메시지의 "추격 판정" 값을 그대로 따른다. 대기·진입가능이면 O,
   추격금지면 X. 스스로 판단하지 않는다)

[금지]
- "매수 추천", "지금이 기회", "저점 매수",
  "비중 확대" 같은 표현
- 목표주가 제시
- 다른 종목 언급이나 추천
- 계산되지 않은 값을 추측으로 채우기
- 네 항목 외의 내용"""

AI_SYSTEM_PROMPT_ETF = """너는 터틀 트레이딩 규칙을 기준으로 ETF 지표를
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

■ 상품 개요
ETF는 개별 기업이 아니라 DART 공시가 없고, 이 앱은 기초지수·추종자산·
운용사 정보를 조회하지 않는다. "기초지수·추종자산·운용사 정보는 제공하지
않음 — 운용사 홈페이지나 KRX 정보데이터시스템에서 확인하세요."라고만
쓴다. 종목명으로 유추해서 채우거나 학습된 지식으로 대신 채우지 않는다.
사용자 메시지의 [최근 N일 뉴스] 목록이 있으면 실적·사업 관련 없이
가격 흐름과 관련된 것만 한 줄로 요약한다 (없으면 "확인된 뉴스 없음").

■ 진입 체크리스트
아래 네 항목에 O/X와 한 줄 근거:
1. 상승 추세인가
2. 20일 고가를 돌파했는가
3. 진입 근거로 삼을 논리가 있는가
   (가격이 싸다는 건 근거가 아니다)
4. 추격 구간이 아닌가
   (사용자 메시지의 "추격 판정" 값을 그대로 따른다. 대기·진입가능이면 O,
   추격금지면 X. 스스로 판단하지 않는다)

[금지]
- "매수 추천", "지금이 기회", "저점 매수",
  "비중 확대" 같은 표현
- 목표주가 제시
- 다른 종목 언급이나 추천
- 계산되지 않은 값을 추측으로 채우기
- 기초지수·운용사를 지어내기
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
    chase_state: str,     # entry_state()가 낸 판정 (대기/진입가능/추격금지)
) -> str:
    """core.py가 계산한 값을 그대로 넘긴다. AI가 다시 계산하지 않게 한다.

    chase_state는 체크리스트 "추격 구간이 아닌가" 항목을 AI가 스스로
    판단하지 않고 이미 계산된 값을 그대로 따르게 하려고 넣는다 — 갭이
    CHASE_ATR_MULT를 넘은 종목도 체크리스트가 전부 O로 나와 화면 상단의
    추격금지 배너와 어긋나던 문제를 막는다.
    """
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
        f"추격 판정: {chase_state}\n"
        f"TE: {te} / RR: {rr} / 승률: {win_rate}\n"
        f"평균수익: {avg_win} / 평균손실: {avg_loss}\n"
        f"1유닛: {pos.unit_shares:,}주 / 손절선: {stop_loss:,.0f}원\n"
        f"1회 리스크액: {pos.risk_amount:,.0f}원"
    )


# ── 최근 뉴스·공시 (AI 의견의 "사업 맥락" 소스 고정용) ────────
#
# google_search 그라운딩은 구글 전체를 검색해서 소스를 고를 수 없다.
# 대신 네이버페이증권 종목뉴스 탭과 DART 공시 목록을 직접 스크래핑해서
# "최근 N일 안에 실제로 있었던 뉴스·공시"만 프롬프트에 넣는다.

RECENT_ACTIVITY_DAYS = 30

NAVER_NEWS_URL = "https://finance.naver.com/item/news_news.naver"
NAVER_NEWS_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)
NAVER_NEWS_MAX_PAGES = 5   # 30일 커버에 보통 1~2페이지, 안전판으로 상한
NAVER_NEWS_ROW_RE = re.compile(
    r'<a href="(/item/news_read\.naver\?[^"]+)" class="tit"[^>]*>([^<]+)</a>'
    r'.*?<td class="info">([^<]*)</td>\s*<td class="date">\s*([^<]+)</td>',
    re.S,
)

DART_API_KEY_NAME = "DART_API_KEY"
DART_CORP_LIST_URL = "https://opendart.fss.or.kr/api/corpCode.xml"
DART_DISCLOSURE_URL = "https://opendart.fss.or.kr/api/list.json"


def fetch_naver_news(code: str, days: int = RECENT_ACTIVITY_DAYS) -> list[tuple[str, str, str]]:
    """네이버페이증권 종목뉴스 탭에서 최근 N일 기사(날짜, 제목, 링크)를 가져온다.

    이 URL은 Referer 없이 호출하면 빈 목록만 돌려준다 (iframe으로만 열리게
    막아둔 것으로 보인다) — main.naver 페이지를 Referer로 넣어 우회한다.
    기사는 최신순으로 오므로, 기준일보다 오래된 기사가 나오면 그 페이지까지만
    쓰고 멈춘다.

    Raises:
        requests.RequestException: 호출 실패는 그대로 전파 (호출부에서 처리)
    """
    cutoff = datetime.now() - timedelta(days=days)
    headers = {
        "User-Agent": NAVER_NEWS_USER_AGENT,
        "Referer": f"https://finance.naver.com/item/main.naver?code={code}",
    }

    articles: list[tuple[str, str, str]] = []
    for page in range(1, NAVER_NEWS_MAX_PAGES + 1):
        resp = requests.get(
            NAVER_NEWS_URL, params={"code": code, "page": page},
            headers=headers, timeout=5,
        )
        resp.encoding = "euc-kr"
        matches = NAVER_NEWS_ROW_RE.findall(resp.text)
        if not matches:
            break

        reached_cutoff = False
        for href, title, source, date_str in matches:
            try:
                dt = datetime.strptime(date_str.strip(), "%Y.%m.%d %H:%M")
            except ValueError:
                continue
            if dt < cutoff:
                reached_cutoff = True
                break
            link = "https://finance.naver.com" + html.unescape(href)
            articles.append((dt.strftime("%Y-%m-%d"), html.unescape(title.strip()), link))
        if reached_cutoff:
            break

    return articles


@st.cache_data(ttl=86_400, show_spinner=False)
def _dart_corp_code_map(api_key: str) -> dict[str, str]:
    """DART 종목코드→corp_code 매핑.

    DART API는 종목코드로 바로 조회할 수 없고 자체 corp_code가 필요하다.
    이 매핑은 전종목 목록(zip)으로만 받을 수 있어 24시간 캐싱한다.
    """
    resp = requests.get(DART_CORP_LIST_URL, params={"crtfc_key": api_key}, timeout=15)
    resp.raise_for_status()

    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        xml_bytes = zf.read(zf.namelist()[0])

    mapping: dict[str, str] = {}
    for item in ET.fromstring(xml_bytes).iter("list"):
        stock_code = (item.findtext("stock_code") or "").strip()
        corp_code = (item.findtext("corp_code") or "").strip()
        if stock_code:
            mapping[stock_code] = corp_code
    return mapping


def fetch_dart_disclosures(
    code: str, days: int = RECENT_ACTIVITY_DAYS,
) -> list[tuple[str, str]] | None:
    """DART 공시 목록(날짜, 제목) 중 최근 N일치.

    Returns:
        None: DART_API_KEY 미설정 (호출부가 "미조회"와 "결과 없음"을
              구분해서 보여준다)
        []: 키는 있으나 최근 N일 안에 공시가 없음

    Raises:
        requests.RequestException / RuntimeError: 호출 실패는 그대로 전파
    """
    api_key = read_secret(DART_API_KEY_NAME)
    if not api_key:
        return None

    corp_code = _dart_corp_code_map(api_key).get(code)
    if not corp_code:
        return []

    end = datetime.now()
    start = end - timedelta(days=days)
    resp = requests.get(
        DART_DISCLOSURE_URL,
        params={
            "crtfc_key": api_key,
            "corp_code": corp_code,
            "bgn_de": start.strftime("%Y%m%d"),
            "end_de": end.strftime("%Y%m%d"),
            "page_count": 100,
        },
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()

    if data.get("status") == "013":
        return []          # "조회된 데이터가 없습니다" — 정상적인 빈 결과
    if data.get("status") != "000":
        raise RuntimeError(f"DART 조회 실패: {data.get('message')}")

    return [
        (datetime.strptime(item["rcept_dt"], "%Y%m%d").strftime("%Y-%m-%d"), item["report_nm"])
        for item in data.get("list", [])
    ]


def build_recent_activity_block(
    code: str, is_etf: bool = False, days: int = RECENT_ACTIVITY_DAYS,
) -> tuple[str, datetime]:
    """AI 프롬프트에 넣을 "최근 뉴스·공시" 블록과 조회 시각.

    스크래핑 실패는 항목을 비우고 넘어간다 — 이 블록이 없다고 나머지
    리포트(지표 해석·현재 국면·진입 체크리스트)까지 막을 이유는 없다.

    ETF는 기업이 아니라 DART 공시가 없으므로 is_etf=True면 DART 조회
    자체를 시도하지 않는다 (불필요한 호출·에러 방지).
    """
    fetched_at = datetime.now()

    try:
        news = fetch_naver_news(code, days=days)
    except Exception:
        news = []

    lines = [f"[최근 {days}일 뉴스 — 네이버페이증권]"]
    if news:
        lines += [f"- {d} {t}" for d, t, _ in news]
    else:
        lines.append(f"- 최근 {days}일간 확인된 뉴스 없음")

    lines.append(f"\n[최근 {days}일 공시 — DART]")
    if is_etf:
        lines.append("- ETF는 개별 기업이 아니므로 DART 공시를 조회하지 않음")
    else:
        try:
            disclosures = fetch_dart_disclosures(code, days=days)
        except Exception:
            disclosures = []

        if disclosures is None:
            lines.append("- DART_API_KEY 미설정으로 조회 안 함")
        elif disclosures:
            lines += [f"- {d} {t}" for d, t in disclosures]
        else:
            lines.append(f"- 최근 {days}일간 확인된 공시 없음")

    return "\n".join(lines), fetched_at


# ── 상관군 유닛 카운터 ──────────────────────────────────────
# 상한값은 core.py에 있다 (daily_report.py의 corr_units.json 저장과
# intraday_watch.py의 표시가 같은 값을 써야 하므로 여기서 다시 정의하지
# 않는다). 종목당 상한은 core.MAX_UNITS.


@st.cache_data(ttl=300, show_spinner=False)
def load_holdings() -> list[core.HoldingInput]:
    """노션 보유 종목(구분="보유") 전체를 조회한다 (5분 캐시).

    load_corr_units()와 종목분석의 보유중 자동 인식(find_holding)이
    이 캐시를 공유한다 — 한 화면에서 둘 다 쓰여도 노션 API는 1회만
    호출된다. 실패하면 예외를 그대로 던진다 (호출부가 각자의 방식으로
    처리 — load_corr_units는 섹션을 비우고, find_holding은 미보유로
    간주한다).
    """
    import os

    for key in ("NOTION_TOKEN", "NOTION_DB_ID"):
        value = read_secret(key)
        if value:
            os.environ[key] = value

    from notion_repo import fetch_holdings

    return [inp for _, inp in fetch_holdings()]


def load_corr_units(
    capital: int,
) -> tuple[dict[str, float], float, list[str], list[str]]:
    """노션 보유 종목의 상관군별 누적 유닛수.

    집계 로직 자체는 core.calc_corr_units()에 있다 (daily_report.py와
    공유 – 다시 구현하지 않는다). 여기서는 캐시된 노션 조회를 넘길 뿐이다.

    Returns:
        (상관군별 유닛수, 전체 유닛수, 셀 수 없어 건너뛴 종목명, 상관군 이름 목록)
    """
    result = core.calc_corr_units(load_holdings(), capital)
    return result.group_units, result.total_units, result.skipped, result.known_groups


def find_holding(code: str) -> Optional[core.HoldingInput]:
    """code가 보유중이면 그 HoldingInput, 아니면 None.

    노션 조회 실패도 조용히 None으로 처리한다 — 종목분석 화면이 이
    실패로 죽지 않고 미보유 경로로 정상 동작해야 하기 때문.
    """
    try:
        holdings = load_holdings()
    except Exception:
        logging.warning("[%s] 보유종목 조회 실패 – 미보유로 간주", code, exc_info=True)
        return None
    for inp in holdings:
        if inp.ticker == code:
            return inp
    return None


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
    user_message: str, system_prompt: str = AI_SYSTEM_PROMPT, use_search: bool = True,
) -> str:
    """Gemini API 호출.

    Args:
        user_message: core.py / screener.py가 계산한 값
        system_prompt: 종목 의견(기본)과 섹터 해설이 같은 호출부를 쓴다
        use_search: True면 구글 검색 그라운딩 도구를 붙인다. 섹터 해설처럼
            출처를 특정할 수 없는 폭넓은 조사에는 필요하지만, 종목 의견의
            "사업 맥락"은 이미 네이버페이증권·DART에서 스크래핑한 자료를
            user_message에 넣어 소스를 고정했으므로 False로 끈다 — 켜두면
            그라운딩이 학습 지식·오래된 페이지를 다시 섞어 넣을 수 있다.

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
    config_kwargs = dict(
        system_instruction=system_prompt,
        max_output_tokens=AI_MAX_OUTPUT_TOKENS,
        thinking_config=types.ThinkingConfig(thinking_budget=AI_THINKING_BUDGET),
    )
    if use_search:
        config_kwargs["tools"] = [types.Tool(google_search=types.GoogleSearch())]

    response = client.models.generate_content(
        model=AI_MODEL,
        contents=user_message,
        config=types.GenerateContentConfig(**config_kwargs),
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
def load_etf_map() -> dict[str, str]:
    """ETF 코드→이름 전체. 조회 실패는 빈 맵으로 — is_etf()가 전부 False가
    되어 기존 주식 조회 동작(krx.get_market_ticker_name)으로 자연히
    떨어진다."""
    try:
        return core.fetch_etf_map()
    except Exception:
        return {}


def is_etf(code: str) -> bool:
    return code in load_etf_map()


@st.cache_data(ttl=86_400, show_spinner=False)
def load_name(code: str) -> str:
    etf_map = load_etf_map()
    if code in etf_map:
        return etf_map[code]
    try:
        name = krx.get_market_ticker_name(code)
    except Exception:
        return code          # 이름 조회 실패는 계산에 영향 없음
    # ETF·ETN 등 KOSPI/KOSDAQ 상장종목 목록에 없는 코드는 예외 없이 빈
    # DataFrame을 돌려준다 (pykrx dataframe_empty_handler) — 문자열이
    # 아니면 조회 실패로 본다.
    return name if isinstance(name, str) and name else code


# ── 즐겨찾기 ────────────────────────────────────────────────
#
# 노션이 유일한 원본이다. 세션·로컬 파일에 목록을 저장하지 않는다 —
# 스트림릿 클라우드는 재배포 시 초기화되므로 그때마다 목록이 사라진다.

@st.cache_data(ttl=300, show_spinner=False)
def load_favorites() -> list[tuple[str, dict]]:
    """즐겨찾기 목록. 화면 이동마다 재조회하지 않도록 5분 캐시한다."""
    try:
        return notion_repo.fetch_favorites()
    except Exception:
        logging.exception("즐겨찾기 목록 조회 실패")
        return []


def _favorite_page_id(code: str) -> str | None:
    for page_id, item in load_favorites():
        if item["ticker"] == code:
            return page_id
    return None


def _toggle_favorite(code: str, name: str, etf: bool,
                      page_id: str | None) -> None:
    """추가/삭제 실행 후 캐시를 비우고 다시 그린다."""
    try:
        if page_id:
            notion_repo.remove_favorite(page_id)
            st.toast(f"☆ {name} 즐겨찾기 삭제")
        else:
            notion_repo.add_favorite(code, name, "ETF" if etf else "주식")
            st.toast(f"⭐ {name} 즐겨찾기 추가")
    except Exception as e:
        logging.exception("즐겨찾기 처리 실패: %s(%s)", name, code)
        st.error(f"즐겨찾기 처리 실패: {e}")
        return
    load_favorites.clear()
    st.rerun()


def render_favorite_toggle(code: str, name: str, etf: bool) -> None:
    """별표 버튼. APP_PASSWORD가 설정돼 있으면 통과 전까지 팝오버로 막는다.

    한 번 통과하면 session_state["unlocked"]가 켜져 사이드바의 AI 잠금도
    같이 풀린다 — 두 기능이 같은 비밀번호·같은 잠금 상태를 공유한다.
    """
    page_id = _favorite_page_id(code)
    label = "⭐" if page_id else "☆"
    app_password = read_secret("APP_PASSWORD")

    if not app_password or st.session_state.get("unlocked"):
        if st.button(label, key=f"fav_{code}", help="즐겨찾기 추가/삭제"):
            _toggle_favorite(code, name, etf, page_id)
        return

    with st.popover(label, help="즐겨찾기 추가/삭제 (비밀번호 필요)"):
        entered = st.text_input("비밀번호", type="password", key=f"fav_pw_{code}")
        if st.button("확인", key=f"fav_pw_btn_{code}"):
            if entered == app_password:
                st.session_state["unlocked"] = True
                _toggle_favorite(code, name, etf, page_id)
            else:
                st.error("비밀번호가 일치하지 않습니다.")


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

    라벨 판정 자체는 core.breakout_verdict()에 위임한다 — 오늘의 돌파·
    전종목 스캔 표도 같은 함수로 판정해야 여기와 갈리지 않는다.
    """
    gap = sig.current_price - sig.high_20_prev
    mult = f", {gap / atr:.1f}×ATR" if atr > 0 else ""
    state = (
        core.breakout_verdict(gap / atr) if atr > 0
        else ("진입가능" if sig.breakout else "대기")
    )

    if state == "추격금지":
        return state, (
            f"직전 20일 고가보다 {gap:,.0f}원 위 "
            f"({gap / atr:.1f}×ATR) — 돌파 시점에서 이미 벗어남"
        )
    if state == "진입가능":
        return state, (
            f"직전 20일 고가 {sig.high_20_prev:,.0f} 돌파 "
            f"(+{gap:,.0f}원{mult})"
        )
    return state, (
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

st.markdown('<div id="stock-analysis-top"></div>', unsafe_allow_html=True)
st.title("📊 종목 진입 점검")
st.caption("신규 종목 조사 전용 · 터틀 트레이딩 규칙을 그대로 계산해 보여줍니다.")

# 앱이 공개라 AI 호출·즐겨찾기 쓰기만 비밀번호로 막는다. 조회·차트·계산과
# ?code= 딥링크 진입은 누구나 쓸 수 있다 — 카톡 인앱 브라우저는 세션이
# 매번 새로 뜰 수 있어, 앱 전체를 게이트하면 그 진입 동선이 깨진다.
# session_state["unlocked"]는 이 사이드바와 종목분석 화면의 별표 버튼
# (즐겨찾기 추가/삭제 팝오버)이 함께 쓴다 — 어느 쪽에서 통과해도 나머지
# 잠긴 기능이 같이 풀린다.
st.session_state.setdefault("unlocked", False)

with st.sidebar:
    st.subheader("🔐 접근")
    _app_password = read_secret("APP_PASSWORD")
    if _app_password and not st.session_state["unlocked"]:
        _entered = st.text_input(
            "비밀번호", type="password",
            help="AI 의견·즐겨찾기 추가/삭제에 필요합니다.",
        )
        if _entered:
            if _entered == _app_password:
                st.session_state["unlocked"] = True
            else:
                st.error("비밀번호가 일치하지 않습니다.")

    if st.session_state["unlocked"]:
        st.success("AI 의견 · 즐겨찾기 사용 가능")
    elif not _app_password:
        st.caption(
            "APP_PASSWORD 미설정 — AI 의견을 쓸 수 없습니다 "
            "(즐겨찾기는 잠금 없이 사용 가능합니다)."
        )

    ai_unlocked = st.session_state["unlocked"]

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


def render_scroll_to_top_button() -> None:
    """화면 우측 하단에 맨 위로(#stock-analysis-top) 이동하는 화살표를 띄운다.

    세 탭(종목 분석·오늘의 돌파·전종목 스캔) 모두 아래로 한참 내려야
    보이는 내용이 있어 공통으로 쓴다. 모달(_stock_dialog)은 닫기
    버튼으로 충분해 대상에서 뺐다.
    """
    st.markdown(
        """
        <a href="#stock-analysis-top" title="맨 위로" style="
            position: fixed; right: 24px; bottom: 72px; z-index: 9999;
            width: 44px; height: 44px; border-radius: 50%;
            background-color: #60a5fa; color: #0f172a;
            display: flex; align-items: center; justify-content: center;
            font-size: 20px; text-decoration: none;
            box-shadow: 0 2px 8px rgba(0, 0, 0, 0.4);
        ">↑</a>
        """,
        unsafe_allow_html=True,
    )


# ── [종목 분석] 화면 ────────────────────────────────────────

def render_analysis(capital: float, ai_unlocked: bool) -> None:
    """[종목 분석] 화면. 종목코드를 직접 입력해 조회하는 수동 조회 전용 화면.

    돌파·스캔 화면에서 종목을 고를 때는 여기로 넘어오지 않고
    render_stock_report()를 모달(_stock_dialog)로 바로 띄운다.

    카톡·메일 딥링크(?code=)로 들어온 경우 자동 조회한다. 세션당
    한 번만 적용해, 이후 사용자가 직접 다른 종목을 검색해도 URL에
    남은 code 파라미터가 매 렌더마다 그 값으로 되돌리지 않게 한다.
    """
    if not st.session_state.get("code_param_applied"):
        st.session_state["code_param_applied"] = True
        # KRX 종목코드는 대문자가 표준이다(예: ETF "0000J0"). 소문자로
        # 들어오면 이름·ETF맵 조회가 대소문자 불일치로 실패해, 시세는
        # 맞는데 종목명이 코드 그대로 나오는 것처럼 보인다.
        param_code = (st.query_params.get("code") or "").strip().upper()
        if param_code:
            if len(param_code) == 6 and param_code.isalnum():
                st.session_state["code_input"] = param_code
                st.session_state["query"] = param_code
            else:
                st.error("링크의 종목코드가 올바르지 않습니다 (6자리 영숫자).")

    col_code, col_btn = st.columns([4, 1])
    with col_code:
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
        entered = (code or "").strip().upper()
        if len(entered) != 6 or not entered.isalnum():
            st.error("종목코드는 숫자,알파벳 6자리여야 합니다.")
            return
        st.session_state["query"] = entered

    if "query" not in st.session_state:
        return

    render_stock_report(st.session_state["query"], capital, ai_unlocked)
    render_scroll_to_top_button()


def render_stock_report(code: str, capital: float, ai_unlocked: bool) -> None:
    """종목 하나의 전체 리포트(진입 신호~AI 의견)를 그린다.

    [종목 분석] 탭과 돌파·스캔 화면의 모달(_stock_dialog)이 이 함수를
    공유한다. 요약 표에는 20일 고가·저가나 기대값 통계, 차트용 전체
    시세가 없어 여기서 load_ohlcv()로 다시 계산한다 — 이 함수는 10분
    캐시라 같은 종목을 반복해서 열어도 재조회하지 않는다.
    """
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

    # 보유중 자동 인식 – 노션 조회 실패는 find_holding()이 삼켜 None으로
    # 돌려준다 (미보유 취급, 이 화면은 항상 정상 렌더링된다).
    holding = find_holding(code)
    held_atr = 0
    held_unit_shares = 0
    held_current_units = 0.0
    held_last_buy_price = 0.0
    if holding is not None:
        # 진입시 ATR이 고정값 – 매일 갱신되는 노션 'ATR'로 대체하지 않는다.
        # 둘 다 없으면(아직 배치가 한 번도 안 돈 신규 편입) 방금 조회한
        # 현재 ATR로 최후 폴백한다 (core.calc_corr_units와 같은 태도).
        held_atr = core.round_atr(holding.entry_atr or holding.notion_atr or atr)
        held_unit_shares = core.calc_position(held_atr, capital).unit_shares
        if held_unit_shares > 0:
            held_current_units = round(holding.shares / held_unit_shares, 2)
        held_last_buy_price = holding.last_buy_price or holding.buy_price

    name = load_name(code)
    etf = is_etf(code)
    col_title, col_star = st.columns([6, 1])
    with col_title:
        st.subheader(f"{name} ({code})")
        if etf:
            st.badge("ETF", color="blue")
        if holding is not None:
            st.badge(f"보유중 · {held_current_units:g}유닛", color="green")
    with col_star:
        render_favorite_toggle(code, name, etf)
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

    p1, p2, p3, p4, p5 = st.columns(5)
    p1.metric(f"ATR ({core.ATR_PERIOD}일)", f"{atr:,.0f}원",
              delta=f"현재가의 {atr / price * 100:.1f}%" if price else None)
    p2.metric("1유닛 주수", f"{pos.unit_shares:,}주")
    if holding is not None and holding.prev_stop_loss is not None:
        # 보유중이면 새로 계산하지 않고 daily 배치가 노션에 저장한 값을
        # 그대로 쓴다 — 이 화면에서 다시 계산한 값과 다를 수 있다
        # (트레일링·래칫). 아래 '유닛 진행표'에서 그 차이를 설명한다.
        p3.metric("실제 적용 손절선", f"{holding.prev_stop_loss:,.0f}원")
        with p3:
            st.caption("└ 노션에 저장된 값 (여기서 재계산하지 않음)")
    else:
        p3.metric("손절선 (진입 시)", f"{stop_loss:,.0f}원",
                  delta=f"-{2 * atr:,.0f}원 (2×ATR)")
    # 1N 움직였을 때의 손익이 아니라, 손절선에 닿았을 때 실제로 잃는 금액.
    # 유닛 진행표 1U 행의 손실액과 같은 값이어야 한다.
    unit_loss = pos.unit_shares * 2 * atr
    p4.metric("1유닛 손절 시", f"-{unit_loss:,.0f}원",
              delta=f"계좌 {unit_loss / capital * 100:.2f}%" if capital else None,
              delta_color="inverse")

    # 거래대금 — [오늘의 돌파] 스크리너(screener.py)가 실제로 거르는
    # 유동성 컷과 같은 기준(20일 평균, 10억)으로 통과 여부를 보여준다.
    window = df.tail(screener.TRADING_VALUE_DAYS)
    value_avg = float((window["종가"] * window["거래량"]).mean())
    liquidity_ok = value_avg >= screener.MIN_TRADING_VALUE
    p5.metric(
        f"거래대금 ({screener.TRADING_VALUE_DAYS}일 평균)",
        f"{value_avg:,.0f}원",
        delta="유동성 컷 통과" if liquidity_ok else "유동성 컷 미달",
        delta_color="off" if liquidity_ok else "inverse",
    )

    with p2:
        st.caption(f"└ 계좌 1% ÷ 1ATR (터틀 원본)")
    with p5:
        st.caption(f"└ 기준 {screener.MIN_TRADING_VALUE / 1e8:.0f}억 ([오늘의 돌파] 필터와 동일)")

    if pos.unit_shares == 0:
        st.warning(
            f"**진입 불가 — 1주도 리스크 한도 초과**  \n"
            f"1ATR {atr:,.0f}원이 계좌 1%({capital * core.RISK_PER_TRADE:,.0f}원)보다 "
            "큽니다. 이 종목은 현재 계좌 규모에 맞지 않습니다."
        )

    # ── 상관군 유닛 카운터 ──────────────────────────────────────
    st.markdown("### ■ 상관군 유닛")
    st.caption(
        f"종목당 {core.MAX_UNITS}유닛 · 상관군당 {core.MAX_UNITS_GROUP}유닛 · "
        f"전체 {core.MAX_UNITS_TOTAL}유닛 상한"
    )

    # 노션 조회가 실패해도 이 섹션만 비고 나머지 화면은 그대로 남는다
    corr_groups: dict[str, float] = {}
    corr_total = 0.0
    corr_skipped: list[str] = []
    corr_names: list[str] = []
    corr_loaded = False
    try:
        corr_groups, corr_total, corr_skipped, corr_names = load_corr_units(int(capital))
        corr_loaded = True
    except Exception as e:
        st.info(f"보유 현황을 불러오지 못해 상관군 집계를 건너뜁니다. ({e})")

    # 집계 자체는 성공했더라도 표시 단계에서 예상 못 한 값(예: 노션에 잘못
    # 입력된 상관군·수량 조합)을 만나면 여기서만 죽고 나머지 화면은
    # 살아있어야 한다 — 위 노션 조회 실패 처리와 같은 태도.
    try:
        if corr_loaded and holding is not None:
            # 보유중이면 이미 배정된 상관군을 읽기 전용으로 보여준다 —
            # 미보유 종목처럼 '어디에 넣을지' 고를 대상이 아니다.
            group = (holding.corr_group or "").strip()
            if group:
                used = corr_groups.get(group, 0.0)
                st.metric(f"{group} 상관군 누적 유닛", f"{used:g}/{core.MAX_UNITS_GROUP}")
            else:
                st.caption("이 종목엔 상관군이 지정돼 있지 않습니다.")
            if corr_total >= core.MAX_UNITS_TOTAL:
                st.error(f"전체 {corr_total:g}/{core.MAX_UNITS_TOTAL}유닛 — 상한 도달")
            else:
                st.caption(f"전체 {corr_total:g}/{core.MAX_UNITS_TOTAL}유닛")
        elif corr_loaded:
            known = corr_names
            pick_col, info_col = st.columns([1, 2])
            with pick_col:
                choice = st.selectbox(
                    "이 종목의 상관군", known + ["(없음)", "(직접 입력)"],
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
                        f"현재 {used:g}유닛 / {core.MAX_UNITS_GROUP}",
                        delta=f"1유닛 진입 시 {after:g}/{core.MAX_UNITS_GROUP}",
                        delta_color="off",
                    )
                    if used >= core.MAX_UNITS_GROUP:
                        st.error(f"상관군 상한 도달 — 이 종목은 진입 불가")
                    elif after >= core.MAX_UNITS_GROUP:
                        st.warning(
                            f"이 종목 1유닛 진입 시 {after:g}/{core.MAX_UNITS_GROUP}, "
                            "이후 추가 불가"
                        )
                else:
                    st.caption("상관군을 고르면 누적 유닛을 확인할 수 있습니다.")

                total_after = corr_total + 1
                if corr_total >= core.MAX_UNITS_TOTAL:
                    st.error(f"전체 {corr_total:g}/{core.MAX_UNITS_TOTAL}유닛 — 상한 도달")
                elif total_after > core.MAX_UNITS_TOTAL:
                    st.warning(f"전체 {corr_total:g}/{core.MAX_UNITS_TOTAL}유닛 — 1유닛 더 넣으면 초과")
                else:
                    st.caption(f"전체 {corr_total:g}/{core.MAX_UNITS_TOTAL}유닛")

            if corr_skipped:
                st.caption("유닛을 셀 수 없어 제외: " + ", ".join(corr_skipped))
    except Exception as e:
        st.info(f"상관군 집계를 표시하지 못했습니다. ({e})")

    # ── 유닛 진행표 ─────────────────────────────────────────────
    st.markdown("### ■ 유닛 진행표")

    if holding is not None:
        st.caption(
            "보유중 — ✱매수단가(1U)·✱진입시ATR 기준의 고정값입니다. "
            "현재가가 바뀌어도 이 표는 바뀌지 않습니다."
        )

        # 노션에 남은 값이 예상 밖 조합(예: 유닛 계산이 애매한 소수 유닛)일
        # 때도 이 표만 비고 아래 차트·AI 의견은 그대로 렌더링돼야 한다.
        try:
            # 개별 유닛의 실제 매수가는 1U(✱매수단가)와 마지막 유닛(마지막매수가)
            # 말고는 노션에 남아있지 않다. 그래서 1U만 ✱매수단가를 그대로 쓰고
            # (표시용, 다른 행 계산에는 안 씀) 나머지 전 구간(이미 산 유닛 포함)은
            # 마지막매수가를 축으로 0.5×ATR 간격으로 채운다 — 아직 안 산 유닛은
            # 이 축에서 앞으로, 이미 산 유닛은 뒤로 되짚는 값이다.
            anchor_units = max(1, min(round(held_current_units) or 1, core.MAX_UNITS))
            held_buy_prices = [round(holding.buy_price)]
            for unit in range(2, core.MAX_UNITS + 1):
                held_buy_prices.append(
                    round(
                        held_last_buy_price
                        + (unit - anchor_units) * core.PYRAMID_ATR_STEP * held_atr
                    )
                )

            held_steps = core.build_pyramid_from_prices(
                held_buy_prices, held_atr, held_unit_shares, capital
            )
            if not held_steps:
                st.info("1유닛 주수가 0이라 진행표를 만들 수 없습니다.")
            else:
                st.dataframe(
                    {
                        "매수": ["✅" if s.unit <= anchor_units else "" for s in held_steps],
                        "단계": [f"{s.unit}U" for s in held_steps],
                        "매수가": [f"{s.buy_price:,.0f}" for s in held_steps],
                        "손절선": [f"{s.stop_loss:,.0f}" for s in held_steps],
                        "누적 주수": [f"{s.cum_shares:,}" for s in held_steps],
                        "누적 투입": [f"{s.cum_cost:,.0f}" for s in held_steps],
                        "누적 투입비중": [f"{s.cum_weight_pct:.1f}%" for s in held_steps],
                        "손절 시 총손실": [f"{s.loss_if_stopped:,.0f}" for s in held_steps],
                        "총자본 대비": [f"{s.loss_pct:.2f}%" for s in held_steps],
                    },
                    hide_index=True, width="stretch",
                )

                if anchor_units >= core.MAX_UNITS:
                    st.success(f"✅ 추가매수 완료 ({core.MAX_UNITS}/{core.MAX_UNITS}유닛)")
                else:
                    # held_buy_prices는 0-index라 unit=anchor_units+1의 값은
                    # index=anchor_units에 있다.
                    next_price = held_buy_prices[anchor_units]
                    st.metric(
                        f"다음 매수가 ({anchor_units + 1}U)", f"{next_price:,.0f}원",
                        delta=f"+{core.PYRAMID_ATR_STEP}×ATR", delta_color="off",
                    )

                actual_stop_txt = (
                    f"{holding.prev_stop_loss:,.0f}원"
                    if holding.prev_stop_loss is not None
                    else "아직 계산되지 않음 (다음 아침 배치 이후 반영)"
                )
                st.caption(
                    f"표의 '손절선'은 그 유닛을 살 때 적용됐을(될) 값입니다. "
                    f"실제로 지금 적용되는 손절선은 {actual_stop_txt}입니다 — 더 "
                    "높다면 진입후최고가 갱신에 따른 트레일링 때문입니다 "
                    "(손절선은 래칫이라 내려가지 않습니다)."
                )
        except Exception as e:
            st.info(f"유닛 진행표를 표시하지 못했습니다. ({e})")
    else:
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
            with st.spinner("최근 뉴스·공시를 확인하는 중…"):
                activity_block, fetched_at = build_recent_activity_block(code, is_etf=etf)
            with st.spinner("AI가 지표를 해석하는 중…"):
                try:
                    user_message = (
                        build_ai_user_message(
                            name, code, price, atr, sig, edge, pos, stop_loss, state,
                        )
                        + "\n\n" + activity_block
                    )
                    # 사업 맥락은 위에서 스크래핑한 자료만 근거로 삼는다 —
                    # google_search는 소스를 못 고르므로 여기서는 끈다.
                    system_prompt = AI_SYSTEM_PROMPT_ETF if etf else AI_SYSTEM_PROMPT
                    ai_cache[ai_key] = request_ai_opinion(
                        user_message, system_prompt=system_prompt, use_search=False
                    )
                    st.session_state.setdefault("ai_fetched_at", {})[ai_key] = fetched_at
                    st.session_state["ai_calls"] = ai_calls + 1
                except Exception as e:
                    # 실패해도 위쪽 리포트는 그대로 유지된다
                    st.error(f"AI 의견 실패: {e}")

    if ai_key in ai_cache:
        st.markdown(ai_cache[ai_key])
        fetched_at = st.session_state.get("ai_fetched_at", {}).get(ai_key)
        if fetched_at:
            st.caption(f"최근 뉴스·공시 기준일: {fetched_at.strftime('%Y-%m-%d %H:%M')}")
        st.caption(
            "AI 해석입니다. 매매 판단은 노션에 정해둔 규칙과 손절선을 따르세요."
        )


# ── 통일 스캔 표 (오늘의 돌파 · 전종목 스캔 공유) ────────────
#
# 두 탭이 같은 판단을 하는데 표에 나오는 정보가 갈리면 안 되므로,
# 컬럼 구성과 조건부 색상을 이 함수 하나로 통일한다.

SCAN_TABLE_HIGHLIGHT = "background-color: rgba(74, 222, 128, 0.28); font-weight: bold"


def render_scan_table(
    rows: list[dict],
    metric_label: str,
    metric_signed: bool = True,
    highlight_metric: bool = True,
    category_label: str = "업종",
    show_vol_mult: bool = True,
) -> None:
    """오늘의 돌파·전종목 스캔·즐겨찾기가 공유하는 표.

    rows의 각 dict는 name·code·sector·price·metric·atr_pct·vol_mult·verdict
    키를 가진다 (metric·vol_mult는 계산 불가 시 None). 색상은 회색약
    대비를 위해 배경색과 함께 볼드를 입힌다 — 색상만으로 구분되지
    않게 한다.

    Args:
        metric_label: 갭(×ATR)/거리(×ATR) 등 세 번째 컬럼 이름
        metric_signed: True면 +/- 부호를 표시 (갭), False면 절대값
            그대로 표시 (거리 — 아직 돌파 전이라 부호가 항상 음수라
            부호를 보여줄 이유가 없다)
        highlight_metric: metric 컬럼에 색을 입힐지. '임박' 섹션처럼
            아직 규칙을 통과하지 않은 값은 강조하지 않는다.
        category_label: 두 번째 컬럼 이름. 즐겨찾기 표는 업종 데이터가
            없어 "구분"(주식/ETF)을 대신 넣는다 — 이때 rows의 sector
            키에 그 값을 담아 넘긴다.
        show_vol_mult: 거래량배수 컬럼 표시 여부. 즐겨찾기는 이 값을
            계산하지 않아 컬럼 자체를 뺀다.
    """
    columns = {
        "종목명(코드)": [f"{r['name']} ({r['code']})" for r in rows],
        category_label: [r["sector"] for r in rows],
        "현재가": [r["price"] for r in rows],
        metric_label: [r["metric"] for r in rows],
        "ATR%": [r["atr_pct"] for r in rows],
    }
    if show_vol_mult:
        columns["거래량배수"] = [r["vol_mult"] for r in rows]
    columns["판정"] = [r["verdict"] for r in rows]
    df = pd.DataFrame(columns)

    metric_fmt = (
        (lambda v: f"{v:+.2f}" if pd.notna(v) else "-") if metric_signed
        else (lambda v: f"{v:.2f}" if pd.notna(v) else "-")
    )
    format_dict = {
        # 즐겨찾기는 야간 배치 전이면 현재가가 비어 있을 수 있어(None→NaN)
        # 고정 포맷 문자열 대신 NaN-safe 람다를 쓴다.
        "현재가": lambda v: f"{v:,.0f}" if pd.notna(v) else "-",
        metric_label: metric_fmt,
        "ATR%": lambda v: f"{v:.1f}%" if pd.notna(v) else "-",
    }
    if show_vol_mult:
        format_dict["거래량배수"] = lambda v: f"{v:.1f}배" if pd.notna(v) else "-"
    styler = df.style.format(format_dict)

    if highlight_metric:
        styler = styler.map(
            lambda v: SCAN_TABLE_HIGHLIGHT if pd.notna(v) and v <= core.CHASE_ATR_MULT else "",
            subset=[metric_label],
        )
    styler = styler.map(
        lambda v: SCAN_TABLE_HIGHLIGHT if pd.notna(v) and v >= core.SCAN_ATR_PCT_MIN else "",
        subset=["ATR%"],
    )
    if show_vol_mult:
        styler = styler.map(
            lambda v: SCAN_TABLE_HIGHLIGHT if pd.notna(v) and v >= core.VOL_MULT_MIN else "",
            subset=["거래량배수"],
        )

    st.dataframe(styler, hide_index=True, width="stretch")


# ── [오늘의 돌파] 화면 ──────────────────────────────────────

# 스캔 기준일이 이만큼 지나면 "최신 결과 없음"으로 안내한다.
# 연휴가 끼면 마지막 거래일이 4일 전일 수 있어(예: 금요일 스캔 → 화요일
# 아침에 확인) 그보다 넉넉히 잡았다. 앱에서 휴장일을 계산하지는 않는다.
BREAKOUT_STALE_DAYS = 5


@st.dialog("종목 분석", width="large")
def _stock_dialog(ticker: str, capital: float, ai_unlocked: bool) -> None:
    """돌파·스캔 목록에서 고른 종목을 화면 이동 없이 모달로 보여준다.

    st.dialog는 st.fragment로 동작해 모달 내부 위젯 상호작용은 이
    함수만 다시 그린다 — 뒤에 있는 스캔 화면의 슬라이더·스크롤 위치는
    그대로 남는다.

    맨 위 X 버튼 말고 맨 아래에도 닫기를 둔다 — 내용을 끝까지 본
    다음 다시 위로 스크롤하지 않아도 되게.
    """
    render_stock_report(ticker, capital, ai_unlocked)
    st.divider()
    if st.button("닫기", width="stretch"):
        st.rerun()


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

    # ── 표 (갭(×ATR) 오름차순 — 아직 덜 뛴 = 추격위험 낮은 종목이 위로) ──
    # 업종 분포 요약(위)은 업종별로 묶어서 보고, 종목 표는 갭 기준으로
    # 다시 정렬한다. 둘의 정렬 기준이 다른 건 의도한 것이다.
    def _gap_atr(s: dict) -> float:
        # 예전 스캔 결과(gap_atr 필드 도입 전)와도 호환되게 필요하면
        # 있는 값으로 계산한다.
        if "gap_atr" in s:
            return s["gap_atr"]
        return (s["price"] - s["high_20_prev"]) / s["atr"] if s["atr"] else 0.0

    rows = sorted(stocks, key=_gap_atr)

    st.markdown("### ■ 통과 종목")
    render_scan_table(
        [
            {
                "name": s["name"],
                "code": s["ticker"],
                "sector": s["sector"],
                "price": s["price"],
                "metric": _gap_atr(s),
                "atr_pct": s["atr_pct"],
                "vol_mult": s["vol_mult"],
                "verdict": core.breakout_verdict(_gap_atr(s)),
            }
            for s in rows
        ],
        metric_label="갭(×ATR)",
    )
    st.caption(
        "ATR%는 현재가 대비, 거래량배수는 직전 20일 평균 대비입니다. "
        "1유닛 주수·손절선 등은 종목을 열면 계좌 금액 기준으로 다시 계산됩니다."
    )

    # ── 종목 선택 → 모달로 리포트 ───────────────────────────
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
        if st.button("종목 분석 보기", type="primary", width="stretch"):
            ticker = labels[chosen]
            st.session_state["dialog_ticker"] = ticker
            _stock_dialog(ticker, capital, ai_unlocked)

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
    render_scroll_to_top_button()


# ── 전종목 스캔 ─────────────────────────────────────────────

@st.cache_data(ttl=600, show_spinner=False)
def load_scan(_mtime: float):
    """scan_latest.csv. 인자는 캐시 키용 – 파일이 바뀌면 다시 읽는다."""
    return scan_all.load_scan()


def render_scan(capital: float, ai_unlocked: bool) -> None:
    """[전종목 스캔] 화면. scan_all.py가 저장한 CSV를 읽어 보여준다.

    스캔을 여기서 돌리지 않는다. CSV에는 임계값을 적용하지 않은 전종목
    원본 수치가 들어 있고, 이 화면은 슬라이더 값으로 마스크만 다시 건다.
    기준을 바꿔도 재조회가 없으므로 즉시 반영된다.
    """
    if not scan_all.LATEST_PATH.exists():
        st.info(
            "**스캔 결과 없음** — `data/scan_latest.csv`가 없습니다.  \n"
            "야간 스캔이 돌면 생깁니다 (GitHub Actions 평일 21:00 KST)."
        )
        return

    frame = load_scan(scan_all.LATEST_PATH.stat().st_mtime)
    if frame is None or frame.empty:
        st.warning("스캔 결과가 비어 있습니다.")
        return

    st.subheader(f"스캔 기준일 {frame['scan_date'].iloc[0]}")
    st.caption(
        f"{len(frame):,}종목 · 임계값 없이 저장된 원본 수치입니다. "
        "아래 슬라이더는 필터만 다시 겁니다 (재조회 없음)."
    )

    # 이 컬럼들이 도입되기 전에 저장된 스캔 결과와도 호환한다 — 다음
    # 야간 스캔(scan_all.py)부터 채워진다.
    missing_cols = [c for c in ("sector", "vol_mult") if c not in frame.columns]
    if missing_cols:
        frame = frame.assign(**{
            "sector": frame.get("sector", "미확인"),
            "vol_mult": frame.get("vol_mult", float("nan")),
        })
        st.caption(
            f"⚠ 이전 스캔 결과라 {'·'.join(missing_cols)} 정보가 없습니다 "
            "(다음 야간 스캔부터 채워집니다)."
        )
    # ATR%는 저장된 값에 기대지 않고 여기서 다시 낸다 — capital과
    # 무관해 재계산 비용이 없고, 스키마 변화에도 흔들리지 않는다.
    frame = frame.assign(
        atr_pct=frame.apply(
            lambda r: core.calc_atr_pct(float(r["atr20"]), float(r["close"])), axis=1
        )
    )

    s1, s2 = st.columns(2)
    gap_max = s1.slider(
        "돌파 갭 상한 (×ATR)", 0.0, 2.0, core.CHASE_ATR_MULT, 0.05,
        help="20일 고가를 넘은 폭. 이보다 더 벌어졌으면 추격 구간으로 봅니다.",
    )
    dist_max = s2.slider(
        "임박 거리 상한 (×ATR)", 0.1, 3.0, 1.0, 0.1,
        help="20일 고가까지 남은 거리. 좁을수록 돌파가 가깝습니다.",
    )

    broke = frame[
        (frame["status"] == scan_all.STATUS_BREAKOUT)
        & (frame["gap_atr"] <= gap_max)
    ].sort_values("gap_atr")
    near = frame[
        (frame["status"] == scan_all.STATUS_NEAR)
        & (frame["dist_atr"] <= dist_max)
    ].sort_values("dist_atr")

    st.markdown(f"### ■ 돌파 ({len(broke)}종목)")
    if broke.empty:
        st.info(f"갭 {gap_max:.2f}×ATR 이내로 돌파한 종목이 없습니다.")
    else:
        render_scan_table(
            [
                {
                    "name": r["name"],
                    "code": r["ticker"],
                    "sector": r["sector"],
                    "price": r["close"],
                    "metric": r["gap_atr"],
                    "atr_pct": r["atr_pct"],
                    "vol_mult": r["vol_mult"],
                    "verdict": core.breakout_verdict(r["gap_atr"]),
                }
                for _, r in broke.iterrows()
            ],
            metric_label="갭(×ATR)",
        )

        st.markdown("### ■ 종목 열기")
        pick_col, btn_col = st.columns([3, 1])
        with pick_col:
            broke_labels = {
                f"{n} ({t})": t
                for n, t in zip(broke["name"], broke["ticker"])
            }
            broke_chosen = st.selectbox(
                "리포트를 볼 종목", list(broke_labels),
                key="scan_broke_pick",
            )
        with btn_col:
            st.write("")
            if st.button(
                "종목 분석 보기", type="primary", width="stretch",
                key="scan_broke_open",
            ):
                ticker = broke_labels[broke_chosen]
                st.session_state["dialog_ticker"] = ticker
                _stock_dialog(ticker, capital, ai_unlocked)

    st.markdown(f"### ■ 임박 ({len(near)}종목)")
    if near.empty:
        st.info(f"20일 고가까지 {dist_max:.1f}×ATR 이내인 종목이 없습니다.")
    else:
        # 아직 20일 고가를 못 넘은 종목들이라 판정은 항상 '대기'이고,
        # 거리(×ATR)는 규칙 통과를 뜻하지 않으므로 색 강조도 안 입힌다.
        render_scan_table(
            [
                {
                    "name": r["name"],
                    "code": r["ticker"],
                    "sector": r["sector"],
                    "price": r["close"],
                    "metric": r["dist_atr"],
                    "atr_pct": r["atr_pct"],
                    "vol_mult": r["vol_mult"],
                    "verdict": "대기",
                }
                for _, r in near.iterrows()
            ],
            metric_label="거리(×ATR)",
            metric_signed=False,
            highlight_metric=False,
        )

        st.markdown("### ■ 종목 열기")
        pick_col, btn_col = st.columns([3, 1])
        with pick_col:
            near_labels = {
                f"{n} ({t})": t
                for n, t in zip(near["name"], near["ticker"])
            }
            near_chosen = st.selectbox(
                "리포트를 볼 종목", list(near_labels),
                key="scan_near_pick",
            )
        with btn_col:
            st.write("")
            if st.button(
                "종목 분석 보기", type="primary", width="stretch",
                key="scan_near_open",
            ):
                ticker = near_labels[near_chosen]
                st.session_state["dialog_ticker"] = ticker
                _stock_dialog(ticker, capital, ai_unlocked)

    st.caption(
        "종가 기준입니다 — 장중 현재가가 아닙니다. 1유닛 주수·손절선 등은 "
        "종목을 열면 사이드바 계좌 금액 기준으로 다시 계산됩니다."
    )
    render_scroll_to_top_button()


# ── [즐겨찾기] 화면 ─────────────────────────────────────────

def render_favorites(capital: float, ai_unlocked: bool) -> None:
    """[즐겨찾기] 화면. 노션 "즐겨찾기 모니터링" DB를 읽어 보여준다.

    여기서는 계산하지 않는다 — 매일 밤 favorites_batch.py가 종가 기준
    으로 계산해 노션에 써둔 값을 그대로 읽는다 (scan_latest.csv와는
    무관한 별도 계산 경로 — 유동성 컷에 걸린 종목도 여기엔 항상 있다).
    """
    favorites = load_favorites()
    if not favorites:
        st.info(
            "즐겨찾기한 종목이 없습니다. [종목 분석]에서 별표(⭐)를 눌러 "
            "추가하세요."
        )
        return

    checked_dates = {
        item["checked_date"] for _, item in favorites if item["checked_date"]
    }
    if checked_dates:
        st.caption(
            f"배치 기준일: {max(checked_dates).isoformat()} "
            "(매일 밤 종가 기준으로 계산됩니다)"
        )
    uncalculated = sum(1 for _, item in favorites if item["checked_date"] is None)
    if uncalculated:
        st.caption(
            f"⚠ {uncalculated}종목은 아직 계산 전입니다 "
            "(다음 야간 배치에서 채워집니다)."
        )

    render_scan_table(
        [
            {
                "name": item["name"],
                "code": item["ticker"],
                "sector": item.get("category") or "-",
                "price": item["current_price"],
                "metric": item["gap_atr"],
                "atr_pct": item["atr_pct"],
                "vol_mult": None,
                "verdict": item.get("verdict") or "-",
            }
            for _, item in favorites
        ],
        metric_label="갭(×ATR)",
        category_label="구분",
        show_vol_mult=False,
    )

    st.markdown("### ■ 종목 열기")
    pick_col, btn_col = st.columns([3, 1])
    with pick_col:
        labels = {
            f"{item['name']} ({item['ticker']})": item["ticker"]
            for _, item in favorites
        }
        chosen = st.selectbox("리포트를 볼 종목", list(labels), key="fav_pick")
    with btn_col:
        st.write("")
        if st.button("종목 분석 보기", type="primary", width="stretch",
                     key="fav_open"):
            ticker = labels[chosen]
            st.session_state["dialog_ticker"] = ticker
            _stock_dialog(ticker, capital, ai_unlocked)

    render_scroll_to_top_button()


# ── [계산기] 화면 ────────────────────────────────────────────

def render_calculator(capital: float) -> None:
    """숫자를 직접 입력해 손절선·추가매수가·유닛 진행표를 계산한다.

    [종목 분석]과 달리 시세를 조회하지 않는다 — 종목이 시스템에 없어도
    즉석 계산이 가능해야 하기 때문. 계산은 전부 core.py의 기존 함수를
    그대로 쓴다 (resolve_stop·calc_position·build_pyramid). 노션에
    아무것도 쓰지 않는다.

    core.resolve_stop()의 entry 인자는 '1유닛 진입가'가 아니라 '현재
    유닛 기준가(=마지막 매수가)'다 (calc_stop 독스트링 참고 — 추가매수
    하면 트레일링 기준 자체가 마지막 매수가로 넘어간다). 그래서 여기서도
    resolve_stop에는 마지막매수가를 entry로 넘긴다. 매수가(1U)는 유닛
    진행표 미리보기의 기준으로만 쓴다.
    """
    st.caption(
        "종목이 시스템에 없어도 숫자만으로 즉석 계산합니다. "
        "시세 조회·노션 저장 없음 — 값을 바꾸면 바로 다시 계산됩니다."
    )

    c1, c2 = st.columns(2)
    with c1:
        first_unit_price = st.number_input(
            "매수가 (1유닛 진입가, 원)", min_value=0, step=100, format="%d",
            value=10_000, key="calc_first_unit_price",
        )
    with c2:
        atr = st.number_input(
            "ATR (진입시 ATR, 원)", min_value=0, step=10, format="%d",
            value=300, key="calc_atr",
        )

    c3, c4 = st.columns(2)
    with c3:
        # 키를 매수가에 연동해, 매수가가 바뀌면 기본값도 그 값으로
        # 다시 맞춰진다 ('비우면 매수가와 같다고 간주'). 매수가가
        # 그대로인 동안 사용자가 고친 값은 재실행에도 유지된다.
        trailing_high = st.number_input(
            "진입후최고가 (원, 선택)", min_value=0, step=100, format="%d",
            value=int(first_unit_price), key=f"calc_trailing_{first_unit_price}",
            help="비워두면(=건드리지 않으면) 매수가와 같다고 봅니다.",
        )
    with c4:
        units = st.radio(
            "유닛수", list(range(1, core.MAX_UNITS + 1)),
            horizontal=True, key="calc_units",
        )

    last_buy_price = float(first_unit_price)
    if units >= 2:
        estimated = first_unit_price + (units - 1) * core.PYRAMID_ATR_STEP * atr
        last_buy_price = st.number_input(
            f"마지막매수가 ({units}유닛째 매수가, 원)",
            min_value=0, step=100, format="%d",
            value=int(round(estimated)),
            key=f"calc_last_buy_{first_unit_price}_{atr}_{units}",
            help="비워두면 0.5×ATR 간격 추정치를 씁니다. "
                 "실제 매수가가 다르면 고쳐서 입력하세요.",
        )

    # ── 오타 방지 경고 (계산은 그대로 진행) ──
    if first_unit_price > 0:
        if trailing_high <= first_unit_price * CALC_DROP_RATIO:
            st.warning(
                "⚠️ 입력값이 비정상적으로 벌어져 있습니다. 자릿수를 확인하세요. "
                f"(진입후최고가가 매수가의 {CALC_DROP_RATIO:.0%} 이하)"
            )
        elif trailing_high >= first_unit_price * CALC_RISE_MULT:
            st.warning(
                "⚠️ 입력값이 비정상적으로 벌어져 있습니다. 자릿수를 확인하세요. "
                f"(진입후최고가가 매수가의 {CALC_RISE_MULT:.0f}배 이상)"
            )

    if first_unit_price <= 0 or atr <= 0:
        st.info("매수가와 ATR을 입력하면 계산 결과가 표시됩니다.")
        return

    # ── 계산 (core.py 기존 함수 재사용, 새 계산식 없음) ──
    stop_loss = core.resolve_stop(
        entry=last_buy_price,
        atr=atr,
        trailing_high=trailing_high,
        last_buy_price=last_buy_price,
        prev_stop=None,
    )
    pos = core.calc_position(atr, capital)

    st.markdown("### ■ 계산 결과")
    r1, r2, r3 = st.columns(3)
    r1.metric(
        "현재 손절선", f"{stop_loss:,.0f}원",
        delta=f"마지막매수가 대비 {stop_loss - last_buy_price:+,.0f}원",
    )
    r2.metric("1유닛 주수", f"{pos.unit_shares:,}주")
    with r2:
        st.caption("└ 계좌 1% ÷ ATR (사이드바 계좌 금액 기준)")
    if units < core.MAX_UNITS:
        next_add = round(last_buy_price + core.PYRAMID_ATR_STEP * atr)
        r3.metric(
            f"다음 유닛({units + 1}U) 매수가", f"{next_add:,.0f}원",
            delta=f"+{core.PYRAMID_ATR_STEP}×ATR", delta_color="off",
        )
    else:
        r3.metric("추가매수", "종료", delta=f"{core.MAX_UNITS}유닛 도달",
                  delta_color="off")

    if pos.unit_shares == 0:
        st.warning(
            f"**1유닛 주수 0 — 계좌 규모에 안 맞는 ATR**  \n"
            f"1ATR {atr:,.0f}원이 계좌 1%({capital * core.RISK_PER_TRADE:,.0f}원)"
            "보다 큽니다."
        )

    st.markdown("### ■ 유닛 진행표")
    steps = core.build_pyramid(first_unit_price, atr, pos.unit_shares, capital)
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
            f"매수가(1U)를 진입가로 놓고 {core.PYRAMID_ATR_STEP}×ATR "
            f"({core.PYRAMID_ATR_STEP * atr:,.0f}원)마다 1유닛씩 "
            f"최대 {core.MAX_UNITS}유닛까지 쌓은 계획표입니다. 위 '현재 손절선'과"
            " 달리 마지막매수가·진입후최고가 입력값과 무관한 이론상 미리보기입니다."
        )


# ── 탭 ──────────────────────────────────────────────────────

# st.tabs는 위젯 상호작용이 있으면 선택이 유지되지 않는다. 상태를 가진
# segmented_control을 탭 막대로 써서 슬라이더 등을 조작해도 탭이 안 바뀐다.
st.session_state.setdefault("tab", TAB_ANALYSIS)
_tab = st.segmented_control(
    "화면",
    (TAB_ANALYSIS, TAB_BREAKOUT, TAB_SCAN, TAB_FAVORITES, TAB_CALCULATOR),
    key="tab",
    label_visibility="collapsed",
)

# 선택을 해제하면 None이 온다. 그때는 직전 탭을 유지한다.
if _tab == TAB_BREAKOUT:
    render_breakout(capital, ai_unlocked)
elif _tab == TAB_SCAN:
    render_scan(capital, ai_unlocked)
elif _tab == TAB_FAVORITES:
    render_favorites(capital, ai_unlocked)
elif _tab == TAB_CALCULATOR:
    render_calculator(capital)
else:
    render_analysis(capital, ai_unlocked)
