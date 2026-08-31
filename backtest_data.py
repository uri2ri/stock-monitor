"""
backtest_data.py – 백테스트용 장기 OHLCV 이력 축적 (1단계: 데이터 준비)

라이브 파이프라인(screener.py/scan_all.py)이 쓰는 data/ohlcv_cache.parquet은
매 저장마다 최근 SCAN_DAYS(120)거래일만 남기고 그 이전을 버린다(screener.py의
save_cache() 참고) - 실시간 스캔용 롤링 윈도우로 설계된 것이라 몇 년을 그대로
돌려도 과거 데이터가 쌓이지 않는다.

이 스크립트는 완전히 별도 파일(data/backtest_ohlcv.parquet)에 트리밍 없이
누적한다. 라이브 캐시·screener.py·core.py·kis_client.py는 이 스크립트가
전혀 읽거나 쓰지 않는다 - 매일 도는 daily/evening/nightly-scan 워크플로는
이 파일의 존재 자체를 모르므로, 파일 용량이나 조회량이 늘어도 라이브
파이프라인 딜레이에 영향이 없다.

[조회 경로]
종목별 개별 조회(get_market_ohlcv(시작, 종료, 티커))만 쓴다 - 기간을 한 번에
주므로 종목당 1회 호출이면 되고, KRX_ID/KRX_PW 로그인이 없어도 동작한다
(전종목 일괄 조회와 달리 개별종목 시세는 로그인 없이 열려 있다 - core.py의
fetch_ohlcv, screener.py의 2순위 폴백 경로와 동일한 근거).

[종목 목록 – 알려진 한계]
data/universe.json(현재 상장 종목 스냅샷)을 그대로 쓴다. 이미 상장폐지된
종목은 이 목록에 없어 생존편향이 남는다. 이 스크립트는 그 문제를 풀지
않는다 - 과거 시점의 실제 상장 종목 목록 확보는 별도 단계(2단계, KRX 로그인
필요)에서 다룬다. 지금은 "현재 살아있는 종목의 과거 시세를 최대한 길게
축적"하는 것만 목표다.

[재실행 안전]
캐시에 이미 있는 구간은 건너뛰고 부족한 구간(과거 쪽 backfill / 최신 쪽
top-up)만 채운다. 시간 예산을 넘기면 중단하고, 다음 실행이 이어받는다.
2,900종목을 한 번에 다 못 받아도 여러 번 나눠 돌리면 된다.

CLI:
    python backtest_data.py                          # 기본 3년, 전체 종목
    python backtest_data.py --years 2
    python backtest_data.py --tickers 005930,000660   # 특정 종목만
    python backtest_data.py --limit 50                # 앞 50종목만(스모크 테스트)
    python backtest_data.py --time-budget-min 20      # 시간 예산 조정
"""

from __future__ import annotations

import argparse
import logging
import random
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

import pandas as pd
from pykrx import stock as krx

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# screener.py를 고치지 않고 읽기만 한다 - 종목 목록 파싱(load_universe)과
# 캐시 컬럼 정의(CACHE_COLUMNS)를 그대로 재사용해 라이브 파이프라인과
# 형식이 갈라지지 않게 한다. 재시도 루프(_call)는 밑줄 접두 = 모듈
# 내부용이라 가져다 쓰지 않고, 같은 정책(간격·재시도 횟수·백오프)만
# 아래에 다시 작게 구현한다.
import screener

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent / "data"
BACKTEST_CACHE_PATH = DATA_DIR / "backtest_ohlcv.parquet"
CACHE_COLUMNS = screener.CACHE_COLUMNS  # ["날짜", "티커", "시가", "고가", "저가", "종가", "거래량"]

# screener.py의 2순위(종목별 개별) 경로와 같은 정책 - KRX 차단 방지.
SLEEP_MIN, SLEEP_MAX = screener.SLEEP_MIN, screener.SLEEP_MAX
RETRY_MAX = screener.RETRY_MAX
RETRY_BACKOFF = screener.RETRY_BACKOFF
MAX_CONSECUTIVE_FAILS = screener.MAX_CONSECUTIVE_FAILS

SAVE_EVERY = 50           # 이만큼 종목을 처리할 때마다 중간 저장 (중단 대비)
DEFAULT_YEARS = 3
DEFAULT_TIME_BUDGET_MIN = 50


class KrxUnavailable(RuntimeError):
    """재시도까지 실패."""


