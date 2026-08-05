"""
screener.py – KOSPI·KOSDAQ 전종목 돌파 스크리너

터틀 진입 트리거(20일 고가 돌파)에 거래량 급증을 얹어 후보를 추린다.
ATR·유닛·손절선·기대값 계산은 전부 core.py를 그대로 호출한다.
이 파일은 조회·필터·정렬·저장만 담당하고 계산식을 다시 구현하지 않는다.

[조회 경로]
1순위 — 날짜별 전종목 일괄: get_market_ohlcv(날짜, market="ALL")
        120거래일이면 120회. 첫 실행 이후에는 새 거래일만 받으므로 하루 1회.
2순위 — 종목별 개별(폴백): get_market_ohlcv(시작, 종료, 티커)
        기간 전체를 한 번에 주므로 종목당 1회, 2,700종목이면 2,700회.
        1순위가 3회 재시도까지 실패할 때만 쓴다.
        대상은 data/universe.json의 티커 목록이다.
어느 경로를 썼는지는 결과 JSON의 source·notes에 남긴다.

[캐시]
data/ohlcv_cache.parquet 에 (날짜, 티커) 단위로 누적 저장한다.
두 경로 모두 캐시에 없는 구간만 받는다.

CLI 테스트:
    python screener.py
    python screener.py --end 2026-07-31 --capital 10000000
"""

from __future__ import annotations

import json
import logging
import os
import random
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

import pandas as pd
from pykrx import stock as krx

# .env 지원 (로컬 테스트용, 없으면 무시)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import core

logger = logging.getLogger(__name__)


# ── 필터 기준 ───────────────────────────────────────────────

# 시가총액 대신 거래대금을 쓴다. 시총 조회(get_market_cap_by_ticker)가
# 응답하지 않는 상황에서도 OHLCV만으로 유동성을 걸러야 하고,
# 거래대금 = 종가 × 거래량이라 이미 받은 데이터 안에 들어 있다.
MIN_TRADING_VALUE = 1_000_000_000   # 20일 평균 거래대금 10억
TRADING_VALUE_DAYS = 20             # 평균 거래대금 산정일 (당일 포함)

# 거래량배수 임계값·평균일수는 core.py에 있다 — scan_all.py도 같은
# 정의를 써야 두 화면의 배수가 갈리지 않는다.
MIN_LISTED_DAYS = 120               # 상장 거래일 – ATR 워밍업에 필요
MAX_RESULTS = 30                    # 결과 상한
SCAN_DAYS = 120                     # 캐시에 유지할 거래일 수
MARKETS = ("KOSPI", "KOSDAQ")       # 코넥스는 제외

# 우선주는 보통주와 별개로 20일 고가를 돌파해도 의미가 다르고, 스팩은
# 사업 실체가 없다. ETF·ETN은 애초에 조회 대상(주식 목록)에 들어오지
# 않지만 이름으로 한 번 더 막는다.
PREF_SHARE_SUFFIXES = ("5", "7", "9")       # 우선주 코드 끝자리
EXCLUDE_NAME_KEYWORDS = ("스팩", "ETN")

# ── 호출 정책 (KRX 차단 방지) ───────────────────────────────

SLEEP_MIN, SLEEP_MAX = 0.3, 0.5     # 호출 사이 대기 (초)
FALLBACK_SLEEP = 0.4                # 종목별 폴백 경로의 고정 간격
RETRY_MAX = 3                       # 재시도 횟수
RETRY_BACKOFF = (2, 4, 8)           # 재시도 간격 (초)
# 폴백은 2,700회를 도니 첫 실행이 20분 넘게 걸릴 수 있다.
# 캐시가 쌓이면 이후에는 새 거래일 구간만 받는다.
TIME_BUDGET_SEC = 30 * 60
# 연속으로 이만큼 실패하면 개별 종목 문제가 아니라 KRX 쪽 문제로 본다
MAX_CONSECUTIVE_FAILS = 20

# ── 경로 ────────────────────────────────────────────────────

DATA_DIR = Path(__file__).resolve().parent / "data"
CACHE_PATH = DATA_DIR / "ohlcv_cache.parquet"
SECTOR_PATH = DATA_DIR / "sector_map.json"
UNIVERSE_PATH = DATA_DIR / "universe.json"
SECTOR_MAX_AGE_DAYS = 7             # 업종 구성은 자주 안 바뀐다

CACHE_COLUMNS = ["날짜", "티커", "시가", "고가", "저가", "종가", "거래량"]

# 시세 달력을 뽑을 기준 종목. 전종목 조회는 거래일에만 응답하므로,
# 어느 날이 거래일인지 먼저 알아야 헛호출을 하지 않는다.
CALENDAR_TICKER = "005930"          # 삼성전자

SOURCE_BULK = "전종목 일괄 조회"
SOURCE_PER_TICKER = "종목별 개별 조회 (폴백)"
SOURCE_CACHE = "캐시 (추가 조회 없음)"


class KrxUnavailable(RuntimeError):
    """재시도까지 실패 – 그날 스캔을 중단한다."""


def check_krx_auth() -> Optional[str]:
    """KRX 로그인 자격증명 확인. 없으면 안내 문구를 돌려준다.

    KRX가 전종목 시세·시가총액·종목목록·업종지수를 로그인 뒤로 옮겼다.
    개별종목 시세만 비로그인으로 열려 있어, 자격증명이 없으면 1순위
    경로가 통째로 막히고 폴백으로 내려간다. 그때 원인이 '3회 실패'로만
    보이지 않도록 미리 알린다.

    pykrx는 import 시점에 로그인 세션을 만들므로, 이 값들은 프로세스가
    뜨기 전에(GitHub Actions는 step env로) 설정돼 있어야 한다.
    """
    if os.environ.get("KRX_ID") and os.environ.get("KRX_PW"):
        return None
    return (
        "KRX_ID/KRX_PW가 없습니다. KRX가 전종목·시가총액·종목목록·업종 조회를 "
        "로그인 뒤로 옮겨서, 이 값이 없으면 전종목 일괄 조회와 업종 맵이 "
        "실패하고 종목별 개별 조회로 내려갑니다. (pykrx 1.2 이상 필요)"
    )


