"""
notion_repo.py – 노션 DB 읽기/쓰기
"""

from __future__ import annotations

import logging
import os
from datetime import date
from typing import Any, Optional

import requests

from core import HoldingInput, HoldingResult

logger = logging.getLogger(__name__)

NOTION_API_VERSION = "2022-06-28"
NOTION_BASE = "https://api.notion.com/v1"


def _headers() -> dict[str, str]:
    token = os.environ["NOTION_TOKEN"]
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_API_VERSION,
        "Content-Type": "application/json",
    }


# ── 유틸: 노션 프로퍼티 파싱 ────────────────────────────────

def _text(prop: dict) -> str:
    """title / rich_text 프로퍼티에서 문자열 추출."""
    ptype = prop.get("type", "")
    if ptype == "title":
        parts = prop.get("title", [])
    elif ptype == "rich_text":
        parts = prop.get("rich_text", [])
    else:
        return ""
    return "".join(p.get("plain_text", "") for p in parts)


def _number(prop: dict) -> Optional[float]:
    """number 프로퍼티에서 숫자 추출. 없으면 None."""
    val = prop.get("number")
    return float(val) if val is not None else None


def _select(prop: dict) -> str:
    """select 프로퍼티에서 이름 추출."""
    sel = prop.get("select")
    if sel and isinstance(sel, dict):
        return sel.get("name", "")
    return ""


def _checkbox(prop: dict) -> bool:
    """checkbox 프로퍼티에서 bool 추출. 없으면 False."""
    return bool(prop.get("checkbox", False))


def _flag(prop: dict) -> bool:
    """'철수신호'처럼 참/거짓을 나타내는 칸을 bool로 읽는다.

    노션에서 select(미해당/해당/확인 불가)로 만들어져 있어 checkbox로
    읽으면 항상 False가 된다. 두 타입을 모두 처리한다.
    """
    if prop.get("type") == "checkbox":
        return _checkbox(prop)
    return _select(prop) == "해당"


def _date_val(prop: dict) -> Optional[date]:
    """date 프로퍼티에서 date 객체 추출."""
    d = prop.get("date")
    if d and isinstance(d, dict) and d.get("start"):
        return date.fromisoformat(d["start"])
    return None


# ── 읽기: 보유 종목 조회 ────────────────────────────────────

