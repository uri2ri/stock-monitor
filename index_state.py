"""
index_state.py – 진입일 지수 상태 태깅 (분석 전용)

개별 종목 터틀 진입의 성과가 "진입일의 지수 상태"에 따라 달라지는지
보려고 만든 모듈이다. 매매 규칙(진입·청산·게이트)에는 쓰지 않는다 -
backtest.py 거래 로그와 실거래 대조표에 태그를 붙이는 데만 쓴다.

[데이터]
data/index_ohlcv_KOSPI.csv · data/index_ohlcv_KOSDAQ.csv
(날짜, 시가, 고가, 저가, 종가) - 2019-06-01부터(20·60일 계산 워밍업 포함).
기존 data/index_regime_*.csv(종가·SMA60, 2023년부터)는 건드리지 않는다.

수집은 네이버 차트 API(fchart.stock.naver.com)로 한다. KRX(pykrx
get_index_ohlcv)는 지수 조회가 로그인 뒤로 옮겨가 익명 세션에선
실패한다 - 네이버는 로그인 없이 지수 일봉 시가·고가·저가·종가를 준다.
수집 결과는 index_regime_*.csv의 종가·60일선 판정과 겹치는 구간에서
대조해 출처 차이가 없는지 확인한다(--fetch 실행 시 출력).

[미래 참조 금지]
태그는 진입일(체결일) **전날까지**의 지수 일봉만으로 계산한다
(state_asof: `날짜 < 진입일` 행만 쓴다). 백테스트 close 모드에서 진입일
전날은 곧 돌파 신호일(종가 확인일)이라, "진입을 판단한 그 시점에 알 수
있던 지수 상태"와 같다. 진입일 당일의 지수 시가·고가·종가는 쓰지 않는다.

[태그 정의] C = 전일 종가
  a) 지수 터틀 상태(idx_turtle)
       C > 직전 20일 최고가(전일 제외 20일 고가 최댓값) → "지수 신고가"
       C < 직전 20일 최저가(전일 제외 20일 저가 최솟값) → "지수 신저가"
       그 외 → "중간"
     종목 진입 판정(core.trend_signals의 high_20_prev)과 같은 "당일 제외" 방식.
  b) 20일 채널 내 위치(idx_channel_pos)
       (C - 20일 최저가) / (20일 최고가 - 20일 최저가), 20일은 전일 포함.
       전일을 포함하므로 항상 0~1이다. 5구간(0.2 간격)으로 나눈다.
  c) 60일선(idx_above_sma60) - 기존 regime 정의 그대로(종가 > 전일 포함 60일
     종가 평균). 비교용.
  d) 직전 20일 수익률(idx_ret20) = C / 20거래일 전 종가 - 1.
     4구간: -5% 미만 / -5~0% / 0~5% / 5% 이상.

CLI:
    python index_state.py --fetch          # 두 지수 일봉 수집·저장·대조
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import pandas as pd
import requests

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent / "data"
MARKETS = ("KOSPI", "KOSDAQ")
INDEX_OHLCV_PATHS = {m: DATA_DIR / f"index_ohlcv_{m}.csv" for m in MARKETS}
INDEX_REGIME_PATHS = {m: DATA_DIR / f"index_regime_{m}.csv" for m in MARKETS}

FETCH_START = "20190601"           # dev 시작(2019-09) 전 60거래일 워밍업 포함
NAVER_CHART_URL = ("https://fchart.stock.naver.com/sise.nhn"
                   "?symbol={symbol}&timeframe=day&count={count}&requestType=0")
NAVER_COUNT = 3000                 # 2019-06 이후 전부 덮는 넉넉한 개수
TIMEOUT = 20

CHANNEL_DAYS = 20
SMA_DAYS = 60
RET_DAYS = 20

TURTLE_HIGH = "지수 신고가"
TURTLE_LOW = "지수 신저가"
TURTLE_MID = "중간"
SMA_ABOVE = "60일선 위"
SMA_BELOW = "60일선 아래"

CHANNEL_BUCKETS = ("0.0~0.2", "0.2~0.4", "0.4~0.6", "0.6~0.8", "0.8~1.0")
RET_BUCKETS = ("-5% 미만", "-5~0%", "0~5%", "5% 이상")

# 태그 칼럼 - backtest.PortfolioTrade 필드명과 같다.
TAG_FIELDS = ("idx_market", "idx_asof", "idx_turtle", "idx_channel_pos",
              "idx_channel_bucket", "idx_above_sma60", "idx_ret20", "idx_ret20_bucket")


# ── 수집 ────────────────────────────────────────────────────

_ITEM_RE = re.compile(r'<item data="([^"]+)"')


def parse_naver_chart(text: str) -> pd.DataFrame:
    """네이버 차트 XML → 날짜·시가·고가·저가·종가 DataFrame (날짜 오름차순)."""
    rows = []
    for raw in _ITEM_RE.findall(text):
        parts = raw.split("|")
        if len(parts) < 5:
            continue
        d, o, h, l, c = parts[:5]
        try:
            rows.append({"날짜": datetime.strptime(d, "%Y%m%d").date(),
                         "시가": float(o), "고가": float(h),
                         "저가": float(l), "종가": float(c)})
        except ValueError:
            continue
    frame = pd.DataFrame(rows, columns=["날짜", "시가", "고가", "저가", "종가"])
    return frame.sort_values("날짜").drop_duplicates("날짜").reset_index(drop=True)


def fetch_index_ohlcv(market: str, start: str = FETCH_START,
                      today: Optional[date] = None) -> pd.DataFrame:
    """지수 일봉을 받아 start 이후·오늘 이전(확정된 날)만 돌려준다.

    오늘 행은 장중이면 미확정 봉이라 뺀다 - 매일 다시 받아도 과거 값이
    바뀌지 않게 하려는 것이다.
    """
    resp = requests.get(NAVER_CHART_URL.format(symbol=market, count=NAVER_COUNT),
                        timeout=TIMEOUT, headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    frame = parse_naver_chart(resp.content.decode("euc-kr", errors="replace"))
    today = today or date.today()
    lo = datetime.strptime(start, "%Y%m%d").date()
    frame = frame[(frame["날짜"] >= lo) & (frame["날짜"] < today)]
    if frame.empty:
        raise RuntimeError(f"{market} 지수 일봉을 받지 못했습니다.")
    if frame["날짜"].iloc[0] > lo + pd.Timedelta(days=10):
        raise RuntimeError(f"{market} 지수 일봉이 {start}까지 거슬러 올라가지 않습니다 "
                           f"(첫 행 {frame['날짜'].iloc[0]}).")
    return frame.reset_index(drop=True)


def save_index_ohlcv(market: str, frame: pd.DataFrame) -> Path:
    path = INDEX_OHLCV_PATHS[market]
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    frame.to_csv(path, index=False, encoding="utf-8-sig")
    return path


def compare_with_regime(market: str, frame: pd.DataFrame) -> dict:
    """기존 index_regime_*.csv와 겹치는 구간에서 종가·60일선 판정을 대조한다."""
    path = INDEX_REGIME_PATHS[market]
    if not path.exists():
        return {"overlap": 0}
    regime = pd.read_csv(path)
    regime["날짜"] = pd.to_datetime(regime["날짜"]).dt.date
    ours = frame.copy()
    ours["SMA60"] = ours["종가"].rolling(SMA_DAYS).mean()
    ours["상승국면_재계산"] = ours["종가"] > ours["SMA60"]
    merged = regime.merge(ours[["날짜", "종가", "상승국면_재계산", "SMA60"]],
                          on="날짜", suffixes=("_기존", "_신규"))
    merged = merged[merged["SMA60_신규"].notna() & merged["SMA60_기존"].notna()]
    return {
        "overlap": len(merged),
        "close_max_abs_diff": float((merged["종가_기존"] - merged["종가_신규"]).abs().max())
        if len(merged) else float("nan"),
        "regime_mismatch": int((merged["상승국면"] != merged["상승국면_재계산"]).sum()),
    }


# ── 태깅 ────────────────────────────────────────────────────

def load_index_ohlcv(market: str) -> pd.DataFrame:
    """저장된 지수 일봉. 날짜(Timestamp) 인덱스, 오름차순."""
    path = INDEX_OHLCV_PATHS[market]
    if not path.exists():
        raise FileNotFoundError(f"{path}가 없습니다. python index_state.py --fetch 먼저.")
    frame = pd.read_csv(path)
    frame["날짜"] = pd.to_datetime(frame["날짜"])
    return frame.set_index("날짜").sort_index()[["시가", "고가", "저가", "종가"]]


def load_all_index_ohlcv() -> dict[str, pd.DataFrame]:
    return {m: load_index_ohlcv(m) for m in MARKETS}


def channel_bucket(pos: Optional[float]) -> Optional[str]:
    if pos is None:
        return None
    i = min(int(pos * 5), 4)        # 1.0은 마지막 구간
    return CHANNEL_BUCKETS[max(i, 0)]


def ret20_bucket(ret: Optional[float]) -> Optional[str]:
    if ret is None:
        return None
    if ret < -0.05:
        return RET_BUCKETS[0]
    if ret < 0:
        return RET_BUCKETS[1]
    if ret < 0.05:
        return RET_BUCKETS[2]
    return RET_BUCKETS[3]


def normalize_market(market: Optional[str]) -> Optional[str]:
    """ticker_market.csv 표기 → 태그용 지수. KOSDAQ GLOBAL은 KOSDAQ."""
    if not market:
        return None
    m = str(market).upper()
    if m == "KOSPI":
        return "KOSPI"
    if m.startswith("KOSDAQ"):
        return "KOSDAQ"
    return None


def empty_tags(market: Optional[str] = None) -> dict:
    tags = {k: None for k in TAG_FIELDS}
    tags["idx_market"] = market
    return tags


def state_asof(index_df: pd.DataFrame, entry_date: date,
               market: Optional[str] = None) -> dict:
    """entry_date **전날까지**의 지수 일봉으로 태그를 계산한다.

    `날짜 < entry_date` 행만 쓴다 - 진입일 당일 이후 값은 어떤 경로로도
    들어오지 않는다. 데이터가 모자라면(워밍업 부족) 해당 태그는 None.
    """
    hist = index_df[index_df.index < pd.Timestamp(entry_date)]
    tags = empty_tags(market)
    if hist.empty:
        return tags
    close = float(hist["종가"].iloc[-1])
    tags["idx_asof"] = hist.index[-1].date()

    # a) 직전 20일(전일 제외) 고가·저가 대비 전일 종가
    if len(hist) >= CHANNEL_DAYS + 1:
        prior = hist.iloc[-(CHANNEL_DAYS + 1):-1]
        if close > float(prior["고가"].max()):
            tags["idx_turtle"] = TURTLE_HIGH
        elif close < float(prior["저가"].min()):
            tags["idx_turtle"] = TURTLE_LOW
        else:
            tags["idx_turtle"] = TURTLE_MID

    # b) 20일 채널 내 위치(전일 포함)
    if len(hist) >= CHANNEL_DAYS:
        win = hist.iloc[-CHANNEL_DAYS:]
        hi, lo = float(win["고가"].max()), float(win["저가"].min())
        if hi > lo:
            pos = min(max((close - lo) / (hi - lo), 0.0), 1.0)
            tags["idx_channel_pos"] = round(pos, 4)
            tags["idx_channel_bucket"] = channel_bucket(pos)

    # c) 60일선(기존 regime 정의: 종가 > 전일 포함 60일 종가 평균)
    if len(hist) >= SMA_DAYS:
        sma = float(hist["종가"].iloc[-SMA_DAYS:].mean())
        tags["idx_above_sma60"] = SMA_ABOVE if close > sma else SMA_BELOW

    # d) 직전 20일 수익률
    if len(hist) >= RET_DAYS + 1:
        base = float(hist["종가"].iloc[-(RET_DAYS + 1)])
        if base > 0:
            ret = close / base - 1
            tags["idx_ret20"] = round(ret, 4)
            tags["idx_ret20_bucket"] = ret20_bucket(ret)
    return tags


def tag_for(ticker_market: dict[str, str], index_dfs: dict[str, pd.DataFrame],
            ticker: str, entry_date: date) -> dict:
    """종목이 속한 시장 지수로 태그. 시장을 모르면 빈 태그(추정하지 않는다)."""
    market = normalize_market(ticker_market.get(ticker))
    if market is None or market not in index_dfs:
        return empty_tags(None)
    return state_asof(index_dfs[market], entry_date, market)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="지수 일봉 수집 (진입일 지수 상태 태깅용)")
    parser.add_argument("--fetch", action="store_true", help="두 지수 일봉 수집·저장")
    parser.add_argument("--start", default=FETCH_START, help="YYYYMMDD (기본 20190601)")
    args = parser.parse_args()
    if not args.fetch:
        parser.print_help()
        return 0
    for market in MARKETS:
        frame = fetch_index_ohlcv(market, start=args.start)
        path = save_index_ohlcv(market, frame)
        cmp = compare_with_regime(market, frame)
        print(f"{market}: {len(frame)}행 {frame['날짜'].iloc[0]} ~ {frame['날짜'].iloc[-1]} "
              f"→ {path.name} · 기존 regime 대조 {cmp}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