# ── 호출 유틸 ───────────────────────────────────────────────

def _sleep(seconds: Optional[float] = None) -> None:
    """KRX 호출 사이 간격."""
    time.sleep(seconds if seconds is not None
               else random.uniform(SLEEP_MIN, SLEEP_MAX))


def _call(label: str, fn: Callable, quiet: bool = False):
    """KRX 호출 + 재시도(2초→4초→8초).

    Args:
        quiet: 실패 로그를 debug로 낮춘다 (폴백에서 종목별 실패는 흔하다)

    Raises:
        KrxUnavailable: RETRY_MAX회 모두 실패
    """
    last: Optional[Exception] = None
    for attempt in range(RETRY_MAX):
        try:
            return fn()
        except Exception as e:              # noqa: BLE001 – 무엇이든 재시도 대상
            last = e
            wait = RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)]
            logger.log(
                logging.DEBUG if quiet else logging.WARNING,
                "%s 실패 (%d/%d): %s — %d초 후 재시도",
                label, attempt + 1, RETRY_MAX, e, wait,
            )
            time.sleep(wait)
    raise KrxUnavailable(f"{label} {RETRY_MAX}회 실패: {last}")


def _to_num(s: pd.Series) -> pd.Series:
    """'1,234' 같은 문자열 숫자를 float로. 빈 값·'-'는 NaN."""
    cleaned = s.astype(str).str.replace(",", "", regex=False).str.strip()
    return pd.to_numeric(cleaned, errors="coerce")


# ── 거래일 달력 ─────────────────────────────────────────────

def trading_days(end: date, count: int) -> list[str]:
    """`end` 이하의 최근 `count` 거래일 (YYYYMMDD 문자열, 오름차순).

    기준 종목 하나의 시세 인덱스를 달력으로 쓴다.
    """
    start = end - timedelta(days=int(count * 1.7) + 20)
    df = _call(
        "거래일 달력 조회",
        lambda: krx.get_market_ohlcv(
            start.strftime("%Y%m%d"), end.strftime("%Y%m%d"), CALENDAR_TICKER
        ),
    )
    if df is None or df.empty:
        raise KrxUnavailable("거래일 달력이 비어 있습니다.")
    days = [d.strftime("%Y%m%d") for d in df.index]
    return days[-count:]


# ── 캐시 ────────────────────────────────────────────────────

def load_cache() -> pd.DataFrame:
    """누적 OHLCV 캐시. 없으면 빈 DataFrame."""
    if not CACHE_PATH.exists():
        return pd.DataFrame(columns=CACHE_COLUMNS)
    try:
        df = pd.read_parquet(CACHE_PATH)
    except Exception as e:                  # noqa: BLE001
        # 깨진 캐시 때문에 스캔 전체가 멈추지 않게 한다. 다시 받으면 된다.
        logger.warning("캐시를 읽지 못해 처음부터 받습니다: %s", e)
        return pd.DataFrame(columns=CACHE_COLUMNS)
    missing = [c for c in CACHE_COLUMNS if c not in df.columns]
    if missing:
        logger.warning("캐시 컬럼 부족%s – 처음부터 받습니다", missing)
        return pd.DataFrame(columns=CACHE_COLUMNS)
    df = df[CACHE_COLUMNS].copy()
    df["날짜"] = df["날짜"].astype(str)
    df["티커"] = df["티커"].astype(str)
    return df


def save_cache(df: pd.DataFrame) -> None:
    """캐시 저장. 유지 구간(SCAN_DAYS 거래일)을 넘는 과거는 버린다."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    keep = sorted(df["날짜"].unique())[-SCAN_DAYS:]
    trimmed = df[df["날짜"].isin(keep)].sort_values(["티커", "날짜"])
    trimmed.to_parquet(CACHE_PATH, index=False)
    logger.info(
        "캐시 저장: %s (%d행, %d거래일, %d종목)",
        CACHE_PATH.name, len(trimmed), len(keep), trimmed["티커"].nunique(),
    )


# ── 종목 목록 (data/universe.json) ──────────────────────────

UNIVERSE_TEMPLATE = {
    "built_on": None,
    "as_of": None,
    "source": "수동 입력",
    "note": (
        "폴백(종목별 개별 조회) 경로가 쓰는 종목 목록입니다. "
        "build_universe.py로 자동 갱신하거나, 아래 stocks 배열에 "
        "{\"ticker\": \"005930\", \"name\": \"삼성전자\", \"market\": \"KOSPI\"} "
        "형식으로 직접 채워도 됩니다. name은 비워도 되고, 그때는 "
        "통과 종목에 한해 조회로 채웁니다. 신규 상장은 120거래일 미만이라 "
        "어차피 필터에서 빠지므로 목록이 며칠 낡아도 됩니다."
    ),
    "stocks": [],
}


def load_universe() -> Optional[pd.DataFrame]:
    """data/universe.json → index=티커, 컬럼 종목명/시장. 없거나 비면 None."""
    if not UNIVERSE_PATH.exists():
        return None
    try:
        raw = json.loads(UNIVERSE_PATH.read_text(encoding="utf-8"))
        stocks = raw.get("stocks") or []
    except Exception as e:                  # noqa: BLE001
        logger.warning("universe.json을 읽지 못했습니다: %s", e)
        return None
    if not stocks:
        return None

    frame = pd.DataFrame(stocks)
    if "ticker" not in frame.columns:
        logger.warning("universe.json에 ticker 항목이 없습니다.")
        return None

    frame["ticker"] = frame["ticker"].astype(str).str.zfill(6)
    frame = frame.drop_duplicates(subset=["ticker"]).set_index("ticker")
    frame.index.name = "티커"
    out = pd.DataFrame(index=frame.index)
    out["종목명"] = frame["name"].fillna("") if "name" in frame else ""
    out["시장"] = frame["market"].fillna("") if "market" in frame else ""
    out["소속부"] = ""
    logger.info("universe.json 종목 %d개 (기준일 %s)",
                len(out), raw.get("as_of"))
    return out


def save_universe(frame: pd.DataFrame, as_of: str, source: str) -> Path:
    """종목 목록을 universe.json 형식으로 저장한다."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = dict(UNIVERSE_TEMPLATE)
    payload["built_on"] = date.today().isoformat()
    payload["as_of"] = as_of
    payload["source"] = source
    payload["stocks"] = [
        {
            "ticker": t,
            "name": str(row.get("종목명", "") or ""),
            "market": str(row.get("시장", "") or ""),
        }
        for t, row in frame.iterrows()
    ]
    UNIVERSE_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    return UNIVERSE_PATH


