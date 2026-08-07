"""
favorites_batch.py – 즐겨찾기 종목 야간 계산

노션 "즐겨찾기 모니터링" DB에 등록된 종목을 종가 기준으로 개별 조회해
ATR·20일고가·10일저가·갭(×ATR)·ATR%·판정을 계산하고 노션 계산 칸에
다시 쓴다. 입력 칸(종목명·코드·구분·메모)은 건드리지 않는다.

★ scan_all.py가 만드는 data/scan_latest.csv는 참조하지 않는다. 그
파일은 유동성 컷(20일 평균거래대금 5억 미만 제외)에 걸린 종목이 아예
빠져 있어서, 즐겨찾기에 넣은 저유동성 종목이 "데이터 없음"으로 뜰 수
있다. 사용자가 직접 지켜보겠다고 정한 종목은 시스템 필터와 무관하게
항상 계산돼야 한다 — 즐겨찾기 종목 수는 적어 개별 조회해도 부담 없다.

주식·ETF 모두 core.fetch_ohlcv(pykrx, 로그인 불필요)로 조회한다.
intraday_watch.py의 네이버 실시간 시세 조회와는 별개 경로다 — 그쪽은
장중 현재가 1건, 이쪽은 ATR 계산에 필요한 과거 OHLCV 전체가 필요하다.

보유종목 점검표 DB와는 완전히 별개다. 서로의 칸을 읽거나 쓰지 않는다.

로컬 실행:
    python favorites_batch.py
    python favorites_batch.py --dry-run     # 노션 기록 없이 계산만 출력
"""

from __future__ import annotations

import logging
import sys
import time
from typing import Optional

import core
import notion_repo

logger = logging.getLogger(__name__)

SLEEP_SEC = 0.3     # 종목 간 조회 간격 (스크래핑 부담 완화)


def calc_one(ticker: str) -> Optional[dict]:
    """한 종목의 계산 결과. 조회·계산이 불가능하면 None (로그만 남기고 건너뜀)."""
    try:
        df = core.fetch_ohlcv(ticker)
        atr = core.round_atr(core.calc_atr(df))
        price = core.latest_close(df)
        sig = core.trend_signals(df)
    except Exception as e:                  # noqa: BLE001 – 종목 하나 실패로 전체를 막지 않는다
        logger.warning("[%s] 조회/계산 실패: %s", ticker, e)
        return None

    if atr <= 0:
        logger.warning("[%s] ATR이 0 이하 – 계산 불가", ticker)
        return None

    gap_atr = (price - sig.high_20_prev) / atr
    return {
        "current_price": round(price),
        "atr": atr,
        "atr_pct": round(core.calc_atr_pct(atr, price), 2),
        "high20": round(sig.high_20_prev),
        "gap_atr": round(gap_atr, 3),
        "verdict": core.breakout_verdict(gap_atr),
        "low10": round(sig.low_10_prev),
    }


def run(dry_run: bool = False) -> int:
    try:
        favorites = notion_repo.fetch_favorites()
    except Exception:
        logger.exception("즐겨찾기 목록 조회 실패")
        return 1

    if not favorites:
        logger.info("즐겨찾기 종목 없음 – 종료")
        return 0

    ok = fail = 0
    for page_id, item in favorites:
        ticker = item["ticker"]
        calc = calc_one(ticker)
        if calc is None:
            fail += 1
            time.sleep(SLEEP_SEC)
            continue

        logger.info(
            "[%s(%s)] 현재가 %s · 갭 %+.2fN · %s",
            item["name"], ticker, f"{calc['current_price']:,.0f}",
            calc["gap_atr"], calc["verdict"],
        )

        if not dry_run:
            try:
                notion_repo.update_favorite_calc(page_id, calc)
            except Exception:
                logger.exception("[%s] 노션 갱신 실패", item["name"])
                fail += 1
                time.sleep(SLEEP_SEC)
                continue

        ok += 1
        time.sleep(SLEEP_SEC)

    logger.info("즐겨찾기 계산 완료: 성공 %d · 실패 %d", ok, fail)
    return 0


def main() -> int:
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        stream=sys.stdout,
    )

    parser = argparse.ArgumentParser(description="즐겨찾기 종목 야간 계산")
    parser.add_argument("--dry-run", action="store_true",
                        help="노션에 기록하지 않고 계산 결과만 출력한다")
    args = parser.parse_args()

    return run(dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
