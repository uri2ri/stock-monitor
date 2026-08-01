"""
build_universe.py – data/universe.json 갱신

screener.py의 폴백(종목별 개별 조회) 경로가 쓸 종목 목록을 만든다.
전종목 일괄 조회가 살아 있으면 screener는 이 파일을 쓰지 않는다.

조회 순서 (앞이 실패하면 다음으로):
  1. KRX 전종목 시세 원본 – 1회 호출로 티커·이름·시장을 모두 받는다
  2. get_market_ticker_list – 시장별 티커 목록.
     이름은 종목당 1회라 오래 걸리므로 --names를 줄 때만 받는다
     (없으면 screener가 통과 종목에 한해서만 채운다)
  3. 둘 다 막히면 직접 채울 수 있는 빈 템플릿을 만든다

신규 상장 종목은 120거래일을 못 채워 어차피 필터에서 빠지므로,
목록이 며칠 낡아도 결과에 영향이 없다.

CLI:
    python build_universe.py
    python build_universe.py --names          # 종목명까지 (느림)
    python build_universe.py --max-age 7      # 최근 7일 내 파일이면 건너뜀
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import date
from typing import Optional

import pandas as pd
from pykrx import stock as krx

import screener

logger = logging.getLogger(__name__)


def _age_days(path) -> Optional[int]:
    """universe.json의 built_on 기준 경과일. 읽을 수 없으면 None."""
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not raw.get("stocks"):
            return None
        return (date.today() - date.fromisoformat(raw["built_on"])).days
    except Exception:                       # noqa: BLE001
        return None


def from_bulk(day: str) -> Optional[pd.DataFrame]:
    """1순위: 전종목 시세 원본에서 티커·이름·시장을 1회로 받는다."""
    try:
        meta = screener.fetch_meta_bulk(day)
    except Exception as e:                  # noqa: BLE001
        logger.warning("전종목 원본 조회 실패: %s", e)
        return None

    frame = meta.frame[meta.frame["시장"].isin(screener.MARKETS)]
    logger.info("전종목 원본에서 %d종목", len(frame))
    return frame[["종목명", "시장"]]


def from_ticker_list(day: str, with_names: bool) -> Optional[pd.DataFrame]:
    """2순위: 시장별 티커 목록. 이름은 --names일 때만 종목당 1회로 받는다."""
    rows: list[dict] = []
    for market in screener.MARKETS:
        try:
            tickers = screener._call(
                f"{market} 종목 목록",
                lambda m=market: krx.get_market_ticker_list(day, market=m),
            )
        except Exception as e:              # noqa: BLE001
            logger.warning("%s 종목 목록 조회 실패: %s", market, e)
            continue
        logger.info("%s %d종목", market, len(tickers))
        rows.extend(
            {"티커": str(t).zfill(6), "종목명": "", "시장": market}
            for t in tickers
        )

    if not rows:
        return None

    frame = pd.DataFrame(rows).set_index("티커")
    frame.index.name = "티커"

    if with_names:
        logger.info("종목명 조회 %d건 (약 %.0f분)",
                    len(frame), len(frame) * screener.FALLBACK_SLEEP / 60)
        step = max(1, len(frame) // 10)
        for i, ticker in enumerate(frame.index, start=1):
            try:
                frame.at[ticker, "종목명"] = krx.get_market_ticker_name(ticker)
            except Exception as e:          # noqa: BLE001
                # 이름을 못 받아도 목록 자체는 쓸 수 있다. 비워 둔다.
                logger.debug("[%s] 종목명 실패: %s", ticker, e)
            if i % step == 0:
                logger.info("종목명 진행률 %d%% (%d/%d)",
                            i * 100 // len(frame), i, len(frame))
            screener._sleep(screener.FALLBACK_SLEEP)

    return frame


def main() -> int:
    import argparse

    screener.setup_logging()
    logger.setLevel(logging.INFO)

    parser = argparse.ArgumentParser(description="종목 목록(universe.json) 갱신")
    parser.add_argument("--end", default=None,
                        help="기준일 YYYY-MM-DD (기본 오늘)")
    parser.add_argument("--names", action="store_true",
                        help="티커 목록 경로에서도 종목명을 받는다 (느림)")
    parser.add_argument("--max-age", type=int, default=None,
                        help="이 일수 안에 만든 파일이 있으면 갱신하지 않는다")
    args = parser.parse_args()

    if args.max_age is not None:
        age = _age_days(screener.UNIVERSE_PATH)
        if age is not None and age <= args.max_age:
            logger.info("universe.json이 %d일 전 파일 – 갱신을 건너뜁니다", age)
            return 0

    end = date.fromisoformat(args.end) if args.end else date.today()

    # 거래일 달력은 개별 종목 조회라 전종목이 막혀도 대개 살아 있다
    try:
        day = screener.trading_days(end, 1)[-1]
    except Exception as e:                  # noqa: BLE001
        logger.warning("거래일 확인 실패, 요청일을 그대로 씁니다: %s", e)
        day = end.strftime("%Y%m%d")

    frame = from_bulk(day)
    source = "KRX 전종목 시세"
    if frame is None:
        frame = from_ticker_list(day, with_names=args.names)
        source = "get_market_ticker_list"

    if frame is None or frame.empty:
        # 자동 조회가 전부 막힌 경우. 직접 채울 수 있게 형식만 만들어 둔다.
        if _age_days(screener.UNIVERSE_PATH) is not None:
            logger.error("종목 목록을 받지 못했습니다. 기존 파일을 그대로 둡니다.")
            return 1
        path = screener.write_universe_template()
        logger.error(
            "종목 목록을 받지 못했습니다. 빈 템플릿을 만들었습니다: %s\n"
            "  stocks 배열에 "
            '{"ticker": "005930", "name": "삼성전자", "market": "KOSPI"} '
            "형식으로 직접 채우면 폴백 경로가 동작합니다.",
            path,
        )
        return 1

    # 우선주·스팩·ETN은 어차피 스크리너가 거른다. 목록 단계에서 빼두면
    # 폴백의 종목당 1회 조회가 그만큼 줄어든다.
    before = len(frame)
    keep = [
        t for t, row in frame.iterrows()
        if t[-1] not in screener.PREF_SHARE_SUFFIXES
        and not screener._name_excluded(str(row["종목명"] or ""))
    ]
    frame = frame.loc[keep]
    logger.info("우선주·스팩·ETN 제외: %d → %d종목", before, len(frame))

    path = screener.save_universe(frame, as_of=day, source=source)
    named = int((frame["종목명"].astype(str) != "").sum())
    logger.info("저장: %s (%d종목, 종목명 %d개)", path, len(frame), named)
    if named < len(frame):
        logger.info("종목명이 없는 종목은 스크리너가 통과 시점에만 조회합니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