def write_universe_template() -> Path:
    """자동 조회가 전부 막혔을 때 직접 채울 빈 파일을 만든다."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = dict(UNIVERSE_TEMPLATE)
    payload["built_on"] = date.today().isoformat()
    UNIVERSE_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    return UNIVERSE_PATH


# ── 종목 메타 (이름·시장·소속부) ────────────────────────────

@dataclass
class Meta:
    """스캔 기준일의 전종목 메타."""

    frame: pd.DataFrame          # index=티커, 컬럼: 종목명/시장/소속부
    has_admin_flag: bool         # 관리종목을 실제로 판별할 수 있었는가
    source: str
    notes: list[str] = field(default_factory=list)


def fetch_meta_bulk(day: str) -> Meta:
    """KRX '전종목 시세'(MDCSTAT01501) 원본에서 메타를 1회로 받는다.

    이 응답은 종목명·시장·소속부를 함께 준다. pykrx의 공개 함수는 OHLCV만
    남기고 나머지를 버리므로 같은 요청의 원본을 직접 쓴다.

    Raises:
        KrxUnavailable / Exception: 실패 시 호출부가 폴백한다
    """
    from pykrx.website.krx.market.core import 전종목시세

    raw = _call(f"전종목 메타 {day}", lambda: 전종목시세().fetch(day, "ALL"))
    if raw is None or raw.empty:
        raise KrxUnavailable("전종목 메타가 비어 있습니다.")

    frame = pd.DataFrame({
        "종목명": raw["ISU_ABBRV"].astype(str).str.strip(),
        "시장": raw["MKT_NM"].astype(str).str.strip(),
        "소속부": raw.get("SECT_TP_NM", pd.Series("", index=raw.index))
                    .astype(str).str.strip(),
    })
    frame.index = raw["ISU_SRT_CD"].astype(str).str.zfill(6)
    frame.index.name = "티커"
    return Meta(frame=frame, has_admin_flag=True, source="전종목시세")


def load_meta(day: str) -> Meta:
    """종목 메타. 전종목 원본이 막히면 universe.json으로 대체한다.

    대체 경로에서는 관리종목 여부를 알 수 없다. 추측해서 채우지 않고
    필터를 생략한 뒤 그 사실을 notes에 남긴다.
    """
    try:
        return fetch_meta_bulk(day)
    except Exception as e:                  # noqa: BLE001
        logger.warning("전종목 메타 조회 실패, universe.json으로 대체: %s", e)

    frame = load_universe()
    if frame is None:
        raise KrxUnavailable(
            "종목 목록이 없습니다. KRX 전종목 조회가 막혔고 "
            f"{UNIVERSE_PATH}도 비어 있습니다. build_universe.py를 먼저 실행하세요."
        )

    notes = ["관리종목 정보를 받지 못해 관리종목 필터를 적용하지 못했습니다."]
    if (frame["종목명"] == "").any():
        notes.append("일부 종목명을 목록에서 받지 못해 통과 종목만 개별 조회합니다.")
    return Meta(
        frame=frame, has_admin_flag=False, source="universe.json", notes=notes,
    )


# ── 1차 필터 (메타 기준) ────────────────────────────────────

@dataclass
class Prefilter:
    """계산 전에 걸러낸 결과."""

    tickers: list[str]
    total: int
    dropped: dict[str, int] = field(default_factory=dict)


def prefilter_meta(meta: Meta) -> Prefilter:
    """조회 전에 이름·코드만으로 거를 수 있는 것을 먼저 거른다.

    폴백 경로는 종목당 1회를 도니, 여기서 줄인 만큼 그대로 시간이 준다.
    """
    f = meta.frame
    total = len(f)
    dropped: dict[str, int] = {}
    keep = pd.Series(True, index=f.index)

    if (f["시장"].astype(str) != "").any():
        m = f["시장"].isin(MARKETS)
        dropped["시장(코넥스 등)"] = int((keep & ~m).sum())
        keep &= m

    if meta.has_admin_flag:
        adm = f["소속부"].str.contains("관리", na=False)
        dropped["관리종목"] = int((keep & adm).sum())
        keep &= ~adm

    pref = pd.Series(
        [t[-1] in PREF_SHARE_SUFFIXES for t in f.index], index=f.index
    )
    dropped["우선주"] = int((keep & pref).sum())
    keep &= ~pref

    # 이름을 모르는 종목은 여기서 못 거른다. 통과 종목의 이름을 채운 뒤
    # 한 번 더 본다 (_drop_by_name).
    names = f["종목명"].astype(str)
    bad = names.apply(lambda n: bool(n) and _name_excluded(n))
    dropped["스팩·ETN"] = int((keep & bad).sum())
    keep &= ~bad

    return Prefilter(tickers=list(f.index[keep]), total=total, dropped=dropped)


def _name_excluded(name: str) -> bool:
    """스팩·ETN처럼 이름으로 걸러내는 대상인가.

    ETF는 조회 소스(주식 목록)에 애초에 들어오지 않는다.
    """
    upper = name.upper()
    return any(k.upper() in upper for k in EXCLUDE_NAME_KEYWORDS)


# ── 조회 (1순위: 전종목 일괄 / 2순위: 종목별) ───────────────

@dataclass
class Universe:
    """확보한 시세와 그 출처."""

    hist: pd.DataFrame
    source: str
    truncated: bool
    notes: list[str] = field(default_factory=list)


def fetch_day(day: str) -> pd.DataFrame:
    """하루치 전종목 OHLCV. 컬럼은 CACHE_COLUMNS 형태로 맞춘다."""
    df = _call(
        f"전종목 시세 {day}",
        lambda: krx.get_market_ohlcv(day, market="ALL"),
    )
    if df is None or df.empty:
        return pd.DataFrame(columns=CACHE_COLUMNS)

    out = df.reset_index()
    out = out.rename(columns={out.columns[0]: "티커"})
    for col in ("시가", "고가", "저가", "종가", "거래량"):
        if col not in out.columns:
            raise KrxUnavailable(f"전종목 시세 {day}: '{col}' 컬럼 없음")
    out["날짜"] = day
    out["티커"] = out["티커"].astype(str).str.zfill(6)
    return out[CACHE_COLUMNS]


def _bulk_history(
    days: list[str], cache: pd.DataFrame, deadline: float,
) -> tuple[list[pd.DataFrame], bool, bool, bool]:
    """1순위: 날짜별 전종목 일괄 조회.

    Returns:
        (받은 조각들, 시간초과 여부, KRX 실패 여부, 조회할 게 있었는지)
    """
    have = set(cache["날짜"]) if not cache.empty else set()
    todo = [d for d in days if d not in have]
    if not todo:
        logger.info("캐시가 최신입니다 (%d거래일). 추가 조회 없음", len(days))
        return [], False, False, False

    logger.info("[1순위] 전종목 일괄 – 받을 거래일 %d일", len(todo))
    chunks: list[pd.DataFrame] = []
    step = max(1, len(todo) // 10)

    for i, day in enumerate(todo, start=1):
        if time.monotonic() > deadline:
            logger.warning("시간 초과 – %d/%d일에서 멈춥니다", i - 1, len(todo))
            return chunks, True, False, True
        try:
            chunks.append(fetch_day(day))
        except KrxUnavailable as e:
            logger.warning("전종목 일괄 조회 불가 (%s) – 폴백으로 넘어갑니다", e)
            return chunks, False, True, True
        if i % step == 0 or i == len(todo):
            logger.info("[1순위] 진행률 %d%% (%d/%d)",
                        i * 100 // len(todo), i, len(todo))
        _sleep()

    return chunks, False, False, True


def _per_ticker_history(
    days: list[str],
    tickers: list[str],
    cache: pd.DataFrame,
    deadline: float,
) -> tuple[list[pd.DataFrame], bool, bool]:
    """2순위(폴백): 종목별로 기간 전체를 1회씩 받는다.

    get_market_ohlcv(시작, 종료, 티커)는 기간을 한 번에 주므로 종목당 1회다.
    캐시에 있는 종목은 마지막 저장일 다음 거래일부터만 받는다.

    Returns:
        (받은 조각들, 시간초과 여부, 조회할 게 있었는지)

    Raises:
        KrxUnavailable: 연속 실패가 MAX_CONSECUTIVE_FAILS를 넘을 때
    """
    scan_day = days[-1]
    last_by_ticker: dict[str, str] = {}
    if not cache.empty:
        last_by_ticker = cache.groupby("티커")["날짜"].max().to_dict()

    todo: list[tuple[str, str]] = []
    for t in tickers:
        last = last_by_ticker.get(t)
        if last is None or last not in days:
            todo.append((t, days[0]))       # 처음이거나 구간 밖 – 전체
        elif last < scan_day:
            todo.append((t, days[days.index(last) + 1]))

    if not todo:
        logger.info("캐시가 최신입니다 (%d종목). 추가 조회 없음", len(tickers))
        return [], False, False

    logger.info(
        "[2순위] 종목별 개별 – 받을 종목 %d개 (예상 %.0f분)",
        len(todo), len(todo) * FALLBACK_SLEEP / 60,
    )

    chunks: list[pd.DataFrame] = []
    step = max(1, len(todo) // 10)
    fails = consecutive = 0

    for i, (ticker, start) in enumerate(todo, start=1):
        if time.monotonic() > deadline:
            logger.warning("시간 초과 – %d/%d종목에서 멈춥니다", i - 1, len(todo))
            return chunks, True, True

        try:
            df = _call(
                f"[{ticker}] 시세",
                lambda t=ticker, s=start: krx.get_market_ohlcv(s, scan_day, t),
                quiet=True,
            )
            consecutive = 0
        except KrxUnavailable:
            # 개별 종목 실패는 건너뛰고 계속한다. 다만 연달아 실패하면
            # 종목 문제가 아니라 KRX 쪽 문제이므로 그날 스캔을 접는다.
            fails += 1
            consecutive += 1
            if consecutive >= MAX_CONSECUTIVE_FAILS:
                raise KrxUnavailable(
                    f"종목별 조회가 {consecutive}회 연속 실패했습니다."
                )
            continue

        if df is not None and not df.empty:
            out = df.reset_index()
            out = out.rename(columns={out.columns[0]: "날짜"})
            out["날짜"] = pd.to_datetime(out["날짜"]).dt.strftime("%Y%m%d")
            out["티커"] = ticker
            chunks.append(out[CACHE_COLUMNS])

        if i % step == 0 or i == len(todo):
            logger.info("[2순위] 진행률 %d%% (%d/%d) · 실패 %d",
                        i * 100 // len(todo), i, len(todo), fails)
        _sleep(FALLBACK_SLEEP)

    if fails:
        logger.info("개별 조회 실패로 건너뛴 종목 %d개", fails)
    return chunks, False, True


def fetch_universe(
    days: list[str], tickers: list[str], deadline: float,
) -> Universe:
    """전종목 일괄을 먼저 시도하고, 막히면 종목별 개별로 돌린다."""
    cache = load_cache()
    notes: list[str] = []

    chunks, truncated, bulk_failed, had_work = _bulk_history(
        days, cache, deadline
    )
    source = SOURCE_BULK if had_work else SOURCE_CACHE

    if bulk_failed:
        if chunks:                          # 일부라도 받았으면 캐시에 먼저 반영
            cache = _merge(cache, chunks)
            save_cache(cache)
            chunks = []
        more, truncated, fell_back = _per_ticker_history(
            days, tickers, cache, deadline
        )
        chunks.extend(more)
        source = SOURCE_PER_TICKER if fell_back else SOURCE_CACHE
        if fell_back:
            notes.append(
                "전종목 일괄 조회가 실패해 종목별 개별 조회로 받았습니다."
            )
        else:
            notes.append(
                "전종목 일괄 조회가 실패했지만 캐시가 최신이라 그대로 썼습니다."
            )

    if chunks:
        cache = _merge(cache, chunks)
        save_cache(cache)

    return Universe(hist=cache, source=source, truncated=truncated, notes=notes)


def _merge(cache: pd.DataFrame, chunks: list[pd.DataFrame]) -> pd.DataFrame:
    merged = pd.concat([cache] + chunks, ignore_index=True)
    merged["날짜"] = merged["날짜"].astype(str)
    merged["티커"] = merged["티커"].astype(str)
    return merged.drop_duplicates(subset=["날짜", "티커"], keep="last")


# ── 2차 필터 (유동성·거래정지) ──────────────────────────────

def prefilter_liquidity(
    hist: pd.DataFrame, tickers: list[str], days: list[str],
    min_value: float = MIN_TRADING_VALUE,
    min_days: int = MIN_LISTED_DAYS,
) -> Prefilter:
    """거래대금·거래정지·상장일수로 거른다.

    거래대금은 최근 TRADING_VALUE_DAYS 거래일 평균(종가 × 거래량)이다.
    유동성 자체를 보는 값이라 당일을 빼지 않는다 — 당일을 빼는 것은
    '오늘 급증했는가'를 보는 거래량 배수 쪽 이야기다.

    Args:
        min_value: 20일 평균 거래대금 하한. 기본은 스크리너 기준(10억).
        min_days: 상장 거래일 하한. 기본은 ATR 워밍업까지 보는 120일.
            scan_all.py는 원본 수치만 뽑으므로 더 낮은 값을 넘긴다.
    """
    scan_day = days[-1]
    sub = hist[hist["티커"].isin(tickers)]
    dropped: dict[str, int] = {}

    # 상장 거래일 – 종가가 있는 날만 센다
    traded = sub[sub["종가"] > 0]
    counts = traded.groupby("티커")["날짜"].nunique()
    enough = set(counts[counts >= min_days].index)
    dropped[f"거래일 {min_days}일 미만"] = len(tickers) - len(enough)

    # 기준일에 거래가 없었으면 거래정지로 본다
    today = traded[(traded["날짜"] == scan_day) & (traded["거래량"] > 0)]
    active = set(today["티커"]) & enough
    dropped["거래정지·거래없음"] = len(enough) - len(active)

    # 20일 평균 거래대금
    window = traded[
        traded["티커"].isin(active) & traded["날짜"].isin(days[-TRADING_VALUE_DAYS:])
    ].copy()
    window["거래대금"] = window["종가"] * window["거래량"]
    avg_value = window.groupby("티커")["거래대금"].mean()
    liquid = set(avg_value[avg_value >= min_value].index)
    dropped[f"평균 거래대금 {int(min_value) // 100_000_000}억 미만"] = (
        len(active) - len(liquid)
    )

    return Prefilter(
        tickers=[t for t in tickers if t in liquid],
        total=len(tickers),
        dropped={k: v for k, v in dropped.items() if v},
    )


# ── 종목 계산 ───────────────────────────────────────────────

@dataclass
class Candidate:
    """통과 종목 한 건. JSON에 그대로 직렬화된다."""

    ticker: str
    name: str
    market: str
    sector: str
    price: float
    volume: int
    vol_avg_20: float
    vol_mult: float
    value_avg_20: float           # 20일 평균 거래대금
    atr: int
    atr_pct: float                # 현재가 대비 ATR %
    gap_atr: float                # 돌파 갭 = (현재가 - 직전20일고가) / ATR
    unit_shares: int
    stop_loss: int
    risk_amount: float            # 1회 리스크액 (계좌 1%)
    high_20_prev: float           # 직전 20일 고가 (돌파 기준선)
    low_10: float                 # 10일 저가 (당일 포함)
    exit_level: float             # 청산선 = 직전 10일 저가
    te: Optional[float]
    rr: Optional[float]
    win_rate: Optional[float]


def evaluate(
    ticker: str,
    hist: pd.DataFrame,
    name: str,
    market: str,
    sector: str,
    capital: float,
) -> Optional[Candidate]:
    """한 종목을 계산하고 돌파·거래량 조건을 본다. 탈락이면 None.

    계산은 전부 core.py 함수로 한다 (ATR·유닛·손절선·기대값·추세신호).

    Raises:
        ValueError: 데이터가 모자라 core 계산이 불가능할 때
    """
    if len(hist) < MIN_LISTED_DAYS:
        return None

    # 거래량 배수 – core.calc_vol_mult()가 당일을 뺀 평균과 비교한다.
    # scan_all.py도 같은 함수를 쓰므로 정의가 갈릴 일이 없다.
    vol_mult_val = core.calc_vol_mult(hist)
    if vol_mult_val is None or vol_mult_val <= core.VOL_MULT_MIN:
        return None

    sig = core.trend_signals(hist)
    if not sig.breakout:
        return None

    atr = core.round_atr(core.calc_atr(hist))
    price = core.latest_close(hist)
    pos = core.calc_position(atr, capital)
    stop = int(core.calc_stop(price, atr))

    # vol_mult_val = today_vol / vol_avg 이므로 today_vol만 다시 읽으면
    # vol_avg를 나눗셈으로 되돌릴 수 있다 (평균 구간을 또 슬라이싱하지 않는다).
    today_vol = float(hist["거래량"].iloc[-1])
    vol_avg = today_vol / vol_mult_val

    tail = hist.tail(TRADING_VALUE_DAYS)
    value_avg = float((tail["종가"] * tail["거래량"]).mean())

    try:
        edge = core.calc_edge(hist)
        te, rr, win_rate = edge.te, edge.rr, edge.win_rate
    except ValueError:
        # 기대값을 못 내도 돌파 사실은 유효하다. 값은 비워 둔다.
        te = rr = win_rate = None

    return Candidate(
        ticker=ticker,
        name=name,
        market=market,
        sector=sector,
        price=price,
        volume=int(today_vol),
        vol_avg_20=round(vol_avg, 1),
        vol_mult=round(vol_mult_val, 2),
        value_avg_20=round(value_avg, 0),
        atr=atr,
        atr_pct=round(core.calc_atr_pct(atr, price), 2),
        gap_atr=round((price - sig.high_20_prev) / atr, 3) if atr > 0 else 0.0,
        unit_shares=pos.unit_shares,
        stop_loss=stop,
        risk_amount=pos.risk_amount,
        high_20_prev=sig.high_20_prev,
        low_10=sig.low_10,
        exit_level=sig.low_10_prev,
        te=round(te, 3) if te is not None else None,
        rr=round(rr, 2) if rr is not None else None,
        win_rate=round(win_rate, 1) if win_rate is not None else None,
    )


def resolve_names(candidates: list[Candidate]) -> tuple[list[Candidate], int]:
    """이름이 비어 있는 통과 종목만 개별 조회로 채우고 스팩·ETN을 뺀다.

    universe.json에 이름이 없을 때를 위한 것이다. 통과 종목은 수십 개
    수준이라 개별 조회를 해도 부담이 없다.

    Returns:
        (이름을 채운 목록, 이름 확인 후 제외한 수)
    """
    dropped = 0
    out: list[Candidate] = []
    for c in candidates:
        if not c.name:
            try:
                _sleep()
                c.name = krx.get_market_ticker_name(c.ticker)
            except Exception as e:          # noqa: BLE001
                logger.debug("[%s] 종목명 조회 실패: %s", c.ticker, e)
                c.name = c.ticker           # 추측하지 않고 코드를 그대로 쓴다
        if _name_excluded(c.name):
            dropped += 1
            continue
        out.append(c)
    return out, dropped


# ── 업종 ────────────────────────────────────────────────────

def _is_sector_index(name: str) -> bool:
    """업종 지수인가 (규모·스타일·테마 지수는 제외).

    코스피200·대형주·배당지수 같은 지수는 업종이 아니면서 구성종목이 적어,
    '가장 작은 지수를 업종으로 본다'는 규칙을 어지럽힌다. 이름으로 걸러낸다.
    """
    n = name.strip()
    for prefix in ("코스피 ", "코스닥 ", "KRX ", "KOSPI ", "KOSDAQ "):
        if n.startswith(prefix):
            n = n[len(prefix):].strip()

    if n in ("코스피", "코스닥", "코넥스", ""):
        return False
    if any(ch.isdigit() for ch in n):       # 코스피200, 코스닥150 …
        return False
    return not any(k in n for k in (
        "대형주", "중형주", "소형주", "우량기업", "벤처기업", "중견기업",
        "기술성장", "배당", "ESG", "지배구조", "가치", "성장", "프리미어",
        "글로벌", "신성장", "모멘텀", "퀄리티", "저변동", "TOP", "섹터", "그룹",
    ))


def build_sector_map(day: str) -> dict[str, str]:
    """티커 → 업종명. KRX 업종 지수의 구성종목으로 만든다.

    한 종목이 여러 지수에 들어가면(예: 제조업 ⊃ 화학) 구성종목이 가장 적은
    지수를 업종으로 본다. 상위 개념보다 구체적인 쪽이 업종에 가깝다.
    """
    members: list[tuple[str, list[str]]] = []

    for market in MARKETS:
        try:
            codes = _call(
                f"{market} 지수 목록",
                lambda m=market: krx.get_index_ticker_list(day, market=m),
                quiet=True,
            )
        except Exception as e:              # noqa: BLE001
            logger.warning("%s 지수 목록 조회 실패: %s", market, e)
            continue

        for code in codes:
            _sleep()
            try:
                name = krx.get_index_ticker_name(code)
                if not _is_sector_index(name):
                    continue
                tickers = krx.get_index_portfolio_deposit_file(code, day)
            except Exception as e:          # noqa: BLE001
                logger.warning("지수 %s 구성종목 조회 실패: %s", code, e)
                continue
            if tickers:
                members.append((name.strip(), [str(t).zfill(6) for t in tickers]))

    members.sort(key=lambda kv: len(kv[1]))     # 작은 지수부터 = 구체적인 업종부터

    mapping: dict[str, str] = {}
    for name, tickers in members:
        for t in tickers:
            mapping.setdefault(t, name)
    return mapping


def load_sector_map(day: str, refresh: bool = False) -> dict[str, str]:
    """업종 맵. SECTOR_MAX_AGE_DAYS 안에 만든 파일이 있으면 재사용한다."""
    if not refresh and SECTOR_PATH.exists():
        try:
            saved = json.loads(SECTOR_PATH.read_text(encoding="utf-8"))
            built = date.fromisoformat(saved["built_on"])
            if (date.today() - built).days <= SECTOR_MAX_AGE_DAYS:
                return saved["map"]
            logger.info("업종 맵이 오래되어 다시 만듭니다 (%s)", built)
        except Exception as e:              # noqa: BLE001
            logger.warning("업종 맵을 읽지 못해 다시 만듭니다: %s", e)

    logger.info("업종 맵 생성 중…")
    mapping = build_sector_map(day)
    if mapping:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        SECTOR_PATH.write_text(
            json.dumps(
                {"built_on": date.today().isoformat(), "as_of": day, "map": mapping},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        logger.info("업종 맵 %d종목 저장", len(mapping))
    else:
        logger.warning("업종 정보를 만들지 못했습니다 – '미분류'로 표시됩니다")
        # 오래된 맵이라도 있으면 그대로 쓴다 (없는 것보다 낫다)
        if SECTOR_PATH.exists():
            try:
                return json.loads(SECTOR_PATH.read_text(encoding="utf-8"))["map"]
            except Exception:               # noqa: BLE001
                pass
    return mapping


# ── 스캔 ────────────────────────────────────────────────────

def scan(
    end: Optional[date] = None,
    capital: float = core.DEFAULT_CAPITAL,
    refresh_sectors: bool = False,
) -> dict:
    """전종목 스캔. 결과 dict를 돌려주고 저장은 하지 않는다."""
    started = time.monotonic()
    deadline = started + TIME_BUDGET_SEC
    end = end or date.today()
    notes: list[str] = []

    auth_warning = check_krx_auth()
    if auth_warning:
        logger.warning("%s", auth_warning)
        notes.append(auth_warning)

    days = trading_days(end, SCAN_DAYS)
    scan_day = days[-1]
    logger.info("스캔 기준일 %s (요청 %s) · 목표 %d거래일",
                scan_day, end, len(days))

    meta = load_meta(scan_day)
    notes.extend(meta.notes)

    pf_meta = prefilter_meta(meta)
    logger.info("1차 필터(메타): 전체 %d종목 → %d종목 (%s)",
                pf_meta.total, len(pf_meta.tickers),
                ", ".join(f"{k} {v}" for k, v in pf_meta.dropped.items() if v))

    uni = fetch_universe(days, pf_meta.tickers, deadline)
    notes.extend(uni.notes)
    truncated = uni.truncated
    if truncated:
        notes.append(
            f"실행 시간 {TIME_BUDGET_SEC // 60}분을 넘어 조회를 중단했습니다."
        )

    hist_all = uni.hist[uni.hist["날짜"].isin(days)]
    available = sorted(hist_all["날짜"].unique()) if not hist_all.empty else []
    if not available:
        raise KrxUnavailable("시세를 한 건도 확보하지 못했습니다.")

    if available[-1] != scan_day:
        logger.warning("기준일을 %s → %s로 조정합니다", scan_day, available[-1])
        notes.append(f"기준일이 {available[-1]}로 조정되었습니다 (조회 미완료).")
        scan_day = available[-1]
        days = [d for d in days if d <= scan_day]

    if len(available) < MIN_LISTED_DAYS:
        raise KrxUnavailable(
            f"거래일 {len(available)}일 < 필요 {MIN_LISTED_DAYS}일 – "
            "ATR 워밍업이 안 되어 스캔을 중단합니다."
        )

    pf = prefilter_liquidity(hist_all, pf_meta.tickers, days)
    dropped = dict(pf_meta.dropped)
    dropped.update(pf.dropped)
    logger.info("2차 필터(유동성): %d종목 → %d종목 (%s)",
                pf.total, len(pf.tickers),
                ", ".join(f"{k} {v}" for k, v in pf.dropped.items()))

    sectors = load_sector_map(scan_day, refresh=refresh_sectors)
    if not sectors:
        notes.append("업종 정보를 받지 못해 '미분류'로 표시됩니다.")

    # 티커별로 나눠 계산. 정렬 후 groupby가 반복 필터링보다 훨씬 빠르다.
    target = hist_all[hist_all["티커"].isin(pf.tickers)].sort_values(
        ["티커", "날짜"]
    )
    groups = dict(tuple(target.groupby("티커")))

    candidates: list[Candidate] = []
    skipped = 0
    step = max(1, len(pf.tickers) // 10)

    for i, ticker in enumerate(pf.tickers, start=1):
        if time.monotonic() > deadline:
            logger.warning("시간 초과 – %d/%d종목에서 계산을 멈춥니다",
                           i - 1, len(pf.tickers))
            truncated = True
            notes.append(f"계산을 {i - 1}/{len(pf.tickers)}종목에서 중단했습니다.")
            break

        g = groups.get(ticker)
        if g is None:
            continue

        try:
            hist = g.set_index("날짜")[["시가", "고가", "저가", "종가", "거래량"]]
            hist = hist[hist["종가"] > 0]      # 거래정지일은 계산에서 뺀다
            row = meta.frame.loc[ticker]
            found = evaluate(
                ticker, hist,
                name=str(row.get("종목명", "") or ""),
                market=str(row.get("시장", "") or ""),
                sector=sectors.get(ticker, "미분류"),
                capital=capital,
            )
        except Exception as e:                # noqa: BLE001
            # 개별 종목 실패는 건너뛰고 계속한다
            logger.debug("[%s] 계산 건너뜀: %s", ticker, e)
            skipped += 1
            continue

        if found is not None:
            candidates.append(found)

        if i % step == 0 or i == len(pf.tickers):
            logger.info("계산 진행률 %d%% (%d/%d) · 통과 %d",
                        i * 100 // len(pf.tickers), i, len(pf.tickers),
                        len(candidates))

    if skipped:
        logger.info("계산 실패로 건너뛴 종목 %d개", skipped)

    candidates, late_dropped = resolve_names(candidates)
    if late_dropped:
        dropped["스팩·ETN"] = dropped.get("스팩·ETN", 0) + late_dropped

    # 거래량 배수 내림차순, 상위 MAX_RESULTS
    candidates.sort(key=lambda c: c.vol_mult, reverse=True)
    passed_total = len(candidates)
    shown = candidates[:MAX_RESULTS]
    if passed_total > MAX_RESULTS:
        notes.append(
            f"조건 통과 {passed_total}종목 중 거래량 배수 상위 "
            f"{MAX_RESULTS}종목만 표시합니다."
        )

    elapsed = time.monotonic() - started
    logger.info("완료: %d종목 통과 (표시 %d) · %.1f분",
                passed_total, len(shown), elapsed / 60)

    return {
        "scan_date": f"{scan_day[:4]}-{scan_day[4:6]}-{scan_day[6:]}",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "capital": capital,
        "source": uni.source,
        "meta_source": meta.source,
        "total_scanned": pf_meta.total,
        "universe_after_prefilter": len(pf.tickers),
        "passed": passed_total,
        "shown": len(shown),
        "trading_days_used": len(available),
        "truncated": truncated,
        "elapsed_sec": round(elapsed, 1),
        "filters": {
            "min_trading_value": MIN_TRADING_VALUE,
            "trading_value_days": TRADING_VALUE_DAYS,
            "vol_mult_min": core.VOL_MULT_MIN,
            "vol_avg_days": core.VOL_AVG_DAYS,
            "min_listed_days": MIN_LISTED_DAYS,
            "high_period": core.HIGH_PERIOD,
            "admin_filter_applied": meta.has_admin_flag,
        },
        "dropped": {k: v for k, v in dropped.items() if v},
        "notes": notes,
        "stocks": [asdict(c) for c in shown],
    }


def result_path(scan_date: str) -> Path:
    """data/breakout_YYYYMMDD.json 경로."""
    return DATA_DIR / f"breakout_{scan_date.replace('-', '')}.json"


def save_result(result: dict) -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = result_path(result["scan_date"])
    path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return path


# ── 결과 읽기 (app.py에서 재사용) ───────────────────────────

def latest_result() -> Optional[dict]:
    """data/ 에서 가장 최근 breakout_*.json. 없으면 None.

    파일이 깨져 있으면 그다음 최근 파일로 내려간다.
    """
    if not DATA_DIR.exists():
        return None
    for path in sorted(DATA_DIR.glob("breakout_*.json"), reverse=True):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:              # noqa: BLE001
            logger.warning("%s 읽기 실패: %s", path.name, e)
            continue
        data["_file"] = path.name
        data["_mtime"] = datetime.fromtimestamp(
            path.stat().st_mtime
        ).isoformat(timespec="seconds")
        return data
    return None


# ── CLI ─────────────────────────────────────────────────────

def _print_summary(result: dict) -> None:
    print()
    print(f"스캔 기준일: {result['scan_date']}  "
          f"({result['trading_days_used']}거래일 · {result['source']})")
    print(f"총 {result['total_scanned']:,}종목 중 "
          f"{result['passed']}종목 통과  "
          f"(필터 통과 {result['universe_after_prefilter']:,}종목)")
    print(f"소요 {result['elapsed_sec'] / 60:.1f}분"
          + ("  ※ 중단됨" if result["truncated"] else ""))

    if result["dropped"]:
        print("필터 제외: "
              + ", ".join(f"{k} {v:,}" for k, v in result["dropped"].items() if v))
    for note in result["notes"]:
        print(f"  · {note}")

    stocks = result["stocks"]
    if not stocks:
        print("\n오늘 조건을 통과한 종목이 없습니다.")
        return

    print(f"\n{'종목명':<12}{'업종':<12}{'현재가':>10}{'거래량배수':>10}"
          f"{'ATR%':>7}{'1유닛':>8}{'손절선':>10}")
    for s in stocks:
        print(f"{s['name'][:11]:<12}{s['sector'][:11]:<12}"
              f"{s['price']:>10,.0f}{s['vol_mult']:>9.1f}배"
              f"{s['atr_pct']:>6.1f}%{s['unit_shares']:>7,}주"
              f"{s['stop_loss']:>10,}")
    print()


def setup_logging() -> None:
    """pykrx가 호출마다 root 로거로 잘못된 형식의 logging.info를 부른다
    (website/comm/util.py). root를 WARNING으로 두고 이 모듈만 INFO로 올린다."""
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s [%(levelname)s] %(message)s",
        stream=sys.stdout,
    )
    logger.setLevel(logging.INFO)


def main() -> int:
    import argparse

    setup_logging()

    parser = argparse.ArgumentParser(description="KOSPI·KOSDAQ 돌파 스크리너")
    parser.add_argument("--end", default=None,
                        help="기준일 YYYY-MM-DD (기본 오늘)")
    parser.add_argument("--capital", type=float, default=None,
                        help="총자본 (기본 TOTAL_CAPITAL 환경변수 또는 1천만원)")
    parser.add_argument("--refresh-sectors", action="store_true",
                        help="업종 맵을 다시 만든다")
    args = parser.parse_args()

    capital = args.capital
    if capital is None:
        capital = float(os.environ.get("TOTAL_CAPITAL") or core.DEFAULT_CAPITAL)

    end = date.fromisoformat(args.end) if args.end else None

    try:
        result = scan(end=end, capital=capital,
                      refresh_sectors=args.refresh_sectors)
    except KrxUnavailable as e:
        # 직전 JSON을 그대로 둔다 – 덮어쓰지 않는다
        logger.error("스캔 중단: %s", e)
        previous = latest_result()
        if previous:
            logger.info("직전 결과 %s를 그대로 유지합니다 (기준일 %s)",
                        previous["_file"], previous["scan_date"])
        return 1

    path = save_result(result)
    logger.info("저장: %s", path)
    _print_summary(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