def _sleep(seconds: Optional[float] = None) -> None:
    time.sleep(seconds if seconds is not None else random.uniform(SLEEP_MIN, SLEEP_MAX))


def _call(label: str, fn: Callable):
    """KRX 호출 + 재시도(2초→4초→8초). screener.py의 _call()과 같은 정책."""
    last: Optional[Exception] = None
    for attempt in range(RETRY_MAX):
        try:
            return fn()
        except Exception as e:                  # noqa: BLE001
            last = e
            wait = RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)]
            logger.debug("%s 실패 (%d/%d): %s — %d초 후 재시도",
                         label, attempt + 1, RETRY_MAX, e, wait)
            time.sleep(wait)
    raise KrxUnavailable(f"{label} {RETRY_MAX}회 실패: {last}")


# ── 캐시 (트리밍 없음 – screener.save_cache()와 다른 점) ────────

def load_backtest_cache() -> pd.DataFrame:
    if not BACKTEST_CACHE_PATH.exists():
        return pd.DataFrame(columns=CACHE_COLUMNS)
    try:
        df = pd.read_parquet(BACKTEST_CACHE_PATH)
    except Exception as e:                      # noqa: BLE001
        logger.warning("백테스트 캐시를 읽지 못해 처음부터 받습니다: %s", e)
        return pd.DataFrame(columns=CACHE_COLUMNS)
    df = df[CACHE_COLUMNS].copy()
    df["날짜"] = df["날짜"].astype(str)
    df["티커"] = df["티커"].astype(str)
    return df


