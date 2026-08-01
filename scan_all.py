"""
scan_all.py – 코스피·코스닥 전종목 터틀 진입 수치 스캔 (야간)

매일 밤 21시(KST)에 돌아 전종목의 20일 고가 대비 위치를 ATR 배수로
환산해 CSV로 남긴다. 스트림릿과 장중 감시 스크립트가 이 CSV를 읽는다.

★ 이 스크립트는 임계값으로 종목을 걸러내지 않는다.
  "0.5×ATR 이내" 같은 판정은 app.py와 intraday_watch.py에서 하고,
  여기서는 gap_atr / dist_atr 원본 수치를 전종목분 그대로 저장한다.
  나중에 기준을 바꿔도 과거 스냅샷을 다시 해석할 수 있어야 하기 때문이다.

시세 캐시(data/ohlcv_cache.parquet)는 screener.py(16:30)가 갱신한다.
여기서는 읽기만 하고, 기준일이 비어 있을 때(= 스크리너 실패)만 그
하루치를 보충한다. 평상시 KRX OHLCV 호출은 0회다.

노션·카톡·메일에 쓰지 않는다.

로컬 실행:
    python scan_all.py
    python scan_all.py --end 2026-07-31
"""

from __future__ import annotations

import logging
import os
import sys
import time
from datetime import date
from pathlib import Path
from typing import Optional

import pandas as pd

import core
import screener
from screener import KrxUnavailable

logger = logging.getLogger(__name__)

# ── 설정 ────────────────────────────────────────────────────

DATA_DIR = screener.DATA_DIR
LATEST_PATH = DATA_DIR / "scan_latest.csv"

# ATR20은 TR 20개가, 직전 20일 고가는 당일을 뺀 20일이 필요하다.
# 둘 다 종가 21행이 최소치다 (= 상장 20거래일 미만이면 제외).
MIN_ROWS = core.HIGH_PERIOD + 1

MIN_TRADING_VALUE = 500_000_000    # 20일 평균 거래대금 5억
TRADING_VALUE_DAYS = 20

# 캐시 보충은 예외 경로다. 오래 끌지 않고 포기한 뒤 그날은 건너뛴다.
BACKFILL_BUDGET_SEC = 10 * 60

SCAN_COLUMNS = [
    "scan_date", "ticker", "name", "market", "close",
    "atr20", "high20", "low10",
    "gap", "gap_atr", "dist_to_break", "dist_atr",
    "unit_shares", "value_avg_20", "status",
]

STATUS_BREAKOUT = "돌파"
STATUS_NEAR = "임박"


def archive_path(day: str) -> Path:
    """일별 스냅샷 경로.

    gzip으로 저장한다. 원본 CSV는 하루 250KB라 1년이면 60MB가 되지만,
    압축하면 30KB 남짓이라 지우지 않고 계속 쌓아둘 수 있다. '임박했던
    종목이 실제로 돌파했는가'를 나중에 되짚으려면 이 스냅샷이 유일한
    근거다. pandas는 확장자를 보고 알아서 풀어 읽는다.
    """
    return DATA_DIR / f"scan_{day}.csv.gz"


# ── 시세 확보 ───────────────────────────────────────────────

def ensure_history(days: list[str]) -> tuple[pd.DataFrame, str]:
    """캐시를 읽고, 기준일이 비어 있으면 그 하루만 보충한다.

    screener.py가 16:30에 이미 당일 종가까지 채워두므로 보통은 그대로
    쓴다. 스크리너가 실패한 날에만 전종목 일괄 조회를 돌린다.

    Returns:
        (거래일이 days 안에 드는 시세, 실제 기준일)

    Raises:
        KrxUnavailable: 캐시가 비었고 보충도 실패했을 때
    """
    cache = screener.load_cache()
    have = set(cache["날짜"]) if not cache.empty else set()
    todo = [d for d in days if d not in have]

    if todo:
        deadline = time.monotonic() + BACKFILL_BUDGET_SEC
        logger.info(
            "캐시에 없는 거래일 %d일 – 보충합니다 (스크리너 실패 추정)",
            len(todo),
        )
        chunks: list[pd.DataFrame] = []
        for i, day in enumerate(todo, start=1):
            if time.monotonic() > deadline:
                logger.warning("보충 시간 초과 – %d/%d일에서 멈춥니다",
                               i - 1, len(todo))
                break
            try:
                chunks.append(screener.fetch_day(day))
            except KrxUnavailable as e:
                logger.warning("전종목 시세 %s 조회 실패: %s", day, e)
                break
            screener._sleep()

        if chunks:
            cache = screener._merge(cache, chunks)
            screener.save_cache(cache)

    if cache.empty:
        raise KrxUnavailable(
            "시세 캐시가 비어 있고 보충도 실패했습니다. "
            "screener.py를 먼저 돌려 data/ohlcv_cache.parquet을 만드세요."
        )

    hist = cache[cache["날짜"].isin(days)]
    available = sorted(hist["날짜"].unique()) if not hist.empty else []
    if not available:
        raise KrxUnavailable("요청 구간에 시세가 한 건도 없습니다.")
    return hist, available[-1]