def fetch_holdings() -> list[tuple[str, HoldingInput]]:
    """노션 DB에서 구분='보유'인 행을 읽어 (page_id, HoldingInput) 리스트로 반환."""
    db_id = os.environ["NOTION_DB_ID"]
    url = f"{NOTION_BASE}/databases/{db_id}/query"

    payload: dict[str, Any] = {
        "filter": {
            "property": "구분",
            "select": {"equals": "보유"},
        },
        "page_size": 100,
    }

    results: list[tuple[str, HoldingInput]] = []
    has_more = True
    start_cursor: Optional[str] = None

    while has_more:
        if start_cursor:
            payload["start_cursor"] = start_cursor

        resp = requests.post(url, headers=_headers(), json=payload, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        for page in data.get("results", []):
            page_id = page["id"]
            props = page.get("properties", {})

            try:
                inp = HoldingInput(
                    ticker=_text(props.get("종목코드", {})).strip(),
                    name=_text(props.get("종목명", {})).strip(),
                    market=_select(props.get("시장", {})),
                    buy_price=_number(props.get("✱ 매수단가", {})) or 0,
                    shares=int(_number(props.get("✱ 보유수량", {})) or 0),
                    # 1차·2차 익절가는 읽지 않는다 (청산은 10일 저가 기준).
                    # 노션 칸은 비교용으로 남겨두되 계산에 넣지 않는다.
                    reeval_date=_date_val(props.get("재평가 기한", {})),
                    # 상관군: 텍스트 칸. 같은 값끼리 합산하며 비어 있으면
                    # 상관군 없음. select로 만들었을 경우도 함께 처리한다.
                    corr_group=(
                        _text(props.get("상관군", {})).strip()
                        or _select(props.get("상관군", {}))
                    ),
                    prev_trailing_high=_number(props.get("진입후 최고가", {})),
                    prev_stop_loss=_number(props.get("손절선", {})),
                    notion_atr=_number(props.get("ATR", {})),
                    # 진입시 고정값 – 배치는 읽기만 하고 덮어쓰지 않는다
                    entry_atr=_number(props.get("✱ 진입시 ATR", {})),
                    last_buy_price=_number(props.get("마지막 매수가", {})),
                    signal_first_date=_date_val(props.get("신호 최초 발생일", {})),
                    last_alerted_stop=_number(props.get("마지막 알린 손절선", {})),
                    news_memo=_text(props.get("공시·뉴스", {})).strip(),
                    exit_signal=_flag(props.get("철수신호", {})),
                    news_date=_date_val(props.get("뉴스 확인일", {})),
                    # 메일 카드용 – 사람이 적어두는 판단 근거 (읽기만 한다)
                    buy_reason=_text(props.get("✱ 산 이유", {})).strip(),
                    bull_case=_text(props.get("강세론", {})).strip(),
                    bear_case=_text(props.get("✱ 약세론", {})).strip(),
                    next_event=_text(props.get("다음 확인 이벤트", {})).strip(),
                )
                if not inp.ticker:
                    logger.warning("종목코드 없는 행 무시: page_id=%s", page_id)
                    continue
                results.append((page_id, inp))
            except Exception:
                logger.exception("노션 행 파싱 실패: page_id=%s", page_id)

        has_more = data.get("has_more", False)
        start_cursor = data.get("next_cursor")

    logger.info("노션에서 보유 종목 %d건 로드", len(results))
    return results


# ── 쓰기: 계산 결과 반영 ────────────────────────────────────

# 배치가 쓸 수 있는 칸은 이게 전부다. Cowork가 쓰는 칸
# (공시·뉴스 / 철수신호 / 뉴스 확인일)과 사람이 쓰는 칸은 절대 건드리지 않는다.
BATCH_WRITABLE = (
    "ATR",
    "✱ 진입시 ATR",          # 값이 없을 때 최초 1회만
    "손절선",
    "진입후 최고가",
    "최근 판정",
    "판정 메모",
    "확인일",
    "신호 최초 발생일",
    "마지막 매수가",        # 값이 없을 때만 매수단가로 초기화
    "마지막 알린 손절선",   # 갱신 알림을 보냈을 때만 (evaluate_holding이 결정)
)


def _rich_text(value: str) -> dict:
    return {"rich_text": [{"type": "text", "text": {"content": value[:2000]}}]}


def _date_prop(value: Optional[date]) -> dict:
    return {"date": {"start": value.isoformat()} if value else None}


def update_holding(
    page_id: str,
    result: HoldingResult,
    inp: Optional[HoldingInput] = None,
) -> None:
    """계산 결과를 노션에 기록한다.

    BATCH_WRITABLE 목록 밖의 칸은 PATCH에 절대 넣지 않는다. 그리고 값이
    실제로 바뀐 칸만 보낸다 — Cowork 예약작업(07:00)과 배치(07:30)가
    같은 행을 건드리므로, 안 바뀐 칸까지 통째로 덮어쓰면 서로의 기록을
    지우게 된다.

    Args:
        inp: 노션에서 읽어온 현재 값. 주면 변경분만 전송한다.
    """
    url = f"{NOTION_BASE}/pages/{page_id}"
    today = date.today()

    desired: dict[str, Any] = {
        "ATR": {"number": result.atr or None},
        "손절선": {"number": result.stop_loss or None},
        "진입후 최고가": {"number": result.trailing_high or None},
        "최근 판정": {
            "select": {"name": result.verdict} if result.verdict else None
        },
        "판정 메모": _rich_text(result.verdict_memo or ""),
        "확인일": _date_prop(today),
        "신호 최초 발생일": _date_prop(result.signal_first_date),
    }

    # 진입시 ATR·마지막 매수가는 '비어 있을 때만' 채운다. 이미 값이 있으면
    # 배치가 절대 덮어쓰지 않는다 (진입 시점 고정값이므로).
    if inp is None or inp.entry_atr is None:
        desired["✱ 진입시 ATR"] = {"number": result.entry_atr or None}
    if inp is None or inp.last_buy_price is None:
        desired["마지막 매수가"] = {"number": result.last_buy_price or None}

    # 마지막 알린 손절선은 evaluate_holding이 갱신이 필요하다고 판단했을
    # 때(알림을 보냈거나, 신규 편입이라 초기화가 필요할 때)만 값이 있다.
    # None이면 이 칸을 아예 건드리지 않는다 – 값을 남겨두면 다음 비교
    # 기준이 계속 리셋돼 임계값에 영원히 도달하지 못한다.
    if result.last_alerted_stop is not None:
        desired["마지막 알린 손절선"] = {"number": result.last_alerted_stop}

    properties = {k: v for k, v in desired.items() if k in BATCH_WRITABLE}

    # 변경분만 남긴다 (읽어온 값이 있을 때만 비교 가능)
    if inp is not None:
        unchanged = []
        if inp.prev_stop_loss is not None and \
                round(inp.prev_stop_loss) == result.stop_loss:
            unchanged.append("손절선")
        if inp.prev_trailing_high is not None and \
                inp.prev_trailing_high == result.trailing_high:
            unchanged.append("진입후 최고가")
        if inp.signal_first_date == result.signal_first_date:
            unchanged.append("신호 최초 발생일")
        if inp.last_alerted_stop is not None and \
                inp.last_alerted_stop == result.last_alerted_stop:
            unchanged.append("마지막 알린 손절선")
        for key in unchanged:
            properties.pop(key, None)

    if not properties:
        logger.info("[%s] 노션 변경 없음 – PATCH 생략", result.name)
        return

    resp = requests.patch(
        url,
        headers=_headers(),
        json={"properties": properties},
        timeout=30,
    )
    resp.raise_for_status()
    logger.info(
        "[%s] 노션 업데이트 완료 (%s)", result.name, ", ".join(properties)
    )


# ── 즐겨찾기 모니터링 DB ────────────────────────────────────
#
# 보유종목 점검표 DB와 완전히 별개다. 서로의 칸을 읽거나 쓰지 않는다.
#
# 스키마 (실제 조회로 확인함):
#   입력 칸 : 종목명(title) · 코드(rich_text) · 구분(select: 주식/ETF) · 메모(rich_text)
#   계산 칸 : 현재가·ATR·ATR%·20일고가·갭(×ATR)·10일저가(number) ·
#             판정(rich_text) · 확인일(date)
#
# "×"는 U+00D7(곱셈기호)다. 속성명을 손으로 다시 치지 말고 이 파일의
# 상수를 참조할 것 — 알파벳 x로 잘못 치면 노션이 새 칸을 만들어버린다.

FAVORITES_GAP_ATR_PROP = "갭(×ATR)"


def fetch_favorites() -> list[tuple[str, dict]]:
    """즐겨찾기 DB 전체 조회. (page_id, dict) 목록을 돌려준다.

    dict 키: ticker, name, category, memo, current_price, atr, atr_pct,
    high20, gap_atr, verdict, low10, checked_date.
    계산 전 종목은 계산 칸이 전부 None이다 (아직 배치가 안 돈 것).
    """
    db_id = os.environ["NOTION_FAVORITES_DB_ID"]
    url = f"{NOTION_BASE}/databases/{db_id}/query"

    results: list[tuple[str, dict]] = []
    has_more = True
    start_cursor: Optional[str] = None
    payload: dict[str, Any] = {"page_size": 100}

    while has_more:
        if start_cursor:
            payload["start_cursor"] = start_cursor

        resp = requests.post(url, headers=_headers(), json=payload, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        for page in data.get("results", []):
            page_id = page["id"]
            props = page.get("properties", {})
            try:
                item = {
                    "ticker": _text(props.get("코드", {})).strip(),
                    "name": _text(props.get("종목명", {})).strip(),
                    "category": _select(props.get("구분", {})),
                    "memo": _text(props.get("메모", {})).strip(),
                    "current_price": _number(props.get("현재가", {})),
                    "atr": _number(props.get("ATR", {})),
                    "atr_pct": _number(props.get("ATR%", {})),
                    "high20": _number(props.get("20일고가", {})),
                    "gap_atr": _number(props.get(FAVORITES_GAP_ATR_PROP, {})),
                    "verdict": _text(props.get("판정", {})).strip(),
                    "low10": _number(props.get("10일저가", {})),
                    "checked_date": _date_val(props.get("확인일", {})),
                }
                if not item["ticker"]:
                    logger.warning("즐겨찾기: 코드 없는 행 무시 page_id=%s", page_id)
                    continue
                results.append((page_id, item))
            except Exception:
                logger.exception("즐겨찾기 행 파싱 실패: page_id=%s", page_id)

        has_more = data.get("has_more", False)
        start_cursor = data.get("next_cursor")

    logger.info("노션에서 즐겨찾기 %d건 로드", len(results))
    return results


def add_favorite(ticker: str, name: str, category: str) -> str:
    """즐겨찾기 페이지 생성. 계산 칸은 비워둔다 (야간 배치가 채운다).

    Returns:
        생성된 page_id
    """
    db_id = os.environ["NOTION_FAVORITES_DB_ID"]
    payload = {
        "parent": {"database_id": db_id},
        "properties": {
            "종목명": {"title": [{"type": "text", "text": {"content": name}}]},
            "코드": {"rich_text": [{"type": "text", "text": {"content": ticker}}]},
            "구분": {"select": {"name": category}},
        },
    }
    resp = requests.post(f"{NOTION_BASE}/pages", headers=_headers(),
                          json=payload, timeout=30)
    resp.raise_for_status()
    page_id = resp.json()["id"]
    logger.info("즐겨찾기 추가: %s(%s) page_id=%s", name, ticker, page_id)
    return page_id


def remove_favorite(page_id: str) -> None:
    """즐겨찾기 삭제. 노션 API는 진짜 삭제 대신 보관(archive) 처리한다."""
    resp = requests.patch(
        f"{NOTION_BASE}/pages/{page_id}",
        headers=_headers(),
        json={"archived": True},
        timeout=30,
    )
    resp.raise_for_status()
    logger.info("즐겨찾기 삭제: page_id=%s", page_id)


def update_favorite_calc(page_id: str, calc: dict) -> None:
    """야간 배치가 계산 칸만 갱신한다. 입력 칸(종목명·코드·구분·메모)은 건드리지 않는다.

    calc 키: current_price, atr, atr_pct, high20, gap_atr, verdict, low10.
    """
    properties = {
        "현재가": {"number": calc.get("current_price")},
        "ATR": {"number": calc.get("atr")},
        "ATR%": {"number": calc.get("atr_pct")},
        "20일고가": {"number": calc.get("high20")},
        FAVORITES_GAP_ATR_PROP: {"number": calc.get("gap_atr")},
        "판정": _rich_text(calc.get("verdict") or ""),
        "10일저가": {"number": calc.get("low10")},
        "확인일": _date_prop(date.today()),
    }
    resp = requests.patch(
        f"{NOTION_BASE}/pages/{page_id}",
        headers=_headers(),
        json={"properties": properties},
        timeout=30,
    )
    resp.raise_for_status()
    logger.info("즐겨찾기 계산 갱신 완료: page_id=%s", page_id)
