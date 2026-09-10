"""
intraday_watch.py – 장중 돌파 감시 (10분 간격)

야간 스캔(scan_all.py)이 남긴 data/scan_latest.csv에서 돌파 임박 종목을
추려, 장중 현재가가 실제로 직전 20일 고가를 넘는 순간 카톡으로 알린다.

판정선은 core.CHASE_ATR_MULT를 그대로 쓴다 (app.py의 진입 점검 화면과
같은 값). 이 파일에 숫자를 다시 적지 않는다.

같은 날 같은 종목은 한 번만 알린다 (data/alerted_YYYYMMDD.json).
노션·메일에 쓰지 않는다.

10시 이전에 돌파한 종목은 같은 파일에 10시 가격(price_10am)도 남긴다 -
진입 시각을 09:10에서 10:00으로 바꾸는 게 나은지 나중에 실측 데이터로
비교하기 위한 기록용이며, 매매 판단에는 쓰이지 않는다.

로컬 실행:
    python intraday_watch.py
    python intraday_watch.py --dry-run     # 카톡 발송 없이 출력만
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import pandas as pd
import requests

import core
import kakao
import kis_client
import notion_repo
import scan_all

logger = logging.getLogger(__name__)

# ── 설정 ────────────────────────────────────────────────────

DATA_DIR = scan_all.DATA_DIR
KST = ZoneInfo("Asia/Seoul")

# 워치리스트에 올릴 거리 상한 (×ATR). 넓히면 조회 종목이 늘 뿐
# 돌파 판정 자체는 변하지 않는다.
WATCH_THRESHOLD = float(os.environ.get("WATCH_THRESHOLD") or 1.0)
MAX_WATCH = 100

NAVER_URL = "https://polling.finance.naver.com/api/realtime/domestic/stock/{codes}"
# 이 엔드포인트는 콤마로 여러 종목을 한 번에 준다. 100종목이 개별 100회가
# 아니라 5회로 끝나므로, 차단 위험도 실행 시간도 크게 준다.
BATCH_SIZE = 20
TIMEOUT = 5
RETRY_MAX = 2
BATCH_SLEEP = 0.2
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)

# 카톡 한 통 200자 제한(kakao.MAX_TEXT_LEN). 줄마다 종목분석 딥링크가
# 붙어 종목당 글자 수가 들쭉날쭉하므로 고정 개수가 아니라 실제 글자
# 수를 채워가며 나눈다 (build_messages 참고).
MAX_MESSAGES = 5           # 통 자체가 이보다 많아지면 "외 N종목"으로 접는다

STATUS_ENTER = "진입가능"
STATUS_CHASE = "추격금지"


def alerted_path(day: str) -> Path:
    return DATA_DIR / f"alerted_{day}.json"


# daily_report.py가 아침에 한 번 계산해 남긴다. 장중에 노션을 매번
# 조회하지 않기 위한 캐시라 여기서는 읽기만 한다.
CORR_UNITS_PATH = DATA_DIR / "corr_units.json"


def load_corr_units() -> Optional[dict]:
    """상관군별 누적 유닛 캐시. 없거나 깨졌으면 None (로그만 남기고 계속)."""
    if not CORR_UNITS_PATH.exists():
        logger.warning("%s가 없습니다 (아침 배치를 아직 안 돌렸을 수 있습니다) "
                       "– 유닛 여유 표시를 생략합니다.", CORR_UNITS_PATH.name)
        return None
    try:
        return json.loads(CORR_UNITS_PATH.read_text(encoding="utf-8"))
    except Exception as e:                  # noqa: BLE001
        logger.warning("%s를 읽지 못했습니다 (%s) – 유닛 여유 표시를 생략합니다.",
                       CORR_UNITS_PATH.name, e)
        return None


# ── 야간 스캔 신선도 ──────────────────────────────────────────
#
# load_watchlist()는 scan_date를 로그에만 찍고 신선도는 검사하지 않았다.
# 야간 작업(scan_all.py)이 실패하거나 스케줄이 아예 안 돌면(GitHub
# Actions 스케줄이 0건 실행된 전례가 있다) scan_latest.csv가 며칠 전
# 기준선·ATR을 그대로 들고 있는데, 장중 감시는 그걸 알 방법이 없어
# 계속 낡은 기준으로 매수해왔다. 신규 진입만 막는다 - 보유 종목의
# 청산(run_auto_sell)·추가매수(run_auto_pyramid)는 스캔 신선도와
# 무관하게 항상 작동해야 한다(run()에서 이 검사보다 먼저, 무조건 실행).

def _expected_scan_date(today: date) -> date:
    """오늘 기준 '직전 거래일'의 근사치 - 주말만 건너뛴다.

    실제 거래일 달력(pykrx)을 조회하지 않고 로컬로만 계산한다 - 이
    신선도 검사 자체가 pykrx/KRX 장애에 발목잡히면 안 된다(원래 목적은
    "야간 스캔이 아예 안 돌았다"를 잡는 것이지, 공휴일까지 정밀하게
    맞히려는 게 아니다). 평일 공휴일 다음날엔 기대값을 실제보다 하루
    늦게(더 최근으로) 잡아 오탐(불필요한 경고 + 그날 신규진입 보류)이
    날 수 있는데, 이건 안전한 방향의 오차다 - 반대로 못 잡으면(누락)
    낡은 기준선으로 계속 매수하는 실제 사고로 이어진다.
    """
    d = today - timedelta(days=1)
    while d.weekday() >= 5:      # 5=토, 6=일
        d -= timedelta(days=1)
    return d


def _scan_is_stale(scan_date_str: Optional[str], today: date) -> tuple[bool, date]:
    """scan_latest.csv의 scan_date(YYYYMMDD 문자열)가 '직전 거래일'이 아니면 True.

    scan_date가 아예 없거나(파일 없음) 형식이 깨졌으면 안전 쪽으로
    신선하지 않다(True)고 본다.
    """
    expected = _expected_scan_date(today)
    if not scan_date_str:
        return True, expected
    try:
        scan_date = datetime.strptime(str(scan_date_str), "%Y%m%d").date()
    except ValueError:
        return True, expected
    return scan_date != expected, expected


# ── 워치리스트 ──────────────────────────────────────────────

def load_watchlist(threshold: float = WATCH_THRESHOLD,
                   limit: int = MAX_WATCH) -> pd.DataFrame:
    """scan_latest.csv에서 감시 대상을 고른다. 없으면 빈 DataFrame.

    아직 돌파하지 않은 종목(status="임박")만 본다. 어젯밤 이미 돌파로
    기록된 종목은 '넘는 순간'이 지났으므로 장 시작과 동시에 전부
    알림이 나가버린다 — 그건 새 신호가 아니라 어제 스캔의 반복이다.
    """
    frame = scan_all.load_scan()
    if frame is None:
        logger.warning("%s가 없습니다. 야간 스캔을 먼저 돌리세요.",
                       scan_all.LATEST_PATH.name)
        return pd.DataFrame()

    watch = frame[
        (frame["status"] == scan_all.STATUS_NEAR)
        & (frame["dist_atr"] <= threshold)
    ]
    watch = watch.sort_values("dist_atr").head(limit)
    logger.info("워치리스트 %d종목 (기준일 %s · dist_atr ≤ %.1f)",
                len(watch),
                frame["scan_date"].iloc[0] if not frame.empty else "?",
                threshold)
    return watch


# ── 시세 조회 (네이버 JSON) ─────────────────────────────────

def _parse(datas: list[dict]) -> dict[str, float]:
    """응답에서 종목코드 → 현재가. 거래정지·장마감은 제외한다.

    장이 열려 있지 않으면 closePrice가 직전 거래일 종가다. 그대로 쓰면
    휴장일에 어제 종가로 돌파 알림이 나간다 — marketStatus로 막는다.
    """
    prices: dict[str, float] = {}
    for item in datas:
        code = str(item.get("itemCode", "")).zfill(6)
        if item.get("marketStatus") != "OPEN":
            logger.debug("[%s] 장중 아님 (%s) – 건너뜀",
                         code, item.get("marketStatus"))
            continue
        if item.get("tradableStatus") != "tradable":
            logger.debug("[%s] 거래불가 (%s) – 건너뜀",
                         code, item.get("tradableStatus"))
            continue
        raw = str(item.get("closePrice", "")).replace(",", "").strip()
        try:
            price = float(raw)
        except ValueError:
            logger.debug("[%s] 현재가 해석 불가: %r", code, raw)
            continue
        if price > 0:
            prices[code] = price
    return prices


def _fetch_batch(codes: list[str]) -> dict[str, float]:
    """한 배치. 실패하면 빈 dict (전체를 멈추지 않는다)."""
    url = NAVER_URL.format(codes=",".join(codes))
    for attempt in range(RETRY_MAX + 1):
        try:
            resp = requests.get(
                url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT,
            )
            resp.raise_for_status()
            return _parse(resp.json().get("datas", []))
        except Exception as e:              # noqa: BLE001 – 무엇이든 재시도
            if attempt < RETRY_MAX:
                logger.debug("배치 조회 실패 (%d/%d): %s",
                             attempt + 1, RETRY_MAX + 1, e)
                time.sleep(BATCH_SLEEP * 2)
            else:
                logger.warning("배치 조회 포기 (%d종목): %s", len(codes), e)
    return {}


def fetch_prices(codes: list[str]) -> dict[str, float]:
    """현재가 일괄 조회. 실패한 배치는 조용히 빠진다."""
    prices: dict[str, float] = {}
    for i in range(0, len(codes), BATCH_SIZE):
        batch = codes[i:i + BATCH_SIZE]
        prices.update(_fetch_batch(batch))
        time.sleep(BATCH_SLEEP)

    missing = len(codes) - len(prices)
    if missing:
        logger.info("현재가 확보 %d/%d종목 (미확보 %d)",
                    len(prices), len(codes), missing)
    return prices


# ── 알림 이력 ───────────────────────────────────────────────

def load_alerted(day: str) -> dict[str, dict]:
    """오늘 이미 알린 종목. 파일이 깨졌으면 빈 dict로 시작한다."""
    path = alerted_path(day)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("alerted", {})
    except Exception as e:                  # noqa: BLE001
        logger.warning("알림 이력을 읽지 못했습니다 (%s) – 새로 시작합니다", e)
        return {}


def _retry_candidates(alerted: dict[str, dict], today: date) -> list[dict]:
    """알림은 갔지만 아직 매수가 확정되지 않은 종목을 다시 후보로 만든다.

    예전에는 "알렸다"(alerted에 있음)와 "샀다"를 구분하지 않아서, 09:30에
    일시적 조회 실패로 매수를 못 해도 09:40에 조건이 좋아지면 재시도할
    방법이 없었다 - alerted에 있다는 이유만으로 다음 회차 워치리스트에서
    아예 빠졌기 때문이다. 이제 alerted는 "카톡 알림을 오늘 이미
    보냈는가"만 의미하고, "오늘 이 종목을 다시 사려 시도할까"는 노션
    자동주문 기록(has_order_today, 기존 중복방지 로직 재사용)으로 매
    회차 새로 판단한다 - 성공/주문중/실패 중 하나라도 있으면(주문중은
    결과 불명 = 체결됐을 수 있어 재시도 대상에서 제외) 더 이상 재시도하지
    않는다. 노션 조회 자체가 실패하면 fail-closed로 이번 회차는 재시도
    보류(다음 회차에 다시 시도).
    """
    retries: list[dict] = []
    for ticker, rec in alerted.items():
        if rec.get("status") != STATUS_ENTER:
            continue  # 추격금지였던 종목은 애초에 사려던 게 아니었다
        try:
            already = notion_repo.has_order_today(
                ticker, today, side=notion_repo.SIDE_BUY,
                order_type=notion_repo.ORDER_NEW,
            )
        except Exception as e:              # noqa: BLE001
            logger.error("[%s] 주문 이력 조회 실패 - 이번 회차 재시도 보류: %s",
                         ticker, e)
            continue
        if already:
            continue
        retries.append({
            "ticker": ticker,
            "name": rec.get("name", ticker),
            "sector": rec.get("sector", ""),
            "market": rec.get("market", ""),
            "price": rec.get("price"),
            "high20": rec.get("high20"),
            "atr20": rec.get("atr20"),
            "gap_atr": rec.get("gap_atr", 0.0),
            "unit_shares": rec.get("unit_shares", 0),
            "status": STATUS_ENTER,
        })
    return retries


def save_alerted(day: str, alerted: dict[str, dict]) -> Path:
    path = alerted_path(day)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"date": day, "alerted": alerted},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


# ── 10시 가격 기록 (진입 시각 룰 비교용) ─────────────────────
#
# 09:10 vs 10:00 중 어느 시각에 진입하는 게 나은지 나중에 실측
# 데이터로 비교하기 위해, 10시 이전에 돌파가 잡힌 종목은 10시 가격도
# 같이 남긴다. TRADE_START_TIME 자체를 바꾸는 게 아니라 기록만 늘리는
# 것이라 매매 로직에는 영향이 없다. ohlcv_cache.parquet에 이미 있는
# 이후 가격 흐름과 로컬에서 그대로 이어붙일 수 있도록 노션이 아니라
# 이 파일(git 커밋으로 영속화됨)에 같이 둔다.
PRICE_10AM_CUTOFF = "10:00"


def _fill_10am_prices(alerted: dict[str, dict], now: datetime) -> bool:
    """10시 이전 돌파 종목 중 10시 가격이 없는 것만 채운다.

    10시가 되기 전엔 아무것도 하지 않는다 - 채워봐야 10시 가격이
    아니다. 이미 채워진 종목은 다시 조회하지 않으므로 10분마다 도는
    회차에서 자연히 한 번만 채워진다 (그 회차가 밀리면 실제 채운
    시각을 time_10am에 같이 남겨 정확히 10:00이 아님을 알 수 있게
    한다).
    """
    if now.strftime("%H:%M") < PRICE_10AM_CUTOFF:
        return False

    targets = [
        ticker for ticker, rec in alerted.items()
        if rec.get("time", "") < PRICE_10AM_CUTOFF and "price_10am" not in rec
    ]
    if not targets:
        return False

    prices = fetch_prices(targets)
    if not prices:
        return False

    for ticker, price in prices.items():
        alerted[ticker]["price_10am"] = price
        alerted[ticker]["time_10am"] = now.strftime("%H:%M")

    logger.info("10시 가격 기록: %d/%d종목", len(prices), len(targets))
    return True


# ── 판정 ────────────────────────────────────────────────────

def judge(row: pd.Series, price: float) -> Optional[dict]:
    """현재가가 다음 거래일 기준선(high20_next)을 넘었는가. 아니면 None.

    scan_all.py의 high20(= 스캔 당일을 뺀 20일 고가)을 장중 판정에 그대로
    쓰면 기준선이 하루 밀린다 - 오늘(장중) 감시는 "어제까지(당일 포함)
    20일 고가"를 넘었는지 봐야 하는데, high20은 "그제까지"의 고가이기
    때문이다. 그래서 high20_next(= 스캔 당일 종가까지 포함한 20일 고가)를
    쓴다. 구버전 scan_latest.csv(컬럼 추가 전에 만들어진 파일)에는 이
    컬럼이 없을 수 있어 그때만 high20으로 대체하고 경고를 남긴다 -
    대체값은 하루 밀린 기준선이라는 걸 알고 써야 한다.

    돌파 폭이 CHASE_ATR_MULT×ATR을 넘으면 추격 구간으로 본다 —
    app.py의 entry_state()와 같은 기준이다.
    """
    high20_next = row.get("high20_next")
    if high20_next is None or pd.isna(high20_next):
        logger.warning(
            "[%s] high20_next 컬럼이 없습니다(구버전 scan_latest.csv) - "
            "high20(하루 밀린 기준선)으로 대체합니다. 야간 스캔을 다시 "
            "돌려 최신 컬럼을 채우세요.", row.get("ticker"),
        )
        high20 = float(row["high20"])
    else:
        high20 = float(high20_next)
    if price <= high20:
        return None

    atr = float(row["atr20"])
    gap_atr = (price - high20) / atr if atr > 0 else 0.0
    return {
        "ticker": str(row["ticker"]),
        "name": str(row["name"]),
        "sector": str(row.get("sector", "") or "").strip(),
        # 자동매수가 노션에 편입할 때 '시장' 칸을 채우는 데 쓴다.
        "market": str(row.get("market", "") or "").strip(),
        "price": price,
        "high20": high20,
        "atr20": atr,
        "gap_atr": gap_atr,
        "unit_shares": int(row["unit_shares"]),
        "status": (STATUS_ENTER if gap_atr <= core.CHASE_ATR_MULT
                   else STATUS_CHASE),
    }


# ── 카톡 메시지 ─────────────────────────────────────────────

def _unit_suffix(hit: dict, corr: Optional[dict]) -> str:
    """'전체 8/12 · 반도체 5/6(추정)' 같은 유닛 여유 표시.

    돌파 알림 시점의 종목은 아직 보유 전이라 어느 상관군에 속할지
    시스템이 알 수 없다 (상관군은 사용자가 직접 판단해 넣는 값이지
    업종에서 자동으로 결정되지 않는다). 그래서:
      - 전체 유닛은 항상 정확하므로 그대로 표시
      - 상관군은 종목 업종명이 기존 상관군 이름과 정확히 일치할 때만
        참고용으로 붙이고, 추정임을 표기한다. 일치하지 않으면 생략.
    """
    if not corr:
        return ""

    total = corr.get("total_units", 0) or 0
    suffix = f" 전체 {total:g}/{core.MAX_UNITS_TOTAL}"
    if total >= core.MAX_UNITS_TOTAL:
        suffix += "⚠상한"

    sector = hit.get("sector", "")
    group_units = corr.get("groups", {}) or {}
    if sector and sector in group_units:
        suffix += f" · {sector} {group_units[sector]:g}/{core.MAX_UNITS_GROUP}(추정)"

    return suffix


def build_messages(
    hits: list[dict], now: datetime, corr: Optional[dict] = None,
) -> list[str]:
    """진입가능 종목만 한 줄씩. 200자를 넘기지 않게 여러 통으로 나눈다.

    추격금지는 넣지 않는다 — 어차피 사지 않을 종목이라 알림으로서
    행동을 유발하지 않고, 진짜 신호를 밀어낸다. CSV와 스트림릿에는
    그대로 남아 있다.

    20일 고가는 뺐다. 돌파했다는 사실 자체가 알림이고, 정확한 수치는
    스트림릿에서 본다.

    줄마다 종목분석 딥링크(core.build_stock_link)를 붙인다. 카카오톡
    리스트 템플릿은 image_url이 사실상 필수이고 기본템플릿 표시 개수가
    문서와 실사용 후기가 엇갈려(3개 vs 5개) 텍스트 템플릿을 유지하기로
    했다 — 링크는 카카오톡이 본문 URL을 자동으로 탭 가능하게 바꿔준다.
    STREAMLIT_APP_URL이 없으면 build_stock_link()가 빈 문자열을 돌려주고
    링크 없이 기존과 같은 줄로 나간다.
    """
    enterable = [h for h in hits if h["status"] == STATUS_ENTER]
    if not enterable:
        return []

    enterable = core.sort_by_gap(enterable)
    header = f"[돌파]{now.strftime('%H:%M')}"

    lines: list[str] = []
    for h in enterable:
        line = f"{h['name']} {h['price']:,.0f} +{h['gap_atr']:.2f}N"
        link = core.build_stock_link(h["ticker"])
        if link:
            line += f" {link}"
        line += _unit_suffix(h, corr)
        lines.append(line)

    # 링크 유무로 종목당 글자 수가 들쭉날쭉해 고정 개수로는 못 묶는다.
    # 200자를 실제로 채우는 만큼씩 그리디하게 묶는다.
    batches: list[list[str]] = []
    current: list[str] = []
    current_len = len(header)
    for line in lines:
        extra = len(line) + 1        # 줄바꿈 포함
        if current and current_len + extra > kakao.MAX_TEXT_LEN:
            batches.append(current)
            current, current_len = [], len(header)
        current.append(line)
        current_len += extra
    if current:
        batches.append(current)

    overflow = 0
    if len(batches) > MAX_MESSAGES:
        overflow = sum(len(b) for b in batches[MAX_MESSAGES:])
        batches = batches[:MAX_MESSAGES]

    messages = ["\n".join([header] + b) for b in batches]
    if overflow:
        tail = f"외 {overflow}종목"
        if len(messages[-1]) + 1 + len(tail) <= kakao.MAX_TEXT_LEN:
            messages[-1] += f"\n{tail}"
        else:
            messages.append(f"{header}\n{tail}")
    return messages


# ── 실행 ────────────────────────────────────────────────────

def run(dry_run: bool = False) -> int:
    now = datetime.now(KST)
    day = now.strftime("%Y%m%d")

    # 자동매도를 돌파 감시보다 먼저 돌린다. 아래는 "감시 종목 없음 / 이미
    # 전부 알림 / 신규 돌파 없음"으로 일찍 return하는 경로가 여럿이라,
    # 매도를 뒤에 두면 대부분의 회차에서 통째로 건너뛴다 - 청산은 돌파
    # 여부와 무관하게 매 회차 확인해야 한다.
    #
    # dry_run에선 실행하지 않는다 (메시지 미리보기 전용 모드라 실주문이
    # 나가면 안 된다). 예외는 여기서 흡수한다 - 매도 쪽 문제로 돌파
    # 알림까지 죽으면 안 되고, 내부 함수들이 각자 카톡으로 이미 알린다.
    if not dry_run:
        try:
            kis_client.run_auto_sell()
        except Exception as e:              # noqa: BLE001
            logger.error("자동매도 실패: %s", e)

        # 추가매수도 돌파 감시와 무관하게 매 회차 확인한다 (보유 종목의
        # 다음 유닛 레벨 도달 여부라 워치리스트와 관계가 없다). 신규 진입
        # 보다 먼저 본다 - 이미 이익 구간에 든 포지션을 키우는 쪽이 터틀
        # 우선순위이고, 일일 주문 상한을 신규 진입과 나눠 쓴다.
        try:
            kis_client.run_auto_pyramid()
        except Exception as e:              # noqa: BLE001
            logger.error("추가매수 실패: %s", e)

    # 10시 가격 기록도 돌파 감시와 무관하게 매 회차 확인한다 (오늘 이미
    # 돌파가 잡힌 종목의 뒤늦은 관찰이라 워치리스트 상태와 관계없다).
    alerted = load_alerted(day)
    if not dry_run:
        try:
            if _fill_10am_prices(alerted, now):
                save_alerted(day, alerted)
        except Exception as e:              # noqa: BLE001
            logger.error("10시 가격 기록 실패: %s", e)

    # 야간 스캔 신선도 - scan_date가 "직전 거래일"이 아니면 신규 진입만
    # 보류한다(위에서 이미 실행한 청산·추가매수는 영향받지 않는다).
    # 경고는 dry_run에선 보내지 않는다(카톡이 실제로 나가면 안 되는 모드).
    scan_frame = scan_all.load_scan()
    scan_date_str = (str(scan_frame["scan_date"].iloc[0])
                     if scan_frame is not None and not scan_frame.empty else None)
    scan_is_stale, expected_scan_date = _scan_is_stale(scan_date_str, now.date())
    if scan_is_stale and not dry_run:
        kis_client._notify_warning_throttled(
            kis_client.WARN_STALE_SCAN,
            f"[KIS] ⚠ 야간 스캔 기준일이 오래됐습니다"
            f"(scan_date={scan_date_str or '없음'}, 기대 {expected_scan_date:%Y%m%d}) - "
            f"신규 진입을 보류합니다. 보유 종목 청산·추가매수는 정상 작동합니다.",
        )

    watch = load_watchlist()

    # 재시도 대상: 이전 회차에 이미 카톡 알림은 갔지만("alerted"에 있음)
    # 아직 매수가 확정되지 않은 종목. dry_run에선 만들지 않는다(실주문
    # 재시도가 목적이라 미리보기 모드와 안 맞는다).
    retry = [] if dry_run else _retry_candidates(alerted, now.date())

    if watch.empty:
        logger.info("감시할 종목이 없습니다.")
        if not retry:
            return 0
        pending = watch  # 빈 DataFrame - 아래 hits는 자연히 빈 리스트
    else:
        pending = watch[~watch["ticker"].isin(list(alerted))]

    hits: list[dict] = []
    if not pending.empty:
        codes = pending["ticker"].tolist()
        prices = fetch_prices(codes)
        for _, row in pending.iterrows():
            price = prices.get(str(row["ticker"]))
            if price is None:
                continue            # 조회 실패는 조용히 건너뛴다(다음 회차에 재시도)
            hit = judge(row, price)
            if hit:
                hits.append(hit)

    if not hits and not retry:
        logger.info("신규 돌파 없음 · 재시도 대상 없음 (감시 %d종목)", len(pending))
        return 0

    messages: list[str] = []
    if hits:
        for h in hits:
            logger.info("돌파: %s(%s) %s원 → %s (+%.2fN)",
                        h["name"], h["ticker"], f"{h['price']:,.0f}",
                        h["status"], h["gap_atr"])

        corr = load_corr_units()
        messages = build_messages(hits, now, corr)
        if not messages:
            # 전부 추격금지 – 카톡은 보내지 않지만 이력에는 남긴다.
            # 남기지 않으면 10분 뒤 같은 종목을 또 판정하게 된다.
            logger.info("돌파 %d종목이 모두 추격 구간 – 발송하지 않습니다", len(hits))

    if dry_run:
        for msg in messages:
            print("─" * 40)
            print(msg)
            print(f"({len(msg)}자)")
        return 0

    if messages:
        try:
            for msg in messages:
                kakao.send_kakao_message(msg)
            logger.info("카톡 %d통 발송", len(messages))
        except Exception as e:              # noqa: BLE001
            # 카톡 발송 실패(토큰 만료 등)가 아래 자동매수·알림 이력
            # 저장까지 막으면 안 된다 - 판정은 이미 끝났고 실제 매매가
            # 알림보다 중요하다. run_auto_sell/run_auto_pyramid와 같은
            # fail-open 원칙.
            logger.error("카톡 발송 실패: %s", e)

    # 자동매수: 이번 회차 신규 돌파(진입가능) + 이전 회차에 알림은 갔지만
    # 아직 체결되지 않은 재시도 대상을 합쳐서 넘긴다. 신호 알림(위)은 이
    # 실패와 무관하게 이미 나갔다 - kis_client.run_auto_trade() 내부
    # 함수들은 각자 실패를 이미 카톡으로 알리므로, 여기서 잡히는 예외는
    # 그 안전장치들까지 넘어온 진짜 예상 밖의 오류다. 알림 이력 저장까지
    # 막으면 안 되니 여기서 흡수한다.
    enterable = [h for h in hits if h["status"] == STATUS_ENTER] + retry
    if retry:
        logger.info("재시도 대상 %d종목 (이전 회차 알림 · 아직 미체결)", len(retry))
    if enterable:
        if scan_is_stale:
            logger.info(
                "야간 스캔 신선도 불량(scan_date=%s, 기대=%s) - 신규 진입 %d건 보류",
                scan_date_str, expected_scan_date, len(enterable),
            )
        else:
            try:
                kis_client.run_auto_trade(enterable)
            except Exception as e:              # noqa: BLE001
                logger.error("자동매수 실패: %s", e)

    if hits:
        for h in hits:
            alerted[h["ticker"]] = {
                "time": now.strftime("%H:%M"),
                "name": h["name"],
                "sector": h.get("sector", ""),
                "market": h.get("market", ""),
                "price": h["price"],
                "high20": h["high20"],
                "atr20": h["atr20"],
                "gap_atr": round(h["gap_atr"], 3),
                "unit_shares": h["unit_shares"],
                "status": h["status"],
            }
        path = save_alerted(day, alerted)
        logger.info("알림 이력 저장: %s (%d종목)", path.name, len(alerted))
    return 0


def main() -> int:
    import argparse

    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s [%(levelname)s] %(message)s",
        stream=sys.stdout,
    )
    logger.setLevel(logging.INFO)
    # kis_client/notion_repo는 root(WARNING)를 물려받아 기본으로는 조용하다.
    # 자동매수 진단(시간 게이트 판단, 상한 상세 분해, 유령 행 등)이 워크플로
    # 로그에 실제로 찍히게 여기서 같이 올린다.
    logging.getLogger("kis_client").setLevel(logging.INFO)
    logging.getLogger("notion_repo").setLevel(logging.INFO)

    parser = argparse.ArgumentParser(description="장중 돌파 감시")
    parser.add_argument("--dry-run", action="store_true",
                        help="카톡을 보내지 않고 메시지만 출력한다")
    args = parser.parse_args()

    try:
        return run(dry_run=args.dry_run)
    except Exception as e:                  # noqa: BLE001
        # 10분마다 도는 작업이다. 한 번 실패해도 다음 실행이 이어받게
        # 비정상 종료로 남기되, 다른 워크플로에는 영향이 없다.
        logger.error("장중 감시 실패: %s", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