# ── 종목별 계산 ─────────────────────────────────────────────

def scan_row(
    ticker: str,
    hist: pd.DataFrame,
    name: str,
    market: str,
    capital: float,
    scan_day: str,
) -> Optional[dict]:
    """한 종목의 원본 수치. 계산이 불가능하면 None.

    계산은 전부 core.py 함수로 한다 (Wilder 20일 ATR·직전 20일 고가·
    직전 10일 저가·1유닛 주수). 여기서 식을 다시 쓰지 않는다.
    """
    if len(hist) < MIN_ROWS:
        return None

    # 거래정지일은 시가·고가·저가가 0으로, 종가만 직전 값으로 남는다.
    # 그대로 TR을 내면 max(|0 - 전일종가|) = 전일종가가 되어 ATR이
    # 주가만큼 부풀고, 고가·저가도 0에 눌린다. 실제로 25일 정지 후
    # 재개된 종목이 ATR 8,516(주가 10,550원)으로 잡혀 dist_atr 0.03의
    # '임박 1위'가 됐다.
    #
    # 최근 구간(20일 고가를 재는 그 구간)에 정지일이 섞였으면 정지일을
    # 빼도 재개 갭이 TR로 남고, Wilder는 그걸 계속 물고 간다. ATR이
    # 실제 변동성이 아니게 되므로 추정하지 않고 종목을 뺀다.
    if (hist.tail(MIN_ROWS)["거래량"] <= 0).any():
        logger.debug("[%s] 최근 %d일에 거래정지 – 제외", ticker, MIN_ROWS)
        return None

    # 더 오래된 정지일은 빼고 계산한다. 남은 갭 TR은 Wilder 감쇠로 묻힌다.
    hist = hist[hist["거래량"] > 0]
    if len(hist) < MIN_ROWS:
        return None

    try:
        atr = core.round_atr(core.calc_atr(hist))
        sig = core.trend_signals(hist)
    except ValueError as e:
        logger.debug("[%s] 계산 불가: %s", ticker, e)
        return None

    # ATR이 0으로 반올림되면(저가·저변동주) ATR 배수 자체가 성립하지 않는다.
    # 추정하지 않고 뺀다 — core가 atr<=0에서 주수를 비우는 것과 같은 태도다.
    if atr <= 0:
        return None

    close = sig.current_price
    high20 = sig.high_20_prev
    gap = close - high20

    window = hist.tail(TRADING_VALUE_DAYS)
    value_avg = float((window["종가"] * window["거래량"]).mean())

    return {
        "scan_date": scan_day,
        "ticker": ticker,
        "name": name,
        "market": market,
        "close": round(close),
        "atr20": atr,
        "high20": round(high20),
        "low10": round(sig.low_10_prev),
        "gap": round(gap),
        "gap_atr": round(gap / atr, 3),
        "dist_to_break": round(-gap),
        "dist_atr": round(-gap / atr, 3),
        "unit_shares": core.calc_position(atr, capital).unit_shares,
        "value_avg_20": round(value_avg),
        "status": STATUS_BREAKOUT if sig.breakout else STATUS_NEAR,
    }


