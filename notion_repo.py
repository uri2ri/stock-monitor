"""
notion_repo.py – 노션 DB 읽기/쓰기
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime
from typing import Any, Optional
from zoneinfo import ZoneInfo

import requests

from core import HoldingInput, HoldingResult, today_kst

logger = logging.getLogger(__name__)

NOTION_API_VERSION = "2022-06-28"
NOTION_BASE = "https://api.notion.com/v1"
_KST = ZoneInfo("Asia/Seoul")


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

def fetch_holdings(managed_by: Optional[str] = None) -> list[tuple[str, HoldingInput]]:
    """노션 DB에서 구분='보유'인 행을 읽어 (page_id, HoldingInput) 리스트로 반환.

    managed_by를 주면 '운용' 칸이 그 값인 행만 읽는다(예: MANAGED_AUTO="자동").
    기본은 필터 없이 전체를 읽는다 - 아침 배치(daily_report.py)는 수동
    보유까지 전부 판정해야 하므로 이 필터를 쓰지 않는다.

    kis_client.run_auto_sell()은 managed_by=MANAGED_AUTO로 좁혀서 부른다:
    모의계좌(자동매매 전용)의 매도가능수량과 종목코드만으로 매칭하면,
    같은 티커의 실계좌 수동 보유 종목까지 매도 후보에 섞여 들어갈 수
    있다 - 자동매수가 실제로 편입한 행(find_auto_holding_page가 쓰는
    운용="자동")만 보게 좁혀 그 경로를 막는다.
    """
    db_id = os.environ["NOTION_DB_ID"]
    url = f"{NOTION_BASE}/databases/{db_id}/query"

    condition: dict[str, Any] = {"property": "구분", "select": {"equals": "보유"}}
    if managed_by:
        condition = {
            "and": [condition, {"property": "운용", "select": {"equals": managed_by}}]
        }

    payload: dict[str, Any] = {
        "filter": condition,
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
                    managed_by=_select(props.get("운용", {})),
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
                    # 배치가 쓴 판정과 그 판정일 – 자동매도가 "오늘 판정"인지
                    # 확인하는 데 쓴다 (kis_client.run_auto_sell).
                    pyramid_anchor=_number(props.get("추가매수 기준가", {})),
                    recent_verdict=_select(props.get("최근 판정", {})),
                    checked_date=_date_val(props.get("확인일", {})),
                    units=(
                        int(units_val)
                        if (units_val := _number(props.get("유닛수", {}))) is not None
                        else None
                    ),
                    evening_signal_type=_select(props.get("저녁판정 신호유형", {})),
                    evening_signal_date=_date_val(props.get("저녁판정 발생일", {})),
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


# ── 쓰기: 자동매수 종목 편입 ────────────────────────────────

MANAGED_AUTO = "자동"
MANAGED_MANUAL = "수동"


def find_auto_holding_page(ticker: str) -> Optional[str]:
    """구분='보유' + 운용='자동'인 행 중 이 종목코드의 page_id. 없으면 None.

    자동매수가 편입 전에 "이미 자동으로 들고 있는 종목인가"를 확인하는 데 쓴다.

    운용='자동'으로 반드시 좁힌다: 같은 종목을 다른 증권사에서 수동으로
    들고 있을 수 있는데, 그 행을 찾아 수량을 더해버리면 서로 다른 계좌의
    포지션이 한 행에 뒤섞인다(보유수량·마지막 매수가가 둘 다 망가진다).
    수동 행은 못 찾은 것으로 두고 자동용 행을 새로 만드는 쪽이 항상 맞다.

    예외를 삼키지 않는다 - 호출자가 fail-open/closed를 정한다.
    """
    db_id = os.environ["NOTION_DB_ID"]
    url = f"{NOTION_BASE}/databases/{db_id}/query"
    payload: dict[str, Any] = {
        "filter": {
            "and": [
                {"property": "종목코드", "rich_text": {"equals": ticker}},
                {"property": "구분", "select": {"equals": "보유"}},
                {"property": "운용", "select": {"equals": MANAGED_AUTO}},
            ]
        },
        "page_size": 1,
    }
    resp = requests.post(url, headers=_headers(), json=payload, timeout=30)
    resp.raise_for_status()
    results = resp.json().get("results", [])
    return results[0]["id"] if results else None


def fetch_auto_holding_page_ids() -> set[str]:
    """운용='자동'인 점검표 행 전체(구분 불문)의 page_id 집합.

    weekly_report.py가 매매일지 청산 기록 중 자동매매 소관만 골라내는 데
    쓴다 - 매매일지는 수동 매매도 같은 DB에 섞여 있어서(카카오·삼현 등),
    청산 기록 행의 '보유종목' relation이 가리키는 점검표 행의 운용을
    봐야 자동매매분만 걸러진다. find_auto_holding_page()와 달리 구분(보유/
    청산)은 가리지 않는다 - 이미 청산된 점검표 행도 이 집합에 있어야
    지난주 이전에 청산된 자동매매 종목이 계속 걸러진다.
    """
    db_id = os.environ["NOTION_DB_ID"]
    url = f"{NOTION_BASE}/databases/{db_id}/query"
    payload: dict[str, Any] = {
        "filter": {"property": "운용", "select": {"equals": MANAGED_AUTO}},
        "page_size": 100,
    }
    page_ids: set[str] = set()
    has_more = True
    start_cursor: Optional[str] = None
    while has_more:
        if start_cursor:
            payload["start_cursor"] = start_cursor
        resp = requests.post(url, headers=_headers(), json=payload, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        page_ids.update(page["id"] for page in data.get("results", []))
        has_more = data.get("has_more", False)
        start_cursor = data.get("next_cursor")
    return page_ids


def set_pyramid_anchor(page_id: str, anchor: float) -> None:
    """추가매수 기준가만 갱신한다 (실제 매수 없이 레벨만 올릴 때).

    마지막 매수가는 손대지 않는다 - 그건 실제 체결 기록이고 손절선 계산의
    기준이라, 사지도 않은 가격으로 덮으면 장부와 손절이 함께 틀어진다.
    """
    resp = requests.patch(
        f"{NOTION_BASE}/pages/{page_id}", headers=_headers(),
        json={"properties": {"추가매수 기준가": {"number": round(anchor)}}},
        timeout=30,
    )
    resp.raise_for_status()
    logger.info("추가매수 기준가 갱신: page_id=%s -> %s", page_id, f"{anchor:,.0f}")


def create_auto_holding(
    *,
    name: str,
    ticker: str,
    market: str,
    buy_price: float,
    shares: int,
    atr: float,
    corr_group: str = "",
    memo: str = "",
    buy_reason: str = "",
    exit_condition: str = "손절선 이탈(자동)",
) -> str:
    """자동매수로 편입한 종목을 보유종목 점검표에 새로 만든다.

    이 행이 있어야 아침 배치가 손절선을 매일 갱신하고(트레일링), 그래야
    자동매도가 판단할 기준이 생긴다 - 자동매수만 하고 이 기록을 안 남기면
    산 종목이 청산 대상에서 통째로 빠진다.

    운용="자동"으로 표시해 수동 관리 종목과 구분한다. 매매 자체는 계좌가
    분리돼 있어(KIS=자동 전용) 섞일 일이 없고, 이 칸은 사람이 노션에서
    구분해 보기 위한 것이다.

    손절선·진입후 최고가는 진입 시점 값으로 초기화한다. 다음 아침 배치부터
    evaluate_holding()이 트레일링으로 갱신한다.

    ✱ 산 이유 / ✱ 철수 조건은 정성적 판단 칸이라 자동매매는 채울 수
    없지만, 빈칸으로 두면 "사람이 깜빡한 것"인지 "자동매매라 원래
    없는 것"인지 구분이 안 된다. buy_reason을 안 주면(신호 정보가 없는
    호출부) "자동매매 — 정성적 근거 미기록"으로 최소한 표시하고,
    철수 조건은 항상 채운다 - 자동매매는 손절선(진입가-2×ATR) 이탈이
    곧 철수 조건이라 값이 고정돼 있다.

    Returns:
        생성된 page_id
    """
    db_id = os.environ["NOTION_DB_ID"]
    today = today_kst()
    stop = round(buy_price - 2 * atr)

    properties: dict[str, Any] = {
        "종목명": {"title": [{"type": "text", "text": {"content": name}}]},
        "종목코드": _rich_text(ticker),
        "구분": {"select": {"name": "보유"}},
        "운용": {"select": {"name": MANAGED_AUTO}},
        "✱ 매수단가": {"number": round(buy_price)},
        "✱ 보유수량": {"number": shares},
        "✱ 진입시 ATR": {"number": round(atr)},
        "ATR": {"number": round(atr)},
        "손절선": {"number": stop},
        "마지막 매수가": {"number": round(buy_price)},
        "추가매수 기준가": {"number": round(buy_price)},
        "진입후 최고가": {"number": round(buy_price)},
        "유닛수": {"number": 1},
        "매수일": _date_prop(today),
        "확인일": _date_prop(today),
        "판정 메모": _rich_text(memo),
        "✱ 산 이유": _rich_text(buy_reason or "자동매매 — 정성적 근거 미기록"),
        "✱ 철수 조건": _rich_text(exit_condition),
    }
    if market:
        properties["시장"] = {"select": {"name": market}}
    if corr_group:
        properties["상관군"] = _rich_text(corr_group)

    resp = requests.post(
        f"{NOTION_BASE}/pages", headers=_headers(),
        json={"parent": {"database_id": db_id}, "properties": properties},
        timeout=30,
    )
    resp.raise_for_status()
    page_id = resp.json()["id"]
    logger.info("자동매수 종목 편입: %s(%s) %d주 @%s 손절선 %s page_id=%s",
                name, ticker, shares, f"{buy_price:,.0f}", f"{stop:,}", page_id)
    return page_id


# 매매일지 '✱ 청산 사유' select 옵션 중 자동매도가 낼 수 있는 값.
# 자동매도는 손절선 이탈과 추세청산(10일 저가) 두 가지로만 판다 -
# 익절·철수조건·임의 매도는 사람이 손으로 적는 값이라 여기 없다.
LEDGER_EXIT_STOP = "손절"
LEDGER_EXIT_TREND = "10일 저가 이탈"

# 수식 칸이라 절대 쓰지 않는다 - 노션이 진입가·청산가·ATR로 자동 계산한다.
# 여기에 값을 넣으려 하면 노션 API가 400을 뱉는다.
LEDGER_FORMULA_FIELDS = (
    "R배수", "수익률(%)", "손익금액", "결과", "지연일수", "진입시 손절선",
)


def create_ledger_record(
    *,
    name: str,
    ticker: str,
    market: str,
    entry_price: float,
    entry_atr: Optional[float],
    shares: int,
    units: Optional[int],
    exit_date: date,
    exit_price: float,
    exit_reason: str,
    signal_date: date,
    entry_date: Optional[date] = None,
    holding_page_id: Optional[str] = None,
    memo: str = "",
) -> str:
    """자동매도로 청산한 포지션을 매매일지(청산 기록)에 1행으로 남긴다.

    점검표는 "지금 뭘 들고 있나"를 보는 현재 상태 테이블이라 청산과 동시에
    구분만 "청산"으로 바뀌고 끝이다 - 승률·R배수·지연일수 같은 사후 통계는
    청산 건마다 한 행이 쌓이는 이 DB에서만 나온다. 자동매도가 여기 안 쓰면
    "자동화 이후 성적이 어떤가"를 볼 방법이 아예 없어진다.

    R배수·수익률·손익금액·결과·지연일수·진입시 손절선은 노션 수식 칸이라
    쓰지 않는다(LEDGER_FORMULA_FIELDS) - 진입가·진입시 ATR·청산가·신호
    발생일만 정확히 넣으면 나머지는 노션이 계산한다.

    entry_date(진입일)는 weekly_report.py의 평균 보유기간 계산 기준이다.
    HoldingInput이 매수일을 안 들고 있어(core.py는 건드리지 않는다)
    호출부가 notion_repo.fetch_holding_buy_date()로 따로 조회해 넘긴다.
    못 구했으면(조회 실패 등) None으로 두고 이 칸을 생략한다 - 다른 값을
    대신 채우지 않는다.

    규칙 준수는 항상 체크한다 - 자동매도는 정의상 신호대로 실행된 것이고,
    사람의 임의 판단이 끼어들 여지가 없다.

    Returns:
        생성된 page_id
    """
    db_id = os.environ["NOTION_LEDGER_DB_ID"]

    properties: dict[str, Any] = {
        "종목명": {"title": [{"type": "text", "text": {"content": name}}]},
        "종목코드": _rich_text(ticker),
        "✱ 진입가": {"number": round(entry_price)},
        "✱ 매수 수량": {"number": shares},
        "✱ 청산일": _date_prop(exit_date),
        "✱ 청산가": {"number": round(exit_price)},
        "✱ 청산 사유": {"select": {"name": exit_reason}},
        "청산 유형": {"select": {"name": "전량"}},
        "규칙 준수": {"checkbox": True},
        # 지연일수(수식) 계산의 기준. 자동매매는 신호 즉시 실행이라 0에
        # 가까워야 정상이고, 커지면 배치가 밀렸다는 신호다.
        "신호 발생일": _date_prop(signal_date),
    }
    if market:
        properties["시장"] = {"select": {"name": market}}
    if entry_atr is not None:
        # R배수의 분모(1R = 진입가 - 진입시 손절선)가 이 값에서 나온다.
        properties["✱ 진입시 ATR"] = {"number": round(entry_atr)}
    if units is not None:
        properties["유닛수"] = {"number": units}
    if entry_date is not None:
        properties["진입일"] = _date_prop(entry_date)
    if holding_page_id:
        properties["보유종목"] = {"relation": [{"id": holding_page_id}]}
    if memo:
        properties["청산 메모"] = _rich_text(memo)

    assert not set(properties) & set(LEDGER_FORMULA_FIELDS)

    resp = requests.post(
        f"{NOTION_BASE}/pages", headers=_headers(),
        json={"parent": {"database_id": db_id}, "properties": properties},
        timeout=30,
    )
    resp.raise_for_status()
    page_id = resp.json()["id"]
    logger.info("매매일지 청산 기록: %s(%s) %d주 진입 %s -> 청산 %s (%s) page_id=%s",
                name, ticker, shares, f"{entry_price:,.0f}",
                f"{exit_price:,.0f}", exit_reason, page_id)
    return page_id


def close_auto_holding(page_id: str) -> None:
    """자동매도 체결 후 보유종목 점검표에서 이 행을 청산 처리한다.

    구분만 "청산"으로 바꾼다 - 손절선·ATR 등 나머지 칸은 마지막 값
    그대로 남겨 청산 시점 기록으로 쓴다. 이렇게 해야
    find_auto_holding_page()(구분="보유" 필터)가 이 행을 더는 못 찾아서,
    같은 종목을 나중에 다시 사면 create_auto_holding()이 새 행을
    만든다 - 옛 행을 재사용해 손절선 계산 기준(진입시 ATR 등)이
    꼬이는 일이 없다.
    """
    resp = requests.patch(
        f"{NOTION_BASE}/pages/{page_id}", headers=_headers(),
        json={"properties": {"구분": {"select": {"name": "청산"}}}},
        timeout=30,
    )
    resp.raise_for_status()
    logger.info("자동매도 체결 - 보유종목 점검표 청산 처리: page_id=%s", page_id)


def add_auto_holding_units(page_id: str, *, add_shares: int,
                            buy_price: float) -> None:
    """이미 있는 보유 행에 추가매수분을 더한다 (수량 누적 + 마지막 매수가 갱신).

    ✱ 매수단가·✱ 진입시 ATR은 건드리지 않는다 - 진입 시점 고정값이라
    추가매수로 바뀌면 안 된다 (손절선 계산 기준이 흔들린다).

    평단가는 가중평균으로 갱신한다:
        새 평단가 = (기존평단가×기존보유수량 + 이번매수가×이번매수수량) / 총보유수량
    매매일지 '✱ 진입가'가 이 칸을 그대로 가져다 쓰는데 그 정의가
    가중평균이라, 여기서 안 갱신하면 2유닛 이상 종목의 R배수가 실제보다
    나쁘게 나온다.

    평단가가 아직 비어 있으면(이 계산을 도입하기 전에 만들어진 행 -
    지금까지 1유닛=매수 1회였다는 뜻) 그 1회 체결가와 같으므로 ✱ 매수단가
    (최초 진입 고정값)를 기존평단가 대신 쓴다.
    """
    url = f"{NOTION_BASE}/pages/{page_id}"
    resp = requests.get(url, headers=_headers(), timeout=30)
    resp.raise_for_status()
    props = resp.json().get("properties", {})
    cur_shares = int(_number(props.get("✱ 보유수량", {})) or 0)
    cur_units = int(_number(props.get("유닛수", {})) or 1)
    cur_avg = _number(props.get("평단가", {}))
    if cur_avg is None:
        cur_avg = _number(props.get("✱ 매수단가", {})) or buy_price

    new_shares = cur_shares + add_shares
    new_avg = (
        (cur_avg * cur_shares + buy_price * add_shares) / new_shares
        if new_shares else buy_price
    )

    payload = {"properties": {
        "✱ 보유수량": {"number": new_shares},
        "마지막 매수가": {"number": round(buy_price)},
        # 실제로 샀으므로 기준가도 체결가로 맞춘다.
        "추가매수 기준가": {"number": round(buy_price)},
        "유닛수": {"number": cur_units + 1},
        "평단가": {"number": round(new_avg)},
    }}
    resp = requests.patch(url, headers=_headers(), json=payload, timeout=30)
    resp.raise_for_status()
    logger.info("자동매수 추가분 반영: page_id=%s %d주 -> %d주, 평단가 %s -> %s",
                page_id, cur_shares, new_shares, f"{cur_avg:,.0f}", f"{new_avg:,.0f}")


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
    today = today_kst()

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


# ── 자동주문 기록 DB ────────────────────────────────────────
#
# 보유종목 점검표·매매일지·즐겨찾기 DB와 완전히 별개다. 서로의 칸을
# 읽거나 쓰지 않는다. kis_client.py가 GitHub Actions의 매 실행마다
# 새로 뜨는 컨테이너에서도 "오늘 몇 건 주문했는지 · 어느 종목을 이미
# 주문했는지"를 알 수 있도록 실행 간 상태를 이 DB에 남긴다.
#
# 조회 함수(count_success_orders_today / has_order_today)는 절대 예외를
# 삼키지 않는다 — 실패하면 그대로 올려서 호출자가 "조회 실패 = 주문 중단"
# (fail-closed)으로 처리하게 한다. 여기서 0건으로 눙치면 일일 상한과
# 중복 방지가 둘 다 무력화된다.
#
# 기록은 2단계다: 주문 전송 "전"에 상태="주문중"으로 먼저 만들고(create_order_record),
# 전송 후 같은 행을 결과로 갱신한다(update_order_record). 전송 -> 기록 순서면
# 주문은 나갔는데 기록이 실패했을 때 다음 실행이 그 주문을 못 보고 또 사는
# 구멍이 생긴다 (kis_client.py 쪽 정책 - 여기선 그 순서만 지원한다).

ORDER_LOG_STATUSES = ("성공", "실패", "거부", "경고", "주문중")

SIDE_BUY = "매수"
SIDE_SELL = "매도"

# 매수 주문의 성격. 신규 진입과 추가매수(피라미딩)는 상한을 따로 세야 한다 -
# 추가매수가 신규 진입 예산을 먹으면 그날 새 돌파를 못 잡고, 반대로 한 예산에
# 섞으면 "같은 종목 하루 몇 번까지"를 종목별로 제한할 수 없다.
ORDER_NEW = "신규"
ORDER_ADD = "추가"


def _side_filter(side: str) -> dict:
    """매매구분 필터 조각.

    "매매구분" 칸은 자동매도를 붙이면서 나중에 추가됐다. 그 전에 쌓인 행은
    전부 매수인데 칸이 비어 있으므로, 매수를 거를 땐 빈 값도 매수로 본다 –
    안 그러면 과거 매수 기록이 중복 방지·일일 상한 계산에서 통째로 빠진다.
    """
    if side == SIDE_BUY:
        return {"or": [
            {"property": "매매구분", "select": {"equals": SIDE_BUY}},
            {"property": "매매구분", "select": {"is_empty": True}},
        ]}
    return {"property": "매매구분", "select": {"equals": side}}


def _order_type_filter(order_type: str) -> dict:
    """주문유형 필터 조각. 빈 값(칸 도입 이전 기록)은 신규로 본다."""
    if order_type == ORDER_NEW:
        return {"or": [
            {"property": "주문유형", "select": {"equals": ORDER_NEW}},
            {"property": "주문유형", "select": {"is_empty": True}},
        ]}
    return {"property": "주문유형", "select": {"equals": order_type}}


def create_order_record(
    *,
    name: str,
    ticker: str,
    order_no: str,
    qty: float,
    price: float,
    status: str,
    reason: str,
    account_type: str,
    when: datetime,
    side: str = SIDE_BUY,
    order_type: str = ORDER_NEW,
) -> str:
    """자동주문 기록 DB에 주문 시도 1건을 남긴다. 성공·실패·거부 모두 기록한다.

    Args:
        side: "매수" | "매도". 매도 기록이 매수의 중복 방지·일일 상한
            계산에 섞여 들어가지 않도록 구분해서 남긴다.

    Returns:
        생성된 page_id
    """
    db_id = os.environ["NOTION_ORDERS_DB_ID"]
    payload = {
        "parent": {"database_id": db_id},
        "properties": {
            "종목명": {"title": [{"type": "text", "text": {"content": name}}]},
            "주문일시": {"date": {"start": when.isoformat()}},
            "종목코드": {"rich_text": [{"type": "text", "text": {"content": ticker}}]},
            "주문번호": {"rich_text": [{"type": "text", "text": {"content": order_no}}]},
            "수량": {"number": qty},
            "주문가": {"number": price},
            "상태": {"select": {"name": status}},
            "사유": _rich_text(reason),
            "계좌구분": {"select": {"name": account_type}},
            "매매구분": {"select": {"name": side}},
            # 매도에는 신규/추가 구분이 없다 - 청산은 항상 전량이다.
            **({"주문유형": {"select": {"name": order_type}}}
               if side == SIDE_BUY else {}),
        },
    }
    resp = requests.post(f"{NOTION_BASE}/pages", headers=_headers(),
                          json=payload, timeout=30)
    resp.raise_for_status()
    page_id = resp.json()["id"]
    logger.info("자동주문 기록: %s(%s) %s page_id=%s", name, ticker, status, page_id)
    return page_id


def update_order_record(page_id: str, *, status: str,
                         order_no: str = "", reason: str = "") -> None:
    """"주문중"으로 미리 만들어둔 행을 주문 결과로 갱신한다. 새 행을 만들지 않는다.

    예외를 삼키지 않는다 - 실패하면 그대로 raise. 이 시점엔 이미 주문이
    실제로 나간 뒤라 호출자(kis_client.py)는 fail-open으로 다룬다(주문
    자체는 막지 않고 경고만 보낸다). 갱신이 실패하면 행은 "주문중"으로
    남는데, 그 덕분에 중복 방지(has_order_today)는 계속 작동한다.
    """
    payload = {
        "properties": {
            "상태": {"select": {"name": status}},
            "주문번호": {"rich_text": [{"type": "text", "text": {"content": order_no}}]},
            "사유": _rich_text(reason),
        },
    }
    resp = requests.patch(f"{NOTION_BASE}/pages/{page_id}", headers=_headers(),
                           json=payload, timeout=30)
    resp.raise_for_status()
    logger.info("자동주문 기록 갱신: page_id=%s -> %s", page_id, status)


def _day_range_filter(prop: str, day: date) -> list[dict]:
    """KST 기준 하루 범위로 date 프로퍼티를 거르는 필터 조각."""
    start = f"{day.isoformat()}T00:00:00+09:00"
    end = f"{day.isoformat()}T23:59:59+09:00"
    return [
        {"property": prop, "date": {"on_or_after": start}},
        {"property": prop, "date": {"on_or_before": end}},
    ]


def count_success_orders_today(day: date, account_type: str,
                                side: str = SIDE_BUY,
                                order_type: Optional[str] = None) -> int:
    """오늘(day) + 계좌구분 + 상태가 '성공' 또는 '주문중'인 행 개수. 일일 주문 상한 판단용.

    '주문중'도 센다 - 결과를 아직 모르는 주문(사후 갱신이 실패했거나 아직
    전송 중인 주문)을 상한 계산에서 빼면, 그만큼 상한을 넘겨서 주문할 수
    있게 된다. 예외를 삼키지 않는다 - 실패하면 그대로 raise (fail-closed는
    호출자 책임).
    """
    db_id = os.environ["NOTION_ORDERS_DB_ID"]
    url = f"{NOTION_BASE}/databases/{db_id}/query"
    payload: dict[str, Any] = {
        "filter": {
            "and": [
                *_day_range_filter("주문일시", day),
                {"property": "계좌구분", "select": {"equals": account_type}},
                _side_filter(side),
                *([_order_type_filter(order_type)] if order_type else []),
                {"or": [
                    {"property": "상태", "select": {"equals": "성공"}},
                    {"property": "상태", "select": {"equals": "주문중"}},
                ]},
            ]
        },
        "page_size": 100,
    }

    count = 0
    has_more = True
    start_cursor: Optional[str] = None
    while has_more:
        if start_cursor:
            payload["start_cursor"] = start_cursor
        resp = requests.post(url, headers=_headers(), json=payload, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        count += len(data.get("results", []))
        has_more = data.get("has_more", False)
        start_cursor = data.get("next_cursor")

    return count


def count_orders_by_status_today(day: date, account_type: str,
                                  side: str = SIDE_BUY,
                                  order_type: Optional[str] = None) -> dict[str, int]:
    """오늘(day) + 계좌구분의 상태별(성공/주문중) 건수를 각각 센다.

    count_success_orders_today의 합계 판단 로직은 그대로 두고, 이건
    "왜 상한에 걸렸는지"를 사람이 알 수 있게 분해해서 보여주는 가시성
    전용이다 - 유령 행("주문중")이 그날 슬롯을 얼마나 먹었는지 구분한다.

    예외를 삼키지 않는다 - 실패하면 그대로 raise.
    """
    db_id = os.environ["NOTION_ORDERS_DB_ID"]
    url = f"{NOTION_BASE}/databases/{db_id}/query"
    payload: dict[str, Any] = {
        "filter": {
            "and": [
                *_day_range_filter("주문일시", day),
                {"property": "계좌구분", "select": {"equals": account_type}},
                _side_filter(side),
                *([_order_type_filter(order_type)] if order_type else []),
                {"or": [
                    {"property": "상태", "select": {"equals": "성공"}},
                    {"property": "상태", "select": {"equals": "주문중"}},
                ]},
            ]
        },
        "page_size": 100,
    }

    counts = {"성공": 0, "주문중": 0}
    has_more = True
    start_cursor: Optional[str] = None
    while has_more:
        if start_cursor:
            payload["start_cursor"] = start_cursor
        resp = requests.post(url, headers=_headers(), json=payload, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        for page in data.get("results", []):
            status = (page.get("properties", {}).get("상태", {}).get("select") or {}).get("name")
            if status in counts:
                counts[status] += 1
        has_more = data.get("has_more", False)
        start_cursor = data.get("next_cursor")

    return counts


def count_rejected_orders_today(ticker: str, day: date,
                                 since: Optional[datetime] = None,
                                 side: str = SIDE_BUY) -> int:
    """오늘(day) 이 종목코드로 상태='거부'인 행 개수. 반복 거부 재시도 상한 판단용.

    since를 주면 그 시각 이후 거부만 센다 - 장 시작 전(호가 미확정 구간)에
    쌓인 거부까지 스트라이크에 포함되면, 정작 거래 시간이 시작됐을 때
    이미 차단 상태가 되는 문제가 있다 (kis_client.py의 TRADE_START_TIME).

    예외를 삼키지 않는다 - 실패하면 그대로 raise (fail-closed는 호출자 책임).
    """
    db_id = os.environ["NOTION_ORDERS_DB_ID"]
    url = f"{NOTION_BASE}/databases/{db_id}/query"
    date_filter = _day_range_filter("주문일시", day)
    if since is not None:
        date_filter = [
            {"property": "주문일시", "date": {"on_or_after": since.isoformat()}},
            date_filter[1],  # 그날 끝 시각 상한은 그대로 유지
        ]
    payload: dict[str, Any] = {
        "filter": {
            "and": [
                {"property": "종목코드", "rich_text": {"equals": ticker}},
                _side_filter(side),
                *date_filter,
                {"property": "상태", "select": {"equals": "거부"}},
            ]
        },
        "page_size": 100,
    }

    count = 0
    has_more = True
    start_cursor: Optional[str] = None
    while has_more:
        if start_cursor:
            payload["start_cursor"] = start_cursor
        resp = requests.post(url, headers=_headers(), json=payload, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        count += len(data.get("results", []))
        has_more = data.get("has_more", False)
        start_cursor = data.get("next_cursor")

    return count


def has_order_today(ticker: str, day: date, side: str = SIDE_BUY,
                     order_type: Optional[str] = None) -> bool:
    """오늘(day) 이 종목코드로 이미 주문 시도가 있고 그 상태가 성공/주문중/실패인가.

    "거부"는 뺀다 - 거래소가 명시적으로 안 받았다는 뜻이라 체결 가능성이
    사실상 없다. 터틀은 돌파 당일 진입이 핵심이라, 일시적 오류(호가
    미개시·수량 오류·네트워크)로 거부됐다고 하루를 통째로 막으면 다음날
    갭 때문에 추격금지에 걸릴 위험이 크다. 반대로 "실패"는 응답을 못
    받았을 뿐 실제로는 체결됐을 수 있어 차단을 유지한다("주문중"과 같은
    취급). 반복 거부(구조적 문제로 의심되는 경우)는 이 함수가 아니라
    count_rejected_orders_today + kis_client.py의 재시도 상한이 막는다.

    예외를 삼키지 않는다 - 실패하면 그대로 raise (fail-closed는 호출자 책임).
    """
    db_id = os.environ["NOTION_ORDERS_DB_ID"]
    url = f"{NOTION_BASE}/databases/{db_id}/query"
    payload: dict[str, Any] = {
        "filter": {
            "and": [
                {"property": "종목코드", "rich_text": {"equals": ticker}},
                _side_filter(side),
                *([_order_type_filter(order_type)] if order_type else []),
                *_day_range_filter("주문일시", day),
                {"or": [
                    {"property": "상태", "select": {"equals": "성공"}},
                    {"property": "상태", "select": {"equals": "주문중"}},
                    {"property": "상태", "select": {"equals": "실패"}},
                ]},
            ]
        },
        "page_size": 1,
    }
    resp = requests.post(url, headers=_headers(), json=payload, timeout=30)
    resp.raise_for_status()
    return len(resp.json().get("results", [])) > 0


def count_orders_today(ticker: str, day: date, *, side: str = SIDE_BUY,
                        order_type: Optional[str] = None) -> int:
    """오늘(day) 이 종목코드의 주문 건수. 상태가 성공/주문중/실패인 것만 센다.

    has_order_today()의 "있나 없나"를 건수로 바꾼 것이다 - 추가매수는 하루
    한 종목에 여러 번 허용하되 상한을 둬야 해서 boolean으로는 부족하다.
    "거부"를 빼는 기준은 has_order_today()와 같다(그쪽 독스트링 참고).

    예외를 삼키지 않는다 - 실패하면 그대로 raise (fail-closed는 호출자 책임).
    """
    db_id = os.environ["NOTION_ORDERS_DB_ID"]
    url = f"{NOTION_BASE}/databases/{db_id}/query"
    payload: dict[str, Any] = {
        "filter": {
            "and": [
                {"property": "종목코드", "rich_text": {"equals": ticker}},
                _side_filter(side),
                *([_order_type_filter(order_type)] if order_type else []),
                *_day_range_filter("주문일시", day),
                {"or": [
                    {"property": "상태", "select": {"equals": "성공"}},
                    {"property": "상태", "select": {"equals": "주문중"}},
                    {"property": "상태", "select": {"equals": "실패"}},
                ]},
            ]
        },
        "page_size": 100,
    }
    count = 0
    has_more = True
    start_cursor: Optional[str] = None
    while has_more:
        if start_cursor:
            payload["start_cursor"] = start_cursor
        resp = requests.post(url, headers=_headers(), json=payload, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        count += len(data.get("results", []))
        has_more = data.get("has_more", False)
        start_cursor = data.get("next_cursor")
    return count


def latest_warning_at(reason: str) -> Optional[datetime]:
    """상태='경고' + 사유=reason인 가장 최근 행의 주문일시. 없으면 None.

    경고 알림 억제(kis_client.py의 _notify_warning_throttled) 판단용이다.
    날짜로 거르지 않고 전체에서 가장 최근 1건을 찾는다 - 억제 기준(1시간)이
    자정을 넘길 수 있어 "오늘"로 한정하면 안 된다.

    예외를 삼키지 않는다 - 실패하면 그대로 raise. 호출자(kis_client.py)는
    이 예외를 fail-open으로 다룬다 (판단 안 되면 그냥 알린다). fail-closed인
    has_order_today/count_success_orders_today와는 반대 방향이니 여기서
    임의로 기본값을 만들어 삼키지 않는다 - 그 판단은 호출자 몫이다.
    """
    db_id = os.environ["NOTION_ORDERS_DB_ID"]
    url = f"{NOTION_BASE}/databases/{db_id}/query"
    payload: dict[str, Any] = {
        "filter": {
            "and": [
                {"property": "상태", "select": {"equals": "경고"}},
                {"property": "사유", "rich_text": {"equals": reason}},
            ]
        },
        "sorts": [{"property": "주문일시", "direction": "descending"}],
        "page_size": 1,
    }
    resp = requests.post(url, headers=_headers(), json=payload, timeout=30)
    resp.raise_for_status()
    results = resp.json().get("results", [])
    if not results:
        return None
    d = results[0].get("properties", {}).get("주문일시", {}).get("date")
    if not d or not d.get("start"):
        return None
    return datetime.fromisoformat(d["start"])


def list_pending_orders_today(day: date) -> list[dict]:
    """오늘(day) 상태='주문중'으로 남아 있는 행 목록 ("유령 행" 알림용).

    사전 기록은 됐는데 주문 전송 중 프로세스가 죽으면 이 상태로 남는다.
    자동으로 지우거나 상태를 바꾸지 않는다 - 실제 체결 여부는 사람이
    확인해야 한다 (kis_client.py 쪽 정책).

    예외를 삼키지 않는다 - 실패하면 그대로 raise. 이건 안전장치가 아니라
    알림용 조회라 호출자는 이 실패로 주문 흐름을 막지 않는다.
    """
    db_id = os.environ["NOTION_ORDERS_DB_ID"]
    url = f"{NOTION_BASE}/databases/{db_id}/query"
    payload: dict[str, Any] = {
        "filter": {
            "and": [
                *_day_range_filter("주문일시", day),
                {"property": "상태", "select": {"equals": "주문중"}},
            ]
        },
        "page_size": 100,
    }

    results: list[dict] = []
    has_more = True
    start_cursor: Optional[str] = None
    while has_more:
        if start_cursor:
            payload["start_cursor"] = start_cursor
        resp = requests.post(url, headers=_headers(), json=payload, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        for page in data.get("results", []):
            props = page.get("properties", {})
            results.append({
                "name": _text(props.get("종목명", {})),
                "ticker": _text(props.get("종목코드", {})),
            })
        has_more = data.get("has_more", False)
        start_cursor = data.get("next_cursor")

    return results


# ── 저녁 실행감사 (evening_audit.py) 전용 ────────────────────
#
# 이 배치가 노션에 쓰는 칸은 아래 3개로 한정한다:
#   보유종목 점검표 - "저녁판정 신호유형" / "저녁판정 발생일"
#   매매일지 (청산 기록) - "신호 발생일"
# 아침 배치(daily_report.py)가 쓰는 손절선·진입후 최고가·최근 판정·
# 신호 최초 발생일 등은 절대 건드리지 않는다 (과거 두 주체가 같은 칸을
# 써서 판정이 덮인 전례가 있다).

EVENING_SIGNAL_WRITABLE = ("저녁판정 신호유형", "저녁판정 발생일")


def update_evening_signal(
    page_id: str, signal_type: Optional[str], signal_date: Optional[date],
) -> None:
    """점검표의 '저녁판정 신호유형'/'저녁판정 발생일' 두 칸만 갱신한다.

    signal_type이 None이면 신호 해소로 보고 두 칸 다 비운다. 이 두 칸
    밖의 어떤 프로퍼티도 이 함수는 절대 건드리지 않는다.
    """
    payload = {
        "properties": {
            "저녁판정 신호유형": {
                "select": {"name": signal_type} if signal_type else None
            },
            "저녁판정 발생일": _date_prop(signal_date),
        },
    }
    assert set(payload["properties"]) <= set(EVENING_SIGNAL_WRITABLE)
    resp = requests.patch(f"{NOTION_BASE}/pages/{page_id}", headers=_headers(),
                           json=payload, timeout=30)
    resp.raise_for_status()
    logger.info("[저녁감사] 점검표 신호 갱신: page_id=%s -> %s (%s)",
                page_id, signal_type or "해소", signal_date)


def fetch_orders_today(day: date) -> list[dict]:
    """자동주문 기록 DB에서 오늘(day) 매수 성공분만 읽는다.

    신규·추가 구분 없이 전부 가져온다 - 저녁 리포트 '오늘의 매매'
    섹션은 둘 다 보여준다 (유닛 표시로 구분).
    """
    db_id = os.environ["NOTION_ORDERS_DB_ID"]
    url = f"{NOTION_BASE}/databases/{db_id}/query"
    payload: dict[str, Any] = {
        "filter": {
            "and": [
                *_day_range_filter("주문일시", day),
                _side_filter(SIDE_BUY),
                {"property": "상태", "select": {"equals": "성공"}},
            ]
        },
        "page_size": 100,
    }

    results: list[dict] = []
    has_more = True
    start_cursor: Optional[str] = None
    while has_more:
        if start_cursor:
            payload["start_cursor"] = start_cursor
        resp = requests.post(url, headers=_headers(), json=payload, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        for page in data.get("results", []):
            props = page.get("properties", {})
            results.append({
                "name": _text(props.get("종목명", {})),
                "ticker": _text(props.get("종목코드", {})),
                "price": _number(props.get("주문가", {})),
                "qty": _number(props.get("수량", {})),
                "order_type": _select(props.get("주문유형", {})) or ORDER_NEW,
            })
        has_more = data.get("has_more", False)
        start_cursor = data.get("next_cursor")

    return results


def fetch_orders_in_range(start_day: date, end_day: date,
                           account_type: str) -> list[dict]:
    """자동주문 기록 DB에서 [start_day, end_day] 범위의 행을 상태 불문 전부 읽는다.

    weekly_report.py가 이번 주 매매 내역(상태='성공'만 골라 신규/추가/청산
    으로 분류)과 규칙 작동 점검(상태='거부'/'경고'를 사유별로 집계) 둘 다
    이 함수 하나로 가져다 쓴다 - 상태별로 따로 쿼리하지 않는다.

    fetch_orders_today와 달리 매매구분(매수/매도)을 가리지 않고 전부
    돌려준다 - 매도 성공분이 있어야 '청산' 내역을 알 수 있다.

    dict 키: name/ticker/side(매매구분, 빈 값은 매수로 간주)/
    order_type(주문유형, 빈 값은 신규로 간주)/status/price/qty/reason.
    """
    db_id = os.environ["NOTION_ORDERS_DB_ID"]
    url = f"{NOTION_BASE}/databases/{db_id}/query"
    start = f"{start_day.isoformat()}T00:00:00+09:00"
    end = f"{end_day.isoformat()}T23:59:59+09:00"
    payload: dict[str, Any] = {
        "filter": {
            "and": [
                {"property": "주문일시", "date": {"on_or_after": start}},
                {"property": "주문일시", "date": {"on_or_before": end}},
                {"property": "계좌구분", "select": {"equals": account_type}},
            ]
        },
        "page_size": 100,
    }

    results: list[dict] = []
    has_more = True
    start_cursor: Optional[str] = None
    while has_more:
        if start_cursor:
            payload["start_cursor"] = start_cursor
        resp = requests.post(url, headers=_headers(), json=payload, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        for page in data.get("results", []):
            props = page.get("properties", {})
            results.append({
                "name": _text(props.get("종목명", {})),
                "ticker": _text(props.get("종목코드", {})),
                "side": _select(props.get("매매구분", {})) or SIDE_BUY,
                "order_type": _select(props.get("주문유형", {})) or ORDER_NEW,
                "status": _select(props.get("상태", {})),
                "price": _number(props.get("주문가", {})),
                "qty": _number(props.get("수량", {})),
                "reason": _text(props.get("사유", {})),
            })
        has_more = data.get("has_more", False)
        start_cursor = data.get("next_cursor")

    return results


def _formula_number(prop: dict) -> Optional[float]:
    """formula 프로퍼티(R배수 등)에서 숫자 결과를 추출한다."""
    formula = prop.get("formula", {})
    val = formula.get("number")
    return float(val) if val is not None else None


def fetch_ledger_closed_today(day: date) -> list[tuple[str, dict]]:
    """매매일지(청산 기록)에서 ✱청산일=오늘인 행을 (page_id, dict)로 읽는다.

    dict 키: ticker/name/entry_price/exit_price/exit_reason/r_multiple/
    signal_date(기존 값, 채워져 있으면 건드리지 않는다)/holding_page_id
    (점검표 관계 - 청산 전에 저녁 배치가 추적하던 저녁판정 발생일이
    남아있는지 확인하는 데 쓴다. 점검표 행은 청산 후 구분='청산'으로
    바뀌지만 삭제되지는 않는다).
    """
    db_id = os.environ["NOTION_LEDGER_DB_ID"]
    url = f"{NOTION_BASE}/databases/{db_id}/query"
    payload: dict[str, Any] = {
        "filter": {"and": _day_range_filter("✱ 청산일", day)},
        "page_size": 100,
    }

    results: list[tuple[str, dict]] = []
    has_more = True
    start_cursor: Optional[str] = None
    while has_more:
        if start_cursor:
            payload["start_cursor"] = start_cursor
        resp = requests.post(url, headers=_headers(), json=payload, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        for page in data.get("results", []):
            props = page.get("properties", {})
            relation = props.get("보유종목", {}).get("relation", [])
            results.append((page["id"], {
                "ticker": _text(props.get("종목코드", {})),
                "name": _text(props.get("종목명", {})),
                "entry_price": _number(props.get("✱ 진입가", {})),
                "exit_price": _number(props.get("✱ 청산가", {})),
                "exit_reason": _select(props.get("✱ 청산 사유", {})),
                "r_multiple": _formula_number(props.get("R배수", {})),
                "signal_date": _date_val(props.get("신호 발생일", {})),
                "holding_page_id": relation[0]["id"] if relation else None,
            }))
        has_more = data.get("has_more", False)
        start_cursor = data.get("next_cursor")

    return results


def fetch_ledger_closed_range(
    start_day: date, end_day: Optional[date] = None,
) -> list[tuple[str, dict]]:
    """매매일지(청산 기록)에서 ✱청산일이 [start_day, end_day] 범위인 행을 전부 읽는다.

    end_day를 안 주면 상한 없이(오늘까지) 읽는다. weekly_report.py가
    "이번 주"(start=end=이번 주 범위)와 "자동매매 시작 이후 누적"
    (start=2026-08-18, end 없음) 둘 다 이 함수 하나로 조회한다.

    fetch_ledger_closed_today와 달리 매매일지는 수동 매매도 섞여 있으므로
    호출부가 dict의 holding_page_id를 fetch_auto_holding_page_ids()의
    결과와 대조해 자동매매분만 걸러야 한다 - 여기서는 걸러내지 않는다
    (이 함수는 날짜 범위 조회만 책임진다).

    dict 키: ticker/name/entry_price/entry_atr/exit_price/exit_reason/
    shares/units/r_multiple/delay_days(지연일수, 수식)/entry_date(진입일 -
    kis_client가 안 채웠으면 None)/exit_date/holding_page_id.
    """
    db_id = os.environ["NOTION_LEDGER_DB_ID"]
    url = f"{NOTION_BASE}/databases/{db_id}/query"
    conditions: list[dict] = [
        {"property": "✱ 청산일",
         "date": {"on_or_after": f"{start_day.isoformat()}T00:00:00+09:00"}},
    ]
    if end_day:
        conditions.append({
            "property": "✱ 청산일",
            "date": {"on_or_before": f"{end_day.isoformat()}T23:59:59+09:00"},
        })
    payload: dict[str, Any] = {"filter": {"and": conditions}, "page_size": 100}

    results: list[tuple[str, dict]] = []
    has_more = True
    start_cursor: Optional[str] = None
    while has_more:
        if start_cursor:
            payload["start_cursor"] = start_cursor
        resp = requests.post(url, headers=_headers(), json=payload, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        for page in data.get("results", []):
            props = page.get("properties", {})
            relation = props.get("보유종목", {}).get("relation", [])
            results.append((page["id"], {
                "ticker": _text(props.get("종목코드", {})),
                "name": _text(props.get("종목명", {})),
                "entry_price": _number(props.get("✱ 진입가", {})),
                "entry_atr": _number(props.get("✱ 진입시 ATR", {})),
                "exit_price": _number(props.get("✱ 청산가", {})),
                "exit_reason": _select(props.get("✱ 청산 사유", {})),
                "shares": _number(props.get("✱ 매수 수량", {})),
                "units": _number(props.get("유닛수", {})),
                "r_multiple": _formula_number(props.get("R배수", {})),
                "delay_days": _formula_number(props.get("지연일수", {})),
                "entry_date": _date_val(props.get("진입일", {})),
                "exit_date": _date_val(props.get("✱ 청산일", {})),
                "holding_page_id": relation[0]["id"] if relation else None,
            }))
        has_more = data.get("has_more", False)
        start_cursor = data.get("next_cursor")

    return results


def fetch_holding_evening_signal_date(page_id: str) -> Optional[date]:
    """점검표 특정 행의 '저녁판정 발생일'만 조회한다 (단건 GET).

    청산된 종목이 매매일지에 '신호 발생일'을 아직 안 채운 채로 남아있을
    때, 청산 전에 저녁 배치가 추적하던 발생일을 그대로 이어받기 위해
    쓴다. 못 찾아도(행 삭제 등) 예외를 삼키고 None을 돌려준다 - 이때는
    호출부가 청산일 자체를 기본값으로 쓴다.
    """
    try:
        resp = requests.get(f"{NOTION_BASE}/pages/{page_id}",
                             headers=_headers(), timeout=30)
        resp.raise_for_status()
        props = resp.json().get("properties", {})
        return _date_val(props.get("저녁판정 발생일", {}))
    except Exception as e:                      # noqa: BLE001
        logger.warning("점검표 저녁판정 발생일 조회 실패 (page_id=%s): %s",
                        page_id, e)
        return None


def fetch_holding_avg_price(page_id: str) -> Optional[float]:
    """점검표 특정 행의 '평단가'만 조회한다 (단건 GET).

    weekly_report.py의 보유 현황 섹션이 진입가로 쓴다 - 가중평균이라야
    2유닛 이상 종목의 손익률·R배수가 정확하다(add_auto_holding_units
    참고). 이 기능 도입 전에 만들어진 행이라 평단가가 비어 있으면 None을
    돌려주고, 호출부가 ✱ 매수단가로 폴백한다(1회 매수의 평균은 그
    체결가와 같으므로 안전하다).

    조회 자체가 실패해도 예외를 삼키고 None을 돌려준다 - 리포트 한 종목의
    평단가 조회 실패로 리포트 전체를 막지 않는다.
    """
    try:
        resp = requests.get(f"{NOTION_BASE}/pages/{page_id}",
                             headers=_headers(), timeout=30)
        resp.raise_for_status()
        props = resp.json().get("properties", {})
        return _number(props.get("평단가", {}))
    except Exception as e:                      # noqa: BLE001
        logger.warning("점검표 평단가 조회 실패 (page_id=%s): %s", page_id, e)
        return None


def fetch_holding_buy_date(page_id: str) -> Optional[date]:
    """점검표 특정 행의 '매수일'만 조회한다 (단건 GET).

    kis_client._record_ledger_after_sell()이 매매일지의 '진입일'을 채우는
    데 쓴다 - HoldingInput은 매수일을 들고 있지 않아(core.py는 건드리지
    않는다) 별도 조회가 필요하다. 실패해도 예외를 삼키고 None을 돌려준다 -
    매매일지 기록 자체는 이미 fail-open이라, 진입일 하나 못 채운다고
    청산 기록 전체를 막을 이유가 없다(호출부가 '진입일' 생략으로 처리).
    """
    try:
        resp = requests.get(f"{NOTION_BASE}/pages/{page_id}",
                             headers=_headers(), timeout=30)
        resp.raise_for_status()
        props = resp.json().get("properties", {})
        return _date_val(props.get("매수일", {}))
    except Exception as e:                      # noqa: BLE001
        logger.warning("점검표 매수일 조회 실패 (page_id=%s): %s", page_id, e)
        return None


def update_ledger_signal_date(page_id: str, signal_date: date) -> None:
    """매매일지의 '신호 발생일' 한 칸만 채운다. 이미 값이 있으면 호출하지 않는다.

    (호출부가 fetch_ledger_closed_today의 signal_date가 비어 있을 때만
    부른다 - 여기서는 중복 호출 방지를 다시 확인하지 않는다.)
    """
    payload = {"properties": {"신호 발생일": _date_prop(signal_date)}}
    resp = requests.patch(f"{NOTION_BASE}/pages/{page_id}", headers=_headers(),
                           json=payload, timeout=30)
    resp.raise_for_status()
    logger.info("[저녁감사] 매매일지 신호 발생일 기입: page_id=%s -> %s",
                page_id, signal_date)


# ── KIS 접근토큰 캐시 DB ────────────────────────────────────
#
# kis_client.get_access_token()이 실행마다 무조건 새로 발급받지 않고
# KIS 공식 권장 방식("접근토큰 만기일을 저장하고 있다가 만기 도래 전에
# 갱신")을 쓰도록 돕는다. 단일 행만 쓴다(레코드="KIS_TOKEN" 고정값) -
# 앱키·앱시크릿 조합 하나에 유효 토큰은 하나뿐이라 여러 행을 둘 이유가
# 없다.
#
# 조회·저장 모두 예외를 삼키지 않는다 - 실패하면 그대로 raise. 호출부
# (kis_client.get_access_token)가 fail-open으로 처리한다: 조회 실패는
# 캐싱 없이 매번 새로 발급하는 예전 방식으로 자연스럽게 폴백하고, 저장
# 실패는 무시하고 계속 진행한다(발급 자체는 이미 성공했다) - 이 캐싱
# 기능 때문에 거래가 막히면 안 된다.

TOKEN_CACHE_RECORD = "KIS_TOKEN"


def get_cached_token() -> Optional[tuple[str, datetime]]:
    """캐시된 KIS 접근토큰과 만료시각을 조회한다. 캐시 행이 없거나 값이
    비어 있으면 None.

    Returns:
        (토큰값, 만료시각) 튜플 또는 None
    """
    db_id = os.environ["NOTION_TOKEN_CACHE_DB_ID"]
    url = f"{NOTION_BASE}/databases/{db_id}/query"
    payload: dict[str, Any] = {
        "filter": {"property": "레코드", "title": {"equals": TOKEN_CACHE_RECORD}},
        "page_size": 1,
    }
    resp = requests.post(url, headers=_headers(), json=payload, timeout=30)
    resp.raise_for_status()
    results = resp.json().get("results", [])
    if not results:
        return None

    props = results[0].get("properties", {})
    token = _text(props.get("토큰값", {}))
    date_prop = (props.get("만료시각", {}) or {}).get("date")
    if not token or not date_prop or not date_prop.get("start"):
        return None
    return token, datetime.fromisoformat(date_prop["start"])


def save_cached_token(token: str, expires_at: datetime) -> None:
    """KIS 접근토큰 캐시를 upsert한다 (레코드=KIS_TOKEN, 항상 단일 행).

    이미 행이 있으면 갱신하고, 없으면(최초 실행) 새로 만든다. 갱신일시도
    같이 기록한다 - 이 캐시를 마지막으로 쓴 시각을 사람이 확인할 수
    있도록.
    """
    db_id = os.environ["NOTION_TOKEN_CACHE_DB_ID"]
    now = datetime.now(_KST)

    url = f"{NOTION_BASE}/databases/{db_id}/query"
    payload: dict[str, Any] = {
        "filter": {"property": "레코드", "title": {"equals": TOKEN_CACHE_RECORD}},
        "page_size": 1,
    }
    resp = requests.post(url, headers=_headers(), json=payload, timeout=30)
    resp.raise_for_status()
    results = resp.json().get("results", [])

    properties: dict[str, Any] = {
        "토큰값": _rich_text(token),
        "만료시각": {"date": {"start": expires_at.isoformat()}},
        "갱신일시": {"date": {"start": now.isoformat()}},
    }

    if results:
        page_id = results[0]["id"]
        resp = requests.patch(f"{NOTION_BASE}/pages/{page_id}", headers=_headers(),
                               json={"properties": properties}, timeout=30)
        resp.raise_for_status()
        logger.info("KIS 토큰 캐시 갱신: page_id=%s 만료=%s",
                    page_id, expires_at.isoformat())
    else:
        properties["레코드"] = {
            "title": [{"type": "text", "text": {"content": TOKEN_CACHE_RECORD}}]
        }
        resp = requests.post(
            f"{NOTION_BASE}/pages", headers=_headers(),
            json={"parent": {"database_id": db_id}, "properties": properties},
            timeout=30,
        )
        resp.raise_for_status()
        page_id = resp.json()["id"]
        logger.info("KIS 토큰 캐시 최초 생성: page_id=%s 만료=%s",
                    page_id, expires_at.isoformat())


# ── 자동매매 정지/재개 제어 DB ───────────────────────────────
#
# 사람이 노션 "자동매매 제어" DB의 상태를 실행중/일시정지로 바꾸면
# 신규 진입(run_auto_trade)·추가매수(run_auto_pyramid)가 이를 그대로
# 따른다. 단일 행만 쓴다(레코드="AUTO_TRADE_CONTROL" 고정값) - KIS
# 토큰 캐시 DB와 같은 패턴. 변경일시·사유는 사람이 노션에서 상태를
# 바꿀 때 같이 채우는 칸이라 이 코드는 쓰지 않고 읽기만 한다.
#
# 자동매도(run_auto_sell)는 이 상태를 아예 확인하지 않는다 - 정지는
# 신규 매수만 막고, 손절·추세청산은 항상 나가야 한다.
#
# 조회 실패는 여기서 그대로 raise한다(토큰 캐시 조회와 동일 원칙) -
# 호출부(kis_client)가 fail-closed로 처리한다: 상태를 모를 땐 정지로
# 간주하고 매수를 막는다.

AUTO_TRADE_CONTROL_RECORD = "AUTO_TRADE_CONTROL"
AUTO_TRADE_RUNNING = "실행중"
AUTO_TRADE_PAUSED = "일시정지"


def get_auto_trade_status() -> Optional[str]:
    """자동매매 제어 DB의 현재 상태를 조회한다.

    Returns:
        "실행중" 또는 "일시정지". 행이 아직 없으면(기능을 배포했지만
        아무도 아직 노션에서 건드리지 않은 초기 상태) None을 돌려준다 -
        호출부가 이 경우 "실행중"(기본 동작)으로 다룬다.
    """
    db_id = os.environ["NOTION_AUTO_TRADE_CONTROL_DB_ID"]
    url = f"{NOTION_BASE}/databases/{db_id}/query"
    payload: dict[str, Any] = {
        "filter": {"property": "레코드", "title": {"equals": AUTO_TRADE_CONTROL_RECORD}},
        "page_size": 1,
    }
    resp = requests.post(url, headers=_headers(), json=payload, timeout=30)
    resp.raise_for_status()
    results = resp.json().get("results", [])
    if not results:
        return None

    props = results[0].get("properties", {})
    return _select(props.get("상태", {})) or None