def save_backtest_cache(df: pd.DataFrame) -> None:
    """트리밍 없이 그대로 저장한다 - screener.save_cache()와 달리 과거를 안 버린다."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    out = df.drop_duplicates(subset=["날짜", "티커"], keep="last") \
            .sort_values(["티커", "날짜"])
    out.to_parquet(BACKTEST_CACHE_PATH, index=False)
    logger.info(
        "백테스트 캐시 저장: %s (%d행, %d종목, %s~%s)",
        BACKTEST_CACHE_PATH.name, len(out), out["티커"].nunique(),
        out["날짜"].min() if len(out) else "-",
        out["날짜"].max() if len(out) else "-",
    )


# ── 종목 목록 ────────────────────────────────────────────────

def load_tickers() -> list[str]:
    """data/universe.json에서 현재 상장 종목 목록을 읽는다.

    알려진 한계: 상장폐지 종목은 여기 없다 (생존편향, 2단계에서 다룰 문제).
    """
    frame = screener.load_universe()
    if frame is None or frame.empty:
        raise SystemExit(
            f"{screener.UNIVERSE_PATH}가 없거나 비어 있습니다. "
            "먼저 build_universe.py(있다면)로 만들거나 --tickers로 직접 지정하세요."
        )
    return list(frame.index)


# ── 종목별 조회 ──────────────────────────────────────────────

@dataclass
class FetchStats:
    processed: int = 0
    fetched: int = 0
    skipped_up_to_date: int = 0
    failed: int = 0
    failed_tickers: list[str] = None

    def __post_init__(self):
        if self.failed_tickers is None:
            self.failed_tickers = []


def _missing_ranges(
    ticker: str, cache: pd.DataFrame, start: date, end: date,
) -> list[tuple[date, date]]:
    """이 종목에 대해 아직 못 받은 구간. 과거쪽 backfill과 최신쪽 top-up을
    각각 따로 돌려준다 (둘 다 필요할 수도, 하나만 필요할 수도, 없을 수도)."""
    sub = cache[cache["티커"] == ticker]
    if sub.empty:
        return [(start, end)]

    have_min = datetime.strptime(sub["날짜"].min(), "%Y%m%d").date()
    have_max = datetime.strptime(sub["날짜"].max(), "%Y%m%d").date()

    ranges: list[tuple[date, date]] = []
    if start < have_min:
        ranges.append((start, have_min - timedelta(days=1)))
    if end > have_max:
        ranges.append((have_max + timedelta(days=1), end))
    return ranges


def fetch_ticker_range(ticker: str, start: date, end: date) -> pd.DataFrame:
    """종목 하나의 [start, end] 시세. screener.py 2순위 경로와 같은 정규화."""
    df = _call(
        f"[{ticker}] {start}~{end}",
        lambda: krx.get_market_ohlcv(
            start.strftime("%Y%m%d"), end.strftime("%Y%m%d"), ticker,
        ),
    )
    if df is None or df.empty:
        return pd.DataFrame(columns=CACHE_COLUMNS)

    out = df.reset_index()
    out = out.rename(columns={out.columns[0]: "날짜"})
    out["날짜"] = pd.to_datetime(out["날짜"]).dt.strftime("%Y%m%d")
    out["티커"] = ticker
    return out[CACHE_COLUMNS]


def run(
    tickers: list[str],
    years: int,
    time_budget_min: int,
    end: Optional[date] = None,
) -> FetchStats:
    end = end or date.today()
    start = end - timedelta(days=int(years * 365.25))
    deadline = time.monotonic() + time_budget_min * 60

    cache = load_backtest_cache()
    stats = FetchStats()
    new_chunks: list[pd.DataFrame] = []
    consecutive_fails = 0

    logger.info(
        "백테스트 데이터 축적 시작: 종목 %d개, 기간 %s~%s (예산 %d분)",
        len(tickers), start, end, time_budget_min,
    )

    for i, ticker in enumerate(tickers, start=1):
        if time.monotonic() > deadline:
            logger.warning(
                "시간 예산 초과 – %d/%d종목에서 멈춥니다. 다시 실행하면 이어받습니다.",
                i - 1, len(tickers),
            )
            break

        stats.processed += 1
        ranges = _missing_ranges(ticker, cache, start, end)
        if not ranges:
            stats.skipped_up_to_date += 1
            continue

        got_any = False
        for r_start, r_end in ranges:
            if r_start > r_end:
                continue
            try:
                chunk = fetch_ticker_range(ticker, r_start, r_end)
                consecutive_fails = 0
            except KrxUnavailable as e:
                stats.failed += 1
                stats.failed_tickers.append(ticker)
                consecutive_fails += 1
                logger.warning("[%s] 조회 실패: %s", ticker, e)
                if consecutive_fails >= MAX_CONSECUTIVE_FAILS:
                    logger.error(
                        "%d회 연속 실패 – KRX 쪽 문제로 보고 중단합니다.",
                        consecutive_fails,
                    )
                    if new_chunks:
                        cache = pd.concat([cache] + new_chunks, ignore_index=True)
                        save_backtest_cache(cache)
                    return stats
                continue
            if not chunk.empty:
                new_chunks.append(chunk)
                got_any = True
            _sleep()

        if got_any:
            stats.fetched += 1

        if i % SAVE_EVERY == 0:
            cache = pd.concat([cache] + new_chunks, ignore_index=True)
            new_chunks = []
            save_backtest_cache(cache)
            logger.info(
                "진행률 %d%% (%d/%d) · 신규 수신 %d · 실패 %d",
                i * 100 // len(tickers), i, len(tickers),
                stats.fetched, stats.failed,
            )

    if new_chunks:
        cache = pd.concat([cache] + new_chunks, ignore_index=True)
    save_backtest_cache(cache)

    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--years", type=int, default=DEFAULT_YEARS,
                         help=f"확보할 연수 (기본 {DEFAULT_YEARS})")
    parser.add_argument("--tickers", type=str, default=None,
                         help="쉼표로 구분한 티커 목록 (지정 시 universe.json 대신 이것만 사용)")
    parser.add_argument("--limit", type=int, default=None,
                         help="앞에서부터 N종목만 처리 (스모크 테스트용)")
    parser.add_argument("--time-budget-min", type=int, default=DEFAULT_TIME_BUDGET_MIN,
                         help=f"실행 시간 예산 분 단위 (기본 {DEFAULT_TIME_BUDGET_MIN})")
    parser.add_argument("--end", type=str, default=None,
                         help="기준일 YYYY-MM-DD (기본 오늘)")
    args = parser.parse_args()

    if args.tickers:
        tickers = [t.strip().zfill(6) for t in args.tickers.split(",") if t.strip()]
    else:
        tickers = load_tickers()

    if args.limit:
        tickers = tickers[: args.limit]

    end = date.fromisoformat(args.end) if args.end else None

    stats = run(tickers, years=args.years, time_budget_min=args.time_budget_min, end=end)

    logger.info(
        "=== 완료: 처리 %d · 신규 수신 %d · 이미 최신 %d · 실패 %d ===",
        stats.processed, stats.fetched, stats.skipped_up_to_date, stats.failed,
    )
    if stats.failed_tickers:
        logger.info("실패 종목(%d개): %s", len(stats.failed_tickers),
                    ", ".join(stats.failed_tickers[:30]) +
                    (" ..." if len(stats.failed_tickers) > 30 else ""))

    return 0


if __name__ == "__main__":
    sys.exit(main())