def scan(
    end: Optional[date] = None,
    capital: float = core.DEFAULT_CAPITAL,
) -> tuple[pd.DataFrame, str]:
    """전종목 스캔. 저장은 하지 않는다.

    Returns:
        (전종목 수치 DataFrame, 기준일 YYYYMMDD)
    """
    started = time.monotonic()
    end = end or date.today()

    warning = screener.check_krx_auth()
    if warning:
        logger.warning("%s", warning)

    days = screener.trading_days(end, screener.SCAN_DAYS)
    hist_all, scan_day = ensure_history(days)
    if scan_day != days[-1]:
        logger.warning("기준일을 %s → %s로 조정합니다", days[-1], scan_day)
        days = [d for d in days if d <= scan_day]
        hist_all = hist_all[hist_all["날짜"].isin(days)]
    logger.info("스캔 기준일 %s · 확보 거래일 %d일", scan_day, len(days))

    meta = screener.load_meta(scan_day)
    for note in meta.notes:
        logger.warning("%s", note)

    # 1차: 시장·관리종목·우선주·스팩 (screener와 같은 규칙)
    pf_meta = screener.prefilter_meta(meta)
    logger.info("1차 필터(메타): %d종목 → %d종목 (%s)",
                pf_meta.total, len(pf_meta.tickers),
                ", ".join(f"{k} {v}" for k, v in pf_meta.dropped.items() if v))

    # 2차: 거래정지·상장일수·유동성. 스크리너보다 문턱이 낮다 —
    # 여기서는 돌파 후보를 고르는 게 아니라 판까지 전부 기록하는 게 목적이다.
    pf = screener.prefilter_liquidity(
        hist_all, pf_meta.tickers, days,
        min_value=MIN_TRADING_VALUE,
        min_days=MIN_ROWS,
    )
    logger.info("2차 필터(유동성): %d종목 → %d종목 (%s)",
                pf.total, len(pf.tickers),
                ", ".join(f"{k} {v}" for k, v in pf.dropped.items()))

    target = hist_all[hist_all["티커"].isin(pf.tickers)].sort_values(
        ["티커", "날짜"]
    )
    groups = dict(tuple(target.groupby("티커")))

    rows: list[dict] = []
    skipped = 0
    for ticker in pf.tickers:
        group = groups.get(ticker)
        if group is None or group.empty:
            skipped += 1
            continue
        info = meta.frame.loc[ticker] if ticker in meta.frame.index else None
        row = scan_row(
            ticker,
            group,
            name=str(info["종목명"]) if info is not None else ticker,
            market=str(info["시장"]) if info is not None else "",
            capital=capital,
            scan_day=scan_day,
        )
        if row is None:
            skipped += 1
            continue
        rows.append(row)

    if not rows:
        raise KrxUnavailable("계산에 성공한 종목이 없습니다.")

    frame = pd.DataFrame(rows, columns=SCAN_COLUMNS)
    frame = frame.sort_values("dist_atr").reset_index(drop=True)

    breakouts = int((frame["status"] == STATUS_BREAKOUT).sum())
    logger.info(
        "스캔 완료: %d종목 (돌파 %d · 임박 %d · 계산불가 %d) · %.0f초",
        len(frame), breakouts, len(frame) - breakouts, skipped,
        time.monotonic() - started,
    )
    return frame, scan_day


# ── 저장 ────────────────────────────────────────────────────

def save_scan(frame: pd.DataFrame, day: str) -> tuple[Path, Path]:
    """일별 스냅샷(gz)과 스트림릿용 고정 경로에 각각 쓴다."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    archive = archive_path(day)
    frame.to_csv(archive, index=False, encoding="utf-8-sig",
                 compression="gzip")
    # 고정 경로는 매일 덮어쓰므로 쌓이지 않는다. 압축하지 않아야
    # 스트림릿·장중 스크립트가 확장자 분기 없이 그대로 읽는다.
    frame.to_csv(LATEST_PATH, index=False, encoding="utf-8-sig")
    logger.info("저장: %s (%.0fKB) · %s",
                archive.name, archive.stat().st_size / 1024, LATEST_PATH.name)
    return archive, LATEST_PATH


def load_scan() -> Optional[pd.DataFrame]:
    """scan_latest.csv. 없으면 None. (스트림릿·장중 스크립트 공용)"""
    if not LATEST_PATH.exists():
        return None
    frame = pd.read_csv(LATEST_PATH, dtype={"ticker": str, "scan_date": str})
    frame["ticker"] = frame["ticker"].str.zfill(6)
    return frame


def main() -> int:
    import argparse

    screener.setup_logging()
    logger.setLevel(logging.INFO)

    parser = argparse.ArgumentParser(description="KOSPI·KOSDAQ 전종목 스캔")
    parser.add_argument("--end", default=None,
                        help="기준일 YYYY-MM-DD (기본 오늘)")
    parser.add_argument("--capital", type=float, default=None,
                        help="총자본 (기본 TOTAL_CAPITAL 환경변수 또는 1천만원)")
    args = parser.parse_args()

    capital = args.capital
    if capital is None:
        capital = float(os.environ.get("TOTAL_CAPITAL") or core.DEFAULT_CAPITAL)

    try:
        frame, scan_day = scan(
            end=date.fromisoformat(args.end) if args.end else None,
            capital=capital,
        )
    except KrxUnavailable as e:
        # 직전 scan_latest.csv를 그대로 둔다 – 덮어쓰지 않는다.
        # 장중 스크립트가 빈 파일을 읽고 워치리스트를 잃는 것보다 낫다.
        logger.error("스캔 중단: %s", e)
        return 1

    save_scan(frame, scan_day)
    return 0


if __name__ == "__main__":
    sys.exit(main())
