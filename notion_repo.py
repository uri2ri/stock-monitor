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
                    buy_price=_number(props.get("매수단가", {})) or 0,
                    shares=int(_number(props.get("보유수량", {})) or 0),
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
                    news_memo=_text(props.get("공시·뉴스", {})).strip(),
                    exit_signal=_checkbox(props.get("철수신호", {})),
                    news_date=_date_val(props.get("뉴스 확인일", {})),
                    # 메일 카드용 – 사람이 적어두는 판단 근거 (읽기만 한다)
                    buy_reason=_text(props.get("산 이유", {})).strip(),
                    bull_case=_text(props.get("강세론", {})).strip(),
                    bear_case=_text(props.get("약세론", {})).strip(),
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

def update_holding(page_id: str, result: HoldingResult) -> None:
    """한 종목의 계산 결과를 노션에 기록한다.

    갱신 필드: ATR, 손절선, 진입후 최고가, 최근 판정, 판정 메모, 확인일
    """
    url = f"{NOTION_BASE}/pages/{page_id}"

    properties: dict[str, Any] = {
        "ATR": {"number": result.atr if result.atr else None},
        "손절선": {"number": result.stop_loss if result.stop_loss else None},
        "진입후 최고가": {
            "number": result.trailing_high if result.trailing_high else None
        },
        "최근 판정": {
            "select": {"name": result.verdict} if result.verdict else None
        },
        "판정 메모": {
            "rich_text": [
                {
                    "type": "text",
                    "text": {"content": (result.verdict_memo or "")[:2000]},
                }
            ]
        },
        "확인일": {
            "date": {"start": date.today().isoformat()}
        },
    }

    resp = requests.patch(
        url,
        headers=_headers(),
        json={"properties": properties},
        timeout=30,
    )
    resp.raise_for_status()
    logger.info("[%s] 노션 업데이트 완료", result.name)
