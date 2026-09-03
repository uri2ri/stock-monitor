"""
backtest.py – 백테스트 골격 (1단계: 단일 종목) + 2단계: 진입 모드 비교

1단계 목적은 "미래를 훔쳐보지 않고 하루씩 전진하는 루프"가 제대로 도는지
확인하는 것이었다(단일 종목, close 모드). 2단계는 그 골격 위에 진입 모드
두 가지(close/intraday)를 얹어 "장중 실시간 진입이 종가 확인 진입보다
나은가"를 전종목·전기간으로 비교한다.

두 단계 다 신호는 진입(20일 고가 돌파 + 추격금지 필터)과 손절(-2ATR
래칫)만 본다. 피라미딩·상관군 캡·일일 상한·복수 종목 동시 처리는 여전히
범위 밖이다 - 변수를 늘리면 두 모드 비교의 의미가 흐려진다.

core.py 판정 함수(calc_atr·trend_signals·breakout_verdict·calc_position·
evaluate_holding)를 그대로 가져다 쓴다. 새 판정 로직은 만들지 않는다.
core.py 소스 자체는 건드리지 않는다.

[미래 참조 방지의 핵심 장치]
evaluate_holding(as_of=...)은 내부에서 core.fetch_ohlcv(ticker, end=as_of)를
부르는데, 그대로 두면 시뮬레이션 날짜마다 pykrx 실시간 API를 수백~수만 번
때리게 된다. 그래서 core.fetch_ohlcv 함수 참조 자체를 이 스크립트 실행
동안만 런타임에 바꿔치기한다(모듈 전역 이름 조회라 가능 - core.py 소스는
안 바뀐다). 바꿔친 함수는 반드시 end를 요구하고, backtest_ohlcv.parquet
에서 그 날짜 이전 데이터만 슬라이스해서 돌려준다.

[두 진입 모드]
- close 모드: 당일 종가로 돌파 판정 -> 다음 거래일 시가로 체결.
- intraday 모드 (라이브 시스템 근사): 당일 "고가"가 전일까지의 20일 고가를
  넘으면 그날 즉시 체결한 것으로 본다. 임계값·ATR은 전일 데이터까지만
  쓴다(당일 데이터는 아직 확정 전이라 쓰면 안 된다 - 실시간 시스템도
  어젯밤 스캔 값을 오늘 장중 내내 그대로 쓴다). 체결가는:
    - 당일 시가가 이미 임계값 이상(갭상승)이면 시가
    - 아니면 임계값(20일 고가) 가격
  에 슬리피지·비용을 얹는다. 추격금지는 체결가가 아니라 "당일 시가"
  기준으로 별도 계산한다 - 체결가 기준으로 하면 갭상승이 아닌 보통
  케이스(체결가=임계값)에서 갭이 항상 0이 돼 추격금지가 무력화된다.
  시가 기준이면 갭상승 여부가 정상 반영되고, 라이브 시스템이 장중
  현재가로 갭을 재는 것과 방향이 맞다.
    갭상승이 아닌 경우(시가가 임계값 아래, 장중에 돌파) gap_atr<=0이
  나오는데, core.breakout_verdict()는 이를 "대기"(아직 안 넘었다)로
  분류한다 - 하지만 이 시점엔 이미 당일 고가로 돌파 자체를 확인했으므로
  "대기"를 "갭상승이 아니라 추격 걱정 없음"으로 해석해 통과시킨다.
  실제로 거르는 건 "추격금지" 판정뿐이다. 갭상승 케이스에서만 추격금지가
  의미 있게 작동하고, 그 판단 기준(시가)조차 10분 폴링의 근사라는 한계는
  여전하다 - 결과 해석 시 감안할 것.

  청산(손절)은 두 모드 다 동일하다 - close 모드 그대로(오늘 종가로
  core.evaluate_holding() 판정 -> 다음 거래일 시가 체결). 이번 비교는
  진입 시점·체결가의 차이만 보는 것이 목적이라 청산 로직은 건드리지
  않는다.

[포지션 크기]
core.calc_position(entry_atr, capital, risk)을 그대로 쓴다. 계좌는
1000만원 고정(복리 미반영 - 두 모드를 같은 조건에서 비교하기 위한 통제).

[유동성 필터]
screener.py와 같은 기준(20일 평균 거래대금 10억 미만 제외, 당일 포함)을
쓴다 - 새 숫자를 만들지 않고 screener.MIN_TRADING_VALUE를 그대로 가져다
쓴다.

[개발/검증 구간 분리]
같은 데이터로 파라미터를 고르고 성적도 확인하면 결과가 그 데이터에만 맞춰진
아티팩트가 된다. --period로 구간을 나눈다:
  - dev     (2019-09-01 ~ 2023-08-31, 기본값): 모든 파라미터 실험은 여기서만.
  - holdout (2023-09-01 ~ 2026-08-31): 검증 전용. 실행 시 경고 배너 + 이력 기록
            (data/holdout_runs.log). 여러 번 실행하면 그만큼 오염된 것이다.
  - full    (전체): 기준선 스냅샷 전용. 기본 실험 흐름엔 쓰지 않는다.
dev/full 모드에서는 holdout 구간 데이터 로드 자체가 예외로 막힌다
(core.fetch_ohlcv를 end 없이 부르면 예외를 던지는 것과 같은 장치).

CLI:
    python backtest.py                                  # 1단계: 삼성전자, close 모드, dev 구간
    python backtest.py --ticker 000660
    python backtest.py --tickers 006660,078930 --risk 0.01

    # 2단계: 전종목 두 모드 비교
    python backtest.py --compare-modes
    python backtest.py --compare-modes --limit 30       # 스모크 테스트용 일부 종목만

    # 3단계: 실운용 규칙 재현 (기준선: 손절 2.0 ATR, 필터 없음)
    python backtest.py --portfolio                      # dev 구간, fixed 자본모드(기본)
    python backtest.py --portfolio --capital-mode compound   # dev를 실계좌 방식으로
    python backtest.py --portfolio --period holdout     # 검증 (compound 기본 + 경고 + 이력)
    python backtest.py --portfolio --period full        # 전체 기준선 스냅샷

[자본 모드]
dev 기준선이 2020년 상반기에 계좌 파산 후 3년 반을 현금부족으로 거의 못 거래해서,
파라미터를 바꿔도 "전략이 개선됐는지"와 "초반 운으로 파산을 면했는지"가 안 갈렸다.
그래서 --capital-mode:
  - fixed   : 유닛 크기를 항상 초기 자본 기준으로 계산, 현금부족 제외 없음.
              모든 신호를 동일 조건에서 평가한다. dev 기본값. 실계좌 아님.
  - compound: 복리 + 현금 제약(지금까지 방식). holdout/full 기본값. 실계좌 재현.
피라미딩·상관군캡·일일상한은 두 모드 공통이다. 어느 모드로 돌렸는지는 결과 출력과
holdout_runs.log에 남는다.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import pandas as pd

import core
import screener

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s",
                     stream=sys.stdout)
logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent / "data"
BACKTEST_CACHE_PATH = DATA_DIR / "backtest_ohlcv.parquet"

# ── 체결 가정 (단순화) ──────────────────────────────────────
SLIPPAGE = 0.003        # 슬리피지 0.3% - 매수는 불리하게(비싸게), 매도는 불리하게(싸게)
COST_RATE = 0.0025      # 세금+수수료 합산 0.25% (매수·매도 각각 부과)
STARTING_CAPITAL = 10_000_000  # 계좌 시작 1000만원. 복리 반영 안 함.

DEFAULT_TICKER = "005930"       # 삼성전자 - 전 구간 커버, 상장폐지 없음
# 기간은 --period(dev/holdout/full)가 정한다. --start/--end는 그 안에서 좁힐 때만.

MIN_TURNOVER = screener.MIN_TRADING_VALUE       # 20일 평균 거래대금 10억
TURNOVER_WINDOW = screener.TRADING_VALUE_DAYS   # 20일


# ── 개발/검증 구간 분리 ─────────────────────────────────────
#
# 같은 데이터로 파라미터를 고르고 성적도 확인하면, 결과가 그 데이터에만
# 맞춰진 아티팩트가 된다 - 3년 백테스트에서 "1.0 ATR 잭팟"·"국면필터 흑자"가
# 전부 그랬다. 그래서 데이터를 둘로 자른다:
#
#   - dev     (2019-09-01 ~ 2023-08-31): 파라미터 탐색·성적 확인은 전부 여기서만.
#   - holdout (2023-09-01 ~ 2026-08-31): 손대지 않고 남겨둔 검증 구간. 이 구간
#     성적을 보고 파라미터를 바꾸는 순간 이 구간도 더 이상 검증용이 아니다.
#   - full    (전체): 이번처럼 기준선을 한 번에 볼 때만. 기본 실험 흐름엔 안 쓴다.
#
# dev 모드에서는 holdout 구간 데이터가 애초에 메모리에 안 올라온다 -
# load_ticker_df / list_universe_tickers가 구간 상한에서 잘라내고,
# core.fetch_ohlcv 대체 함수도 end가 상한을 넘으면 예외를 던진다
# (core.fetch_ohlcv를 end 없이 부르면 예외를 던지게 한 것과 같은 장치).
# 워밍업(과거 히스토리) 목적의 하한 클립은 하지 않는다 - holdout 실행이
# 2023-09-01 첫날 신호를 계산하려면 그 이전 dev 구간 시세가 필요하고,
# 그건 미래참조가 아니다.

DEV_START = date(2019, 9, 1)
DEV_END = date(2023, 8, 31)
HOLDOUT_START = date(2023, 9, 1)
HOLDOUT_END = date(2026, 8, 31)

PERIOD_BOUNDS: dict[str, tuple[date, date]] = {
    "dev": (DEV_START, DEV_END),
    "holdout": (HOLDOUT_START, HOLDOUT_END),
    "full": (DEV_START, HOLDOUT_END),
}

HOLDOUT_LOG_PATH = DATA_DIR / "holdout_runs.log"

# 현재 실행의 구간 상한. None이면 구간 미설정(가드·클립 모두 비활성).
_PERIOD_NAME: Optional[str] = None
_PERIOD_MAX: Optional[pd.Timestamp] = None


def set_period(name: str) -> tuple[date, date]:
    """실행 구간을 정하고 (start, end)를 돌려준다.

    이 호출 이후 load_ticker_df / list_universe_tickers / fetch_ohlcv 대체
    함수가 _PERIOD_MAX 이후 데이터를 걸러낸다.
    """
    global _PERIOD_NAME, _PERIOD_MAX
    if name not in PERIOD_BOUNDS:
        raise ValueError(f"모르는 구간: {name!r} (dev | holdout | full)")
    lo, hi = PERIOD_BOUNDS[name]
    _PERIOD_NAME = name
    _PERIOD_MAX = pd.Timestamp(hi)
    return lo, hi


def _guard_period_end(end: Optional[date], where: str) -> None:
    """fetch_ohlcv 대체 함수 진입 가드 - end가 현재 구간 상한을 넘으면 예외.

    dev 모드에서 holdout 구간 시세를 조회하려는 시도를 조용히 통과시키지
    않는다. core.fetch_ohlcv를 end 없이 부르면 예외를 던지는 것과 같은 취지.
    """
    if _PERIOD_MAX is None or end is None:
        return
    if pd.Timestamp(end) > _PERIOD_MAX:
        raise RuntimeError(
            f"[{where}] 구간 가드: 지금은 '{_PERIOD_NAME}' 구간(≤ {_PERIOD_MAX.date()})인데 "
            f"end={end} 데이터를 조회하려 했다 - 검증 구간을 훔쳐보는 미래참조."
        )


def _clip_to_period(df: pd.DataFrame) -> pd.DataFrame:
    """날짜 인덱스 DataFrame을 현재 구간 상한까지로 자른다(상한 없으면 그대로)."""
    if _PERIOD_MAX is None:
        return df
    return df[df.index <= _PERIOD_MAX]


HOLDOUT_BANNER = (
    "\n"
    "╔══════════════════════════════════════════════════════════════════════╗\n"
    "║  검증 구간 실행 — 이 결과를 보고 파라미터를 바꾸면 이 구간은 더 이상  ║\n"
    "║  검증용으로 쓸 수 없습니다.                                            ║\n"
    "╚══════════════════════════════════════════════════════════════════════╝\n"
)


def _count_holdout_runs() -> int:
    if not HOLDOUT_LOG_PATH.exists():
        return 0
    return sum(
        1 for line in HOLDOUT_LOG_PATH.read_text(encoding="utf-8").splitlines()
        if line.startswith("=== holdout run #")
    )


def record_holdout_run(config: dict, summary: dict) -> int:
    """holdout 모드 실행 이력을 파일에 남긴다(시각, 설정, 결과 요약).

    이 파일에 쌓인 실행 횟수가 곧 "검증 구간을 몇 번 들여다봤는가"다 -
    1번을 넘어가면 그만큼 이 구간이 파라미터 선택에 오염됐다는 뜻이다.
    실행 번호를 돌려준다.
    """
    n = _count_holdout_runs() + 1
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [f"=== holdout run #{n} @ {stamp} ==="]
    lines += [f"  [설정] {k}: {v}" for k, v in config.items()]
    lines += [f"  [결과] {k}: {v}" for k, v in summary.items()]
    lines.append("")
    with HOLDOUT_LOG_PATH.open("a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return n


# ── 데이터 준비 ─────────────────────────────────────────────

_FULL_RAW_DF: Optional[pd.DataFrame] = None


def _load_full_raw() -> pd.DataFrame:
    """backtest_ohlcv.parquet 전체를 프로세스당 한 번만 읽는다.

    전종목 비교에서 종목마다 22MB 파일을 다시 읽으면 안 되므로 캐싱한다.
    """
    global _FULL_RAW_DF
    if _FULL_RAW_DF is None:
        if not BACKTEST_CACHE_PATH.exists():
            raise FileNotFoundError(
                f"{BACKTEST_CACHE_PATH}가 없습니다. backtest_data.py를 먼저 돌리세요."
            )
        _FULL_RAW_DF = pd.read_parquet(BACKTEST_CACHE_PATH)
    return _FULL_RAW_DF


def load_ticker_df(ticker: str) -> pd.DataFrame:
    """이 종목만 추려 core.py가 기대하는 형태(날짜 인덱스 +
    시가/고가/저가/종가/거래량 + 유동성충족 bool)로 만든다."""
    full = _load_full_raw()
    sub = full[full["티커"] == ticker].copy()
    if sub.empty:
        raise ValueError(f"[{ticker}] backtest_ohlcv.parquet에 데이터 없음")

    sub["날짜"] = pd.to_datetime(sub["날짜"], format="%Y%m%d")
    sub = sub.set_index("날짜").sort_index()
    sub = sub[sub["종가"] > 0]
    sub = _clip_to_period(sub)   # dev 모드면 여기서 holdout 구간이 잘려 나간다

    turnover = sub["종가"] * sub["거래량"]
    sub["유동성충족"] = turnover.rolling(TURNOVER_WINDOW).mean() >= MIN_TURNOVER

    return sub[["시가", "고가", "저가", "종가", "거래량", "유동성충족"]]


def list_universe_tickers(min_turnover: float = MIN_TURNOVER,
                           window: int = TURNOVER_WINDOW) -> list[str]:
    """backtest_ohlcv.parquet의 전종목 중, 단 한 번이라도 20일 평균 거래대금이
    기준을 넘긴 적 있는 종목만 추린다.

    한 번도 기준을 못 넘긴 종목은 day-by-day 루프까지 갈 것도 없이 여기서
    걸러 계산량을 줄인다. 실제 "그날 거래 가능한가" 판단은 여전히
    load_ticker_df의 유동성충족 컬럼으로 날짜별로 다시 본다 - 종목이 한 번
    기준을 넘겼다고 전체 기간이 다 유효해지는 게 아니다.
    """
    full = _load_full_raw().copy()
    if _PERIOD_MAX is not None:
        full = full[pd.to_datetime(full["날짜"], format="%Y%m%d") <= _PERIOD_MAX]
    full = full.sort_values(["티커", "날짜"])
    turnover = full["종가"] * full["거래량"]
    avg = turnover.groupby(full["티커"]).transform(lambda s: s.rolling(window).mean())
    qualifying = full.loc[avg >= min_turnover, "티커"].unique()
    return sorted(qualifying.tolist())


# ── core.fetch_ohlcv 바꿔치기 (미래 참조 방지) ─────────────────

_TICKER_DF: Optional[pd.DataFrame] = None
_TICKER_CODE: Optional[str] = None


def _cached_fetch_ohlcv(code: str, days: int = core.FETCH_DAYS,
                         end: Optional[date] = None) -> pd.DataFrame:
    """core.fetch_ohlcv 대체 - 로컬 캐시에서 end 이전 데이터만 슬라이스한다.

    end가 없으면 예외를 올린다 - 이 백테스트에서 end 없는 조회는 전부
    "오늘"을 참조하는 미래참조 버그이므로 조용히 넘어가면 안 된다.
    """
    if end is None:
        raise ValueError(
            "백테스트에서는 end(as_of) 없는 fetch_ohlcv 호출을 허용하지 않는다 "
            "- 미래 참조 방지 가드"
        )
    _guard_period_end(end, "_cached_fetch_ohlcv")
    if code != _TICKER_CODE or _TICKER_DF is None:
        raise ValueError(f"[{code}] 이 백테스트는 {_TICKER_CODE}용으로만 준비됐다")

    end_ts = pd.Timestamp(end)
    df = _TICKER_DF[_TICKER_DF.index <= end_ts]
    if df.empty:
        raise ValueError(f"[{code}] {end} 이전 데이터 없음")
    df = df[["시가", "고가", "저가", "종가", "거래량"]].tail(days)
    if len(df) < core.MIN_TRADING_DAYS:
        raise ValueError(
            f"[{code}] 거래일 부족: {len(df)}일 < 최소 {core.MIN_TRADING_DAYS}일"
        )
    return df


# ── 신호 판정 (core.py 함수 조합 - 새 로직 없음) ────────────────

# 진입 돌파 판정 기간. 기본은 core.HIGH_PERIOD(20). run_portfolio_backtest가
# breakout_period 인자를 받으면 실행 동안만 이 값을 바꿔치기한다(core 소스는
# 그대로 - core.trend_signals(df, high_period=...)로 넘길 뿐이다). ATR 기간·
# 추세청산(10일 저가)은 건드리지 않는다 - 변수 하나만 바꾸는 실험용.
_ENTRY_HIGH_PERIOD: int = core.HIGH_PERIOD

# 진입 거래량 배수 하한. 1.0이면 사실상 필터 없음. run_portfolio_backtest가
# vol_mult_min 인자를 받으면 실행 동안만 바꿔치기한다. 배수 = 신호일 거래량 /
# 직전 20일 평균 거래량(신호일 제외). screener의 VOL_MULT와 같은 정의.
_ENTRY_VOL_MULT_MIN: float = 1.0


def _passes_vol_mult(df: pd.DataFrame) -> bool:
    """df의 마지막 행(신호일) 거래량이 직전 20일 평균의 _ENTRY_VOL_MULT_MIN배 이상인가.

    신호일 거래량은 이미 확정된 값이라 미래참조가 아니다(스캔은 종가 후 실행).
    """
    if _ENTRY_VOL_MULT_MIN <= 1.0:
        return True
    vols = df["거래량"]
    if len(vols) < 21:
        return False
    prev20 = float(vols.iloc[-21:-1].mean())
    if prev20 <= 0:
        return False
    return float(vols.iloc[-1]) / prev20 >= _ENTRY_VOL_MULT_MIN


def _entry_signal_close(as_of: date) -> Optional[dict]:
    """close 모드: 당일 종가 기준 돌파 + 추격금지 아님.

    intraday_watch.judge()·screener.py와 같은 조합(core.trend_signals +
    core.calc_atr + core.breakout_verdict)을 그대로 쓴다.
    """
    df = _cached_fetch_ohlcv(_TICKER_CODE, end=as_of)
    try:
        sig = core.trend_signals(df, high_period=_ENTRY_HIGH_PERIOD)
    except ValueError:
        return None
    atr = core.round_atr(core.calc_atr(df))
    if atr <= 0:
        return None
    gap_atr = (sig.current_price - sig.high_20_prev) / atr
    verdict = core.breakout_verdict(gap_atr)
    if verdict != "진입가능":
        return None
    return {"atr": atr, "base_price": sig.current_price, "threshold": sig.high_20_prev,
            "gap_atr": gap_atr}


def _entry_signal_intraday(as_of: date) -> Optional[dict]:
    """intraday 모드: 당일 고가가 전일까지의 20일 고가를 넘으면 즉시 진입.

    임계값·ATR은 전일(as_of 하루 전) 데이터까지만으로 계산한다 - 당일 고가/
    종가는 아직 확정 전이므로 포함하면 안 된다.
    """
    idx = _TICKER_DF.index.get_indexer([pd.Timestamp(as_of)])[0]
    if idx <= 0:
        return None
    prev_day = _TICKER_DF.index[idx - 1].date()

    try:
        df_prev = _cached_fetch_ohlcv(_TICKER_CODE, end=prev_day)
        sig = core.trend_signals(df_prev, high_period=_ENTRY_HIGH_PERIOD)
    except ValueError:
        return None
    atr = core.round_atr(core.calc_atr(df_prev))
    if atr <= 0:
        return None

    threshold = sig.high_20  # 전일까지 슬라이스의 "당일 포함 20일 고가" = 오늘의 판단 기준선
    today_high = float(_TICKER_DF.loc[pd.Timestamp(as_of), "고가"])
    if today_high <= threshold:
        return None  # 장중에도 돌파선을 못 넘음

    # 추격금지 판정은 체결가가 아니라 "오늘 시가"를 기준으로 한다 - 체결가를
    # 그대로 쓰면 갭상승이 아닌 보통 케이스(체결가=돌파선)에서 갭이 항상
    # 0이 돼 추격금지가 사실상 무력화된다. 시가 기준이면 갭상승 여부가
    # 정상적으로 반영되고, 라이브 시스템이 장중 현재가로 갭을 재는 것과
    # 방향이 맞다.
    #
    # 단, gap_atr<=0(갭상승이 아님)인 경우를 core.breakout_verdict의 "대기"로
    # 그대로 받아들이면 안 된다 - "대기"는 원래 "아직 안 넘었다"는 뜻인데,
    # 여기서는 today_high > threshold로 돌파 자체는 이미 확인했다. 시가가
    # 돌파선 아래(비갭상승, 장중에 넘은 경우)라 gap_atr<=0이 나온 것뿐이니
    # "대기"를 "갭상승이 아니라 추격 걱정이 없다"로 해석해 통과시킨다.
    # core.breakout_verdict()가 실제로 걸러야 하는 건 "추격금지"뿐이다.
    today_open = float(_TICKER_DF.loc[pd.Timestamp(as_of), "시가"])
    gap_atr = (today_open - threshold) / atr
    verdict = core.breakout_verdict(gap_atr)
    if verdict == "추격금지":
        return None

    base_price = today_open if today_open >= threshold else threshold
    return {"atr": atr, "base_price": base_price, "threshold": threshold, "gap_atr": gap_atr}


@dataclass
class Position:
    entry_date: date
    entry_price: float          # 체결가 (슬리피지 반영)
    entry_atr: int
    shares: int
    trailing_high: Optional[float] = None
    stop_loss: Optional[float] = None
    fake_breakout: Optional[bool] = None   # intraday 모드 전용: 당일 종가가 임계값 밑으로 되돌아갔는가


@dataclass
class Trade:
    ticker: str
    mode: str
    entry_date: date
    entry_price: float
    exit_date: date
    exit_price: float
    shares: int
    entry_atr: int
    exit_reason: str
    evaluate_r: Optional[float] = None   # evaluate_holding() 시점 기준 R배수 (교차검증용)
    fake_breakout: Optional[bool] = None

    @property
    def net_pnl(self) -> float:
        gross = (self.exit_price - self.entry_price) * self.shares
        cost = (self.entry_price + self.exit_price) * self.shares * COST_RATE
        return gross - cost

    @property
    def r_multiple(self) -> float:
        return (self.exit_price - self.entry_price) / (2 * self.entry_atr)


# ── 하루씩 전진하는 루프 ────────────────────────────────────

def simulate_ticker(ticker: str, start: date, end: date, *,
                     mode: str = "close",
                     risk: Optional[float] = None,
                     require_liquidity: bool = False,
                     ) -> tuple[list[Trade], Optional[Position]]:
    """종목 1개를 하루씩 전진하며 시뮬레이션한다.

    mode: "close"(종가 판정 다음날 시가 체결) | "intraday"(당일 고가 돌파 시
        즉시 체결). 청산(손절) 판정은 두 모드 공통 - 항상 close 방식이다.
    risk: None이면 core.RISK_PER_TRADE(현재 설정값). core.RISK_PER_TRADE는
        2026-08-24 11:25(KST) 커밋으로 1% -> 0.5%로 바뀌었으므로, 그 이전
        실거래를 재현하려면 명시적으로 0.01을 넘겨야 한다.
    require_liquidity: True면 20일 평균 거래대금이 기준(screener.py와 동일,
        10억) 미만인 날은 진입 신호 자체를 무시한다(그날 실제 스캔에서도
        빠졌을 종목이므로). 청산에는 적용하지 않는다 - 이미 산 종목은
        유동성이 떨어져도 계속 감시·청산해야 한다.

    반환: (청산까지 끝난 거래 목록, 기간 종료 시점 미청산 포지션 또는 None).
    """
    global _TICKER_DF, _TICKER_CODE
    _TICKER_DF = load_ticker_df(ticker)
    _TICKER_CODE = ticker
    risk = core.RISK_PER_TRADE if risk is None else risk

    original_fetch_ohlcv = core.fetch_ohlcv
    core.fetch_ohlcv = _cached_fetch_ohlcv

    trades: list[Trade] = []
    try:
        sim_days = _TICKER_DF.index[
            (_TICKER_DF.index >= pd.Timestamp(start))
            & (_TICKER_DF.index <= pd.Timestamp(end))
        ]
        if sim_days.empty:
            return [], None

        position: Optional[Position] = None
        pending_entry_atr: Optional[int] = None   # close 모드 전용(다음날 체결 대기)
        pending_exit = False
        pending_exit_reason = ""
        pending_exit_r: Optional[float] = None

        for ts in sim_days:
            today: date = ts.date()
            today_open = float(_TICKER_DF.loc[ts, "시가"])

            # 1) 청산 체결 - 두 모드 공통, 전날 손절 신호가 있었으면 오늘 시가로.
            if pending_exit and position is not None:
                fill_price = today_open * (1 - SLIPPAGE)
                trades.append(Trade(
                    ticker=ticker, mode=mode,
                    entry_date=position.entry_date, entry_price=position.entry_price,
                    exit_date=today, exit_price=fill_price, shares=position.shares,
                    entry_atr=position.entry_atr, exit_reason=pending_exit_reason,
                    evaluate_r=pending_exit_r, fake_breakout=position.fake_breakout,
                ))
                position = None
                pending_exit = False
                pending_exit_r = None

            # 1-b) close 모드 진입 체결 - 어제 신호를 오늘 시가로.
            if mode == "close" and pending_entry_atr is not None and position is None:
                fill_price = today_open * (1 + SLIPPAGE)
                shares = core.calc_position(pending_entry_atr, STARTING_CAPITAL, risk).unit_shares
                if shares > 0:
                    position = Position(entry_date=today, entry_price=fill_price,
                                        entry_atr=pending_entry_atr, shares=shares)
                pending_entry_atr = None

            # 2) 신호 판정.
            if position is None:
                if require_liquidity and not bool(_TICKER_DF.loc[ts, "유동성충족"]):
                    pass  # 유동성 미달 - 오늘은 신호를 보지 않는다
                elif mode == "close":
                    sig = _entry_signal_close(today)
                    if sig is not None:
                        pending_entry_atr = sig["atr"]
                else:  # intraday - 당일 즉시 체결
                    sig = _entry_signal_intraday(today)
                    if sig is not None:
                        fill_price = sig["base_price"] * (1 + SLIPPAGE)
                        shares = core.calc_position(sig["atr"], STARTING_CAPITAL, risk).unit_shares
                        if shares > 0:
                            today_close = float(_TICKER_DF.loc[ts, "종가"])
                            position = Position(
                                entry_date=today, entry_price=fill_price,
                                entry_atr=sig["atr"], shares=shares,
                                fake_breakout=today_close < sig["threshold"],
                            )
            else:
                inp = core.HoldingInput(
                    ticker=ticker, name=ticker, market="",
                    buy_price=position.entry_price, shares=position.shares,
                    prev_trailing_high=position.trailing_high,
                    prev_stop_loss=position.stop_loss,
                    entry_atr=position.entry_atr,
                    last_buy_price=position.entry_price,
                )
                result = core.evaluate_holding(inp, STARTING_CAPITAL, as_of=today)
                position.trailing_high = result.trailing_high
                position.stop_loss = result.stop_loss
                # "손절"·"추세청산" 둘 다 core가 정확히 판정해서 돌려주는 청산
                # 신호다 - 손절만 보고 추세청산을 누락하면 실제 청산 시점보다
                # 늦게(또는 아예 안) 청산돼 실운용을 재현하지 못한다.
                if result.verdict in ("손절", "추세청산"):
                    pending_exit = True
                    pending_exit_reason = result.verdict_memo
                    pending_exit_r = (
                        (result.current_price - position.entry_price)
                        / (2 * position.entry_atr)
                    )

    finally:
        core.fetch_ohlcv = original_fetch_ohlcv

    return trades, position


def run_backtest(ticker: str, start: date, end: date,
                  risk: Optional[float] = None,
                  ) -> tuple[list[Trade], Optional[Position]]:
    """1단계 호환용 얇은 래퍼 - close 모드, 유동성 필터 없음."""
    return simulate_ticker(ticker, start, end, mode="close", risk=risk,
                            require_liquidity=False)


# ── 1단계: 단일/복수 종목 콘솔 출력 ─────────────────────────

def print_trades(ticker: str, trades: list[Trade],
                  open_position: Optional[Position]) -> None:
    if not trades and open_position is None:
        print(f"[{ticker}] 거래 없음 (기간 안에 진입 신호가 없었음)")
        return

    if trades:
        header = ("진입일       | 진입가   | 청산일       | 청산가   |  수량 |"
                  "     순손익 | R배수 | 검증R | 사유")
        print(f"[{ticker}]")
        print(header)
        print("-" * len(header))
        total_pnl = 0.0
        for t in trades:
            total_pnl += t.net_pnl
            eval_r = t.evaluate_r if t.evaluate_r is not None else float("nan")
            print(f"{t.entry_date} | {t.entry_price:>8,.0f} | {t.exit_date} | "
                  f"{t.exit_price:>8,.0f} | {t.shares:>5} | {t.net_pnl:>10,.0f} | "
                  f"{t.r_multiple:>5.2f} | {eval_r:>5.2f} | {t.exit_reason}")
        print(f"총 {len(trades)}건 · 순손익 합계 {total_pnl:,.0f}원 "
              f"(슬리피지·비용 반영, 복리 미반영)")

    if open_position is not None:
        p = open_position
        print(f"[{ticker}] (참고) 기간 종료 시점까지 포지션 보유 중(미청산, 위 집계에 "
              f"미포함): {p.shares}주 @ {p.entry_price:,.0f} (진입 {p.entry_date}, "
              f"진입시ATR {p.entry_atr})")


# ── 2단계: 지표 계산 ────────────────────────────────────────

def compute_metrics(trades: list[Trade]) -> dict:
    """거래 목록(여러 종목 섞여도 됨) 하나에 대한 요약 지표.

    체결(=청산) 시각순으로 정렬해 누적손익곡선을 만든다 - 계좌가 고정
    1000만원이라 종목 간 자금 경쟁이 없으므로, 청산일 순으로 실현손익만
    이어 붙이는 것으로 충분하다(포트폴리오 동시보유 자금배분은 범위 밖).
    """
    n = len(trades)
    if n == 0:
        return {"거래수": 0, "승률": float("nan"), "평균R": float("nan"),
                "총손익": 0.0, "MDD": 0.0, "최장연속손실": 0, "수익집중도": float("nan")}

    ordered = sorted(trades, key=lambda t: t.exit_date)
    pnls = [t.net_pnl for t in ordered]
    rs = [t.r_multiple for t in ordered]

    wins = sum(1 for p in pnls if p > 0)
    win_rate = wins / n * 100
    avg_r = sum(rs) / n
    total_pnl = sum(pnls)

    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        max_dd = min(max_dd, cum - peak)

    max_streak = streak = 0
    for p in pnls:
        if p < 0:
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0

    # 수익집중도: 전체 순손익(음수/0 근처면 분모가 깨져 해석 불가능한 값이
    # 나온다) 대신, 이익 거래만 모아서 "상위 5% 거래의 이익 합계 ÷ 전체
    # 이익 합계"로 낸다 - 분모가 항상 양수라 해석 가능하다.
    profits = [p for p in pnls if p > 0]
    if profits:
        top_n = max(1, round(len(profits) * 0.05))
        top_sum = sum(sorted(profits, reverse=True)[:top_n])
        concentration = top_sum / sum(profits) * 100
    else:
        top_n = 0
        concentration = float("nan")

    return {
        "거래수": n, "승률": win_rate, "평균R": avg_r, "총손익": total_pnl,
        "MDD": abs(max_dd), "최장연속손실": max_streak, "수익집중도": concentration,
        "상위N": top_n,
    }


def print_metrics_table(metrics_by_mode: dict[str, dict]) -> None:
    labels = ["거래수", "승률(%)", "평균R", "총손익(원)", "MDD(원)",
              "최장연속손실", "이익집중도(%,이익거래상위5%)"]
    keys = ["거래수", "승률", "평균R", "총손익", "MDD", "최장연속손실", "수익집중도"]
    modes = list(metrics_by_mode.keys())

    col_w = 16
    print(f"{'지표':<18}" + "".join(f"{m:>{col_w}}" for m in modes))
    print("-" * (18 + col_w * len(modes)))
    for label, key in zip(labels, keys):
        row = f"{label:<18}"
        for m in modes:
            v = metrics_by_mode[m].get(key, float("nan"))
            if key in ("거래수", "최장연속손실"):
                row += f"{v:>{col_w}}"
            elif key == "총손익" or key == "MDD":
                row += f"{v:>{col_w},.0f}"
            else:
                row += f"{v:>{col_w}.2f}"
        print(row)


# ── 2단계: 전종목 두 모드 비교 ────────────────────────────────

def run_universe_compare(tickers: list[str], start: date, end: date,
                          risk: Optional[float] = None,
                          out_csv: Optional[Path] = None) -> None:
    all_trades: dict[str, list[Trade]] = {"close": [], "intraday": []}
    n = len(tickers)
    for i, ticker in enumerate(tickers, 1):
        if i % 100 == 0 or i == n:
            logger.warning("진행률 %d/%d (%s)", i, n, ticker)
        for mode in ("close", "intraday"):
            try:
                trades, _ = simulate_ticker(
                    ticker, start, end, mode=mode, risk=risk, require_liquidity=True,
                )
            except Exception as e:                  # noqa: BLE001
                logger.warning("[%s/%s] 시뮬레이션 실패 - 건너뜀: %s", ticker, mode, e)
                continue
            all_trades[mode].extend(trades)

    print(f"\n=== 진입 모드 비교: 종목 {n}개 · {start} ~ {end} ===")
    print(f"슬리피지 {SLIPPAGE*100:.1f}% · 매매비용 {COST_RATE*100:.2f}% · "
          f"계좌 {STARTING_CAPITAL:,.0f}원 · 유동성 20일평균거래대금 "
          f"{MIN_TURNOVER/1e8:.0f}억 이상\n")

    metrics = {m: compute_metrics(all_trades[m]) for m in ("close", "intraday")}
    print_metrics_table(metrics)

    # ── 가짜 돌파 분석 (intraday 모드 전용) ──
    intraday_trades = all_trades["intraday"]
    fake = [t for t in intraday_trades if t.fake_breakout]
    real = [t for t in intraday_trades if t.fake_breakout is False]
    print(f"\n=== 가짜 돌파 분석 (intraday 모드, 총 {len(intraday_trades)}건 중) ===")
    if intraday_trades:
        fake_rate = len(fake) / len(intraday_trades) * 100
        print(f"진입일 종가가 돌파선 아래로 되돌아간 거래(가짜 돌파): "
              f"{len(fake)}건 ({fake_rate:.1f}%)")
        fake_m = compute_metrics(fake)
        real_m = compute_metrics(real)
        print(f"  가짜 돌파만: 승률 {fake_m['승률']:.1f}% · 평균R {fake_m['평균R']:.2f} · "
              f"총손익 {fake_m['총손익']:,.0f}원")
        print(f"  나머지(진짜 돌파): 승률 {real_m['승률']:.1f}% · "
              f"평균R {real_m['평균R']:.2f} · 총손익 {real_m['총손익']:,.0f}원")
        if metrics["intraday"]["총손익"] != 0:
            drag = fake_m["총손익"] / metrics["intraday"]["총손익"] * 100
            print(f"  가짜 돌파 거래가 intraday 모드 총손익에서 차지하는 비중: "
                  f"{drag:.1f}%p")
    else:
        print("intraday 모드 거래 없음")

    # ── 근사의 한계 안내 ──
    print("\n[주의] intraday 모드의 추격금지는 '당일 시가' 기준으로 판정한다. "
          "갭상승 케이스(시가가 돌파선 위)에서는 갭이 실제로 크게 나와 정상 작동하지만, "
          "갭상승이 아닌 보통 케이스(장중에 돌파선을 넘김)는 시가가 돌파선 아래라 "
          "gap_atr<=0이 나오고, 이건 core.breakout_verdict()가 '대기'로 분류하는 값이라 "
          "'갭상승이 아니라 추격 걱정 없음'으로 해석해 통과시킨다 - 즉 이 경우 추격금지는 "
          "구조적으로 걸리지 않는다(당일 고가로 이미 돌파를 확인했으므로 '대기'로 막지는 "
          "않되, 시가 자체가 낮으니 추격 판정도 할 게 없다). 10분 폴링을 시가 하나로 "
          "근사한 데서 오는 한계다.")

    # ── CSV 저장 ──
    if out_csv is None:
        out_csv = DATA_DIR / "backtest_compare_trades.csv"
    rows = []
    for mode in ("close", "intraday"):
        for t in all_trades[mode]:
            rows.append({
                "종목코드": t.ticker, "모드": t.mode,
                "진입일": t.entry_date, "진입가": t.entry_price,
                "청산일": t.exit_date, "청산가": t.exit_price,
                "수량": t.shares, "진입시ATR": t.entry_atr,
                "순손익": t.net_pnl, "R배수": t.r_multiple,
                "청산사유": t.exit_reason, "가짜돌파": t.fake_breakout,
            })
    pd.DataFrame(rows).to_csv(out_csv, index=False, encoding="utf-8-sig")
    print(f"\n거래 단위 상세 저장: {out_csv} ({len(rows)}행)")


# ── 3단계: 실제 운용 규칙 재현 (피라미딩 + 상관군 캡 + 자금 게이트 + 복리) ──
#
# 1·2단계는 "판정이 맞는가"·"진입 타이밍이 맞는가"를 독립적으로 검증하는
# 게 목적이라 종목마다 별개 계좌(무제한 자본)로 돌렸다. 이번 단계는 반대로
# "전략 자체가 되는가"를 보는 게 목적이라 계좌 하나를 전종목이 공유한다 -
# 현금이 없으면 신호가 있어도 못 사고, 상관군 캡에 걸리면 순위가 밀린
# 후보는 그날 못 들어간다. kis_client.py의 select_buy_candidates()·
# run_auto_trade()가 실제로 하는 일을 그대로 백테스트로 옮긴 것이다.

MAX_STOCK_PRICE = 150_000      # 주가 상한 - kis_client.py와 동일
MAX_UNIT_RATIO = 0.20          # 유닛금액이 계좌평가액의 이 비율 넘으면 제외
MAX_DAILY_ENTRIES = 3          # 일일 신규 진입(1유닛) 상한 - 추가매수는 별도(제한 없음)
SECTOR_REFRESH_DAYS = 30       # 업종 맵 갱신 주기(달력일). 실운영은 7일이지만 과거
                                # 시점 업종 맵 1회 조회에 KRX 로그인 후 약 60~70초가
                                # 걸려(3·4단계 조사에서 실측), 7일 주기로는 3년·전종목
                                # 백테스트가 비현실적으로 오래 걸린다. 업종 구성 자체가
                                # 한 달 안에 크게 안 바뀐다는 전제로 완화한 근사다.

REJECTION_REASONS = ("가격상한", "유닛금액", "상관군캡", "현금부족", "우선순위밀림", "국면필터",
                     "코스피200외")

DEBUG_TICKER = os.environ.get("BT_DEBUG_TICKER")  # 특정 트레이드 검증용 - 매일 상태를 콘솔에 찍는다

INDEX_REGIME_PATHS = {"KOSPI": DATA_DIR / "index_regime_KOSPI.csv",
                       "KOSDAQ": DATA_DIR / "index_regime_KOSDAQ.csv"}
TICKER_MARKET_PATH = DATA_DIR / "ticker_market.csv"
KOSPI200_HISTORY_PATH = DATA_DIR / "kospi200_history.csv"


def load_kospi200_pit() -> list[tuple[date, set]]:
    """(스냅샷일, 편입종목 set)을 날짜 오름차순으로. kospi200_history.csv는 pykrx
    get_index_portfolio_deposit_file('1028', date)로 미리 뽑아둔 반기별 스냅샷이다
    (KRX 로그인 필요, .env의 KRX_ID/KRX_PW 사용) - 이 함수는 읽기만 한다.
    시점별 편입이므로 생존편향이 없다."""
    df = pd.read_csv(KOSPI200_HISTORY_PATH, dtype={"티커": str})
    df["기준일"] = pd.to_datetime(df["기준일"]).dt.date
    return sorted(((d, set(g["티커"])) for d, g in df.groupby("기준일")),
                  key=lambda x: x[0])


def _k200_members_asof(pit: list, d: date) -> set:
    """d 시점 유효한 코스피200 편입 종목 = d 이하 최근 스냅샷. d가 첫 스냅샷보다
    이르면 첫 스냅샷을 쓴다(dev 시작 직전 리밸런스 근사)."""
    chosen = pit[0][1]
    for sd, members in pit:
        if sd <= d:
            chosen = members
        else:
            break
    return chosen


def load_regime_gate() -> tuple[dict[str, dict], dict[str, str]]:
    """지수 60일선 국면 필터에 쓸 (시장별 날짜->상승국면bool, 티커->시장) 반환.

    index_regime_*.csv/ticker_market.csv는 pykrx 지수 OHLCV·전종목 메타에서
    미리 뽑아둔 정적 스냅샷이다(생성 방법은 이번 세션 국면 분석 참고) -
    이 함수는 그 결과를 읽기만 한다.
    """
    regime_by_market: dict[str, dict] = {}
    for market, path in INDEX_REGIME_PATHS.items():
        df = pd.read_csv(path)
        df["날짜"] = pd.to_datetime(df["날짜"]).dt.date
        regime_by_market[market] = dict(zip(df["날짜"], df["상승국면"]))
    tm = pd.read_csv(TICKER_MARKET_PATH, dtype={"티커": str})
    ticker_market = dict(zip(tm["티커"], tm["시장"]))
    return regime_by_market, ticker_market


@dataclass
class PortfolioUnit:
    unit: int
    shares: int
    buy_price: float       # 체결가(슬리피지 반영, 매수 수수료는 별도로 현금에서 차감)
    fill_date: date


@dataclass
class PortfolioPosition:
    ticker: str
    sector: str
    entry_atr: int
    unit_shares: int
    pyramid_plan: list                      # core.build_pyramid() 결과, 진입 시점에 고정
    units: list = None
    trailing_high: Optional[float] = None
    stop_loss: Optional[float] = None
    total_invested: float = 0.0             # 매수원가+매수수수료 누적 (실제로 나간 현금)
    entry_gap_atr: float = 0.0              # 1유닛 진입 신호일의 gap_atr (진단용, 고정)
    breakeven_engaged: bool = False         # breakeven_ratchet 모드에서 본전 하한이 한 번이라도 core 자체 손절선보다 높았던 적이 있는지
    exit_verdict: str = ""                  # "손절"|"추세청산"|"본전하한" - 청산 트리거 시점에 채워짐

    def __post_init__(self):
        if self.units is None:
            self.units = []

    @property
    def total_shares(self) -> int:
        return sum(u.shares for u in self.units)

    @property
    def num_units(self) -> int:
        return len(self.units)

    @property
    def last_buy_price(self) -> float:
        return self.units[-1].buy_price

    @property
    def entry_date(self) -> date:
        return self.units[0].fill_date


@dataclass
class PortfolioTrade:
    ticker: str
    entry_date: date
    exit_date: date
    final_units: int
    total_shares: int
    total_invested: float
    proceeds: float
    entry_atr: int
    unit_shares: int
    exit_reason: str
    entry_gap_atr: float = 0.0              # 1유닛 진입 신호일의 gap_atr (진단용)
    exit_verdict: str = ""                  # "손절"|"추세청산"|"본전하한" (core.evaluate_holding 판정 그대로)

    @property
    def net_pnl(self) -> float:
        return self.proceeds - self.total_invested

    @property
    def r_multiple(self) -> float:
        risk_1r = self.unit_shares * 2 * self.entry_atr
        return self.net_pnl / risk_1r if risk_1r else float("nan")


# ── 다종목 core.fetch_ohlcv 바꿔치기 ─────────────────────────

_ALL_DFS: dict[str, pd.DataFrame] = {}


def _prepare_all_ticker_dfs(tickers: list[str]) -> None:
    global _ALL_DFS
    _ALL_DFS = {t: load_ticker_df(t) for t in tickers}


def _cached_fetch_ohlcv_multi(code: str, days: int = core.FETCH_DAYS,
                               end: Optional[date] = None) -> pd.DataFrame:
    if end is None:
        raise ValueError("포트폴리오 백테스트에서는 end(as_of) 없는 조회를 허용하지 않는다")
    _guard_period_end(end, "_cached_fetch_ohlcv_multi")
    df_all = _ALL_DFS.get(code)
    if df_all is None:
        raise ValueError(f"[{code}] 준비된 데이터 없음")
    end_ts = pd.Timestamp(end)
    df = df_all[df_all.index <= end_ts]
    if df.empty:
        raise ValueError(f"[{code}] {end} 이전 데이터 없음")
    df = df[["시가", "고가", "저가", "종가", "거래량"]].tail(days)
    if len(df) < core.MIN_TRADING_DAYS:
        raise ValueError(f"[{code}] 거래일 부족: {len(df)}일 < 최소 {core.MIN_TRADING_DAYS}일")
    return df


def _entry_signal_for(ticker: str, as_of: date) -> Optional[dict]:
    """close 모드 진입 신호 - _entry_signal_close와 같은 계산, 다종목용."""
    try:
        df = _cached_fetch_ohlcv_multi(ticker, end=as_of)
        sig = core.trend_signals(df, high_period=_ENTRY_HIGH_PERIOD)
    except ValueError:
        return None
    atr = core.round_atr(core.calc_atr(df))
    if atr <= 0:
        return None
    gap_atr = (sig.current_price - sig.high_20_prev) / atr
    verdict = core.breakout_verdict(gap_atr)
    if verdict != "진입가능":
        return None
    if not _passes_vol_mult(df):
        return None
    return {"ticker": ticker, "atr": atr, "price": sig.current_price, "gap_atr": gap_atr}


def _day_open(ticker: str, ts: pd.Timestamp) -> Optional[float]:
    df = _ALL_DFS[ticker]
    if ts not in df.index:
        return None
    return float(df.loc[ts, "시가"])


def _day_close(ticker: str, ts: pd.Timestamp) -> Optional[float]:
    df = _ALL_DFS[ticker]
    if ts not in df.index:
        return None
    return float(df.loc[ts, "종가"])


def run_portfolio_backtest(tickers: list[str], start: date, end: date,
                            risk: float = core.RISK_PER_TRADE,
                            starting_capital: float = STARTING_CAPITAL,
                            unlimited_cash: bool = False,
                            fixed_account_size: Optional[float] = None,
                            stop_atr_mult: Optional[float] = None,
                            breakeven_ratchet: bool = False,
                            regime_gate: bool = False,
                            unit_caps: Optional[tuple[int, int, int]] = None,
                            breakout_period: Optional[int] = None,
                            kospi200: bool = False,
                            vol_mult_min: Optional[float] = None,
                            ) -> dict:
    """계좌 하나를 전종목이 공유하는 포트폴리오 시뮬레이션.

    unlimited_cash=True면 현금부족으로 진입·추가매수를 막지 않는다(현금은
    계속 추적은 하되 음수가 될 수 있다 - 순수하게 "이 룰셋이 신호를 다
    받았을 때 기대값이 어떤가"만 보기 위한 것). fixed_account_size를 주면
    유닛 크기·자금 게이트 계산에 매일 변하는 시가평가 대신 이 고정값을
    쓴다(복리 효과 자체를 차단) - 두 옵션을 같이 켜면 "계좌 제약이 결과를
    얼마나 왜곡했는지"를 압축 버전과 비교할 수 있다.

    CLI --capital-mode fixed가 바로 이 두 옵션을 같이 켠 것이다
    (unlimited_cash=True + fixed_account_size=starting_capital). dev 구간
    파라미터 비교는 이 모드로, holdout 최종 검증은 둘 다 끈 compound로 돌린다.

    stop_atr_mult: None이면 core.STOP_ATR_MULT(기본 2.0) 그대로. 값을 주면
        실행 동안만 core.STOP_ATR_MULT를 그 값으로 바꿔치기한다(core.py
        소스는 그대로 - calc_stop/resolve_stop이 이 이름을 호출 시점에
        전역에서 읽으므로 몽키패치가 그대로 반영된다. core.fetch_ohlcv를
        바꿔치기하는 것과 같은 방식). 실행이 끝나면 원래 값으로 되돌린다.

    breakeven_ratchet: True면, core.evaluate_holding()이 계산한 손절선과
        별개로 "진입후최고가가 진입가 대비 +1×진입시ATR 이상 오른 적이
        있으면 손절선은 최소 진입가(본전)"라는 층을 backtest.py에서만
        추가로 씌운다. core의 트레일링(half 방식, 상승분의 절반만 따라감)은
        건드리지 않고 그 위에 별도 하한선을 얹는 것 - 4×ATR 올라야 본전에
        닿는 core 방식보다 훨씬 이르게(+1×ATR) 본전 손절을 강제한다.

    regime_gate: True면 신규 진입(1유닛)만 막는다 - 진입 후보 종목이 속한
        시장(코스피/코스닥)의 지수가 그날 60일 이동평균 아래(하락/횡보
        국면)면 그 후보를 "국면필터"로 제외한다. 이미 보유 중인 종목의
        피라미딩 추가매수는 막지 않는다(사용자 지시: "신규 진입 금지").
        load_regime_gate()가 읽는 data/index_regime_*.csv·ticker_market.csv는
        pykrx에서 미리 뽑아둔 정적 스냅샷.

    unit_caps: None이면 core 기본값(종목당 4 / 업종당 6 / 전체 12) 그대로.
        (stock, group, total) 튜플을 주면 실행 동안만 core.MAX_UNITS·
        MAX_UNITS_GROUP·MAX_UNITS_TOTAL을 그 값으로 바꿔치기한다(stop_atr_mult
        와 같은 방식 - core 소스는 그대로, 호출 시점 전역 조회라 반영됨).
        "캡 없음" 실험은 아주 큰 값(예: 10**9)을 넘긴다. 실행이 끝나면 복원.

    breakout_period: None이면 core.HIGH_PERIOD(20) 그대로. 값을 주면 실행 동안만
        진입 돌파 판정 기간을 그 값으로 바꾼다(_ENTRY_HIGH_PERIOD 전역을 통해
        core.trend_signals(df, high_period=...)로 전달). ATR 기간·추세청산(10일
        저가)은 그대로 - 변수 하나만 바꾸는 실험용. 실행이 끝나면 복원.

    vol_mult_min: None/1.0이면 거래량 필터 없음. 값을 주면 실행 동안만
        _ENTRY_VOL_MULT_MIN 전역을 그 값으로 바꿔 "신호일 거래량 / 직전 20일
        평균 ≥ 이 값"인 후보만 진입 신호로 통과시킨다(_passes_vol_mult). 실행이
        끝나면 복원. 신호일 거래량은 스캔 시점(종가 후)에 확정된 값이라 미래참조 아님.

    kospi200: True면 신규 진입을 진입일 시점 코스피200 편입 종목으로 제한한다
        (data/kospi200_history.csv 반기별 스냅샷 기준, 시점별이라 생존편향 없음).
        이미 보유 중인 종목이 지수에서 빠져도 피라미딩은 계속 허용한다. 제한에
        걸린 후보는 "코스피200외"로 카운트한다.

    반환 dict 키: trades(list[PortfolioTrade]), rejections(dict),
    equity_curve(list[(date, 평가금액)]), final_cash, open_positions_value,
    final_value, open_positions(dict[ticker, PortfolioPosition]).
    """
    _prepare_all_ticker_dfs(tickers)
    original_fetch_ohlcv = core.fetch_ohlcv
    core.fetch_ohlcv = _cached_fetch_ohlcv_multi
    original_stop_mult = core.STOP_ATR_MULT
    if stop_atr_mult is not None:
        core.STOP_ATR_MULT = stop_atr_mult
    original_caps = (core.MAX_UNITS, core.MAX_UNITS_GROUP, core.MAX_UNITS_TOTAL)
    if unit_caps is not None:
        core.MAX_UNITS, core.MAX_UNITS_GROUP, core.MAX_UNITS_TOTAL = unit_caps
    global _ENTRY_HIGH_PERIOD, _ENTRY_VOL_MULT_MIN
    original_high_period = _ENTRY_HIGH_PERIOD
    if breakout_period is not None:
        _ENTRY_HIGH_PERIOD = breakout_period
    original_vol_mult = _ENTRY_VOL_MULT_MIN
    if vol_mult_min is not None:
        _ENTRY_VOL_MULT_MIN = vol_mult_min

    regime_by_market, ticker_market = load_regime_gate() if regime_gate else ({}, {})
    k200_pit = load_kospi200_pit() if kospi200 else None

    rejections = {r: 0 for r in REJECTION_REASONS}
    trades: list[PortfolioTrade] = []
    positions: dict[str, PortfolioPosition] = {}
    equity_curve: list[tuple] = []

    pending_exits: dict[str, str] = {}
    pending_entries: dict[str, dict] = {}
    pending_adds: dict[str, dict] = {}

    sector_map: dict[str, str] = {}
    sector_map_asof: Optional[date] = None

    cash = starting_capital

    calendar_ticker = "005930" if "005930" in _ALL_DFS else tickers[0]
    master_days = _ALL_DFS[calendar_ticker].index
    sim_days = master_days[(master_days >= pd.Timestamp(start)) & (master_days <= pd.Timestamp(end))]

    try:
        for i, ts in enumerate(sim_days):
            today = ts.date()
            if i % 100 == 0:
                logger.warning("진행률 %d/%d (%s) · 계좌 %.0f원 · 보유 %d종목",
                               i, len(sim_days), today, cash, len(positions))

            # 1) 어제 확정된 청산·진입·추가매수를 오늘 시가로 체결.
            for ticker, reason in pending_exits.items():
                pos = positions.pop(ticker, None)
                if pos is None:
                    continue
                op = _day_open(ticker, ts)
                if op is None:
                    continue  # 거래정지 등 - 단순화를 위해 재시도하지 않고 포기
                fill = op * (1 - SLIPPAGE)
                proceeds = fill * pos.total_shares * (1 - COST_RATE)
                cash += proceeds
                trades.append(PortfolioTrade(
                    ticker=ticker, entry_date=pos.entry_date, exit_date=today,
                    final_units=pos.num_units, total_shares=pos.total_shares,
                    total_invested=pos.total_invested, proceeds=proceeds,
                    entry_atr=pos.entry_atr, unit_shares=pos.unit_shares,
                    exit_reason=reason, entry_gap_atr=pos.entry_gap_atr,
                    exit_verdict=pos.exit_verdict,
                ))
                if ticker == DEBUG_TICKER:
                    print(f"[DEBUG {ticker}] {today} 청산 체결 fill={fill:.1f} "
                          f"units={pos.num_units} proceeds={proceeds:.0f}")
            pending_exits.clear()

            for ticker, info in pending_entries.items():
                if ticker in positions:
                    continue
                op = _day_open(ticker, ts)
                if op is None:
                    continue
                fill = op * (1 + SLIPPAGE)
                total_needed = fill * info["shares"] * (1 + COST_RATE)
                if not unlimited_cash and cash < total_needed:
                    rejections["현금부족"] += 1
                    continue
                cash -= total_needed
                plan = core.build_pyramid(fill, info["atr"], info["shares"],
                                          capital=info["account_size"], max_units=core.MAX_UNITS)
                pos = PortfolioPosition(
                    ticker=ticker, sector=info["sector"], entry_atr=info["atr"],
                    unit_shares=info["shares"], pyramid_plan=plan,
                    total_invested=total_needed, entry_gap_atr=info["gap_atr"],
                )
                pos.units.append(PortfolioUnit(unit=1, shares=info["shares"],
                                               buy_price=fill, fill_date=today))
                positions[ticker] = pos
                if ticker == DEBUG_TICKER:
                    print(f"[DEBUG {ticker}] {today} 진입 체결 1유닛 fill={fill:.1f} "
                          f"atr={info['atr']} shares={info['shares']}")
            pending_entries.clear()

            for ticker in pending_adds:
                pos = positions.get(ticker)
                if pos is None or pos.num_units >= core.MAX_UNITS:
                    continue
                op = _day_open(ticker, ts)
                if op is None:
                    continue
                fill = op * (1 + SLIPPAGE)
                total_needed = fill * pos.unit_shares * (1 + COST_RATE)
                if not unlimited_cash and cash < total_needed:
                    rejections["현금부족"] += 1
                    continue
                cash -= total_needed
                pos.total_invested += total_needed
                pos.units.append(PortfolioUnit(unit=pos.num_units + 1, shares=pos.unit_shares,
                                               buy_price=fill, fill_date=today))
                if ticker == DEBUG_TICKER:
                    print(f"[DEBUG {ticker}] {today} 추가매수 체결 -> {pos.num_units}유닛 fill={fill:.1f}")
            pending_adds.clear()

            # 2) 업종 맵 - 달마다 한 번만 갱신 (사유는 클래스 상단 주석 참고).
            if sector_map_asof is None or (today - sector_map_asof).days >= SECTOR_REFRESH_DAYS:
                try:
                    sector_map = screener.load_sector_map(today.strftime("%Y%m%d"))
                except Exception as e:                  # noqa: BLE001
                    logger.warning("업종 맵 갱신 실패(%s) - 이전 맵 유지: %s", today, e)
                sector_map_asof = today

            # 3) 계좌평가액 = 현금 + 보유종목 시가평가(오늘 종가 기준).
            #    equity_curve는 항상 실제 시가평가를 기록한다 - fixed_account_size는
            #    유닛 크기·게이트 계산에만 쓰고, 성과 곡선 자체는 왜곡하지 않는다.
            mtm_equity = cash
            for ticker, pos in positions.items():
                c = _day_close(ticker, ts)
                if c is not None:
                    mtm_equity += c * pos.total_shares
            equity_curve.append((today, mtm_equity))
            account_size = fixed_account_size if fixed_account_size is not None else mtm_equity

            # 4) 보유종목 판정 - 청산(evaluate_holding) 또는 피라미딩 트리거.
            for ticker, pos in list(positions.items()):
                c = _day_close(ticker, ts)
                if c is None:
                    continue
                inp = core.HoldingInput(
                    ticker=ticker, name=ticker, market="",
                    buy_price=pos.last_buy_price, shares=pos.total_shares,
                    prev_trailing_high=pos.trailing_high, prev_stop_loss=pos.stop_loss,
                    entry_atr=pos.entry_atr, last_buy_price=pos.last_buy_price,
                )
                result = core.evaluate_holding(inp, account_size, as_of=today)
                pos.trailing_high = result.trailing_high
                new_stop = result.stop_loss
                breakeven_exit = False
                if breakeven_ratchet:
                    entry_price = pos.units[0].buy_price
                    if result.trailing_high - entry_price >= pos.entry_atr:
                        if entry_price > new_stop:
                            pos.breakeven_engaged = True
                        new_stop = max(new_stop, entry_price)
                    # breakeven_engaged는 core 판정과 별개로 backtest.py에서만 얹는 추가
                    # 하한선이라 core.evaluate_holding()의 verdict에 안 잡힌다 - 그래서
                    # 여기서만 별도로 c<=new_stop을 직접 비교한다(core 판정을 대체하는
                    # 게 아니라, core 판정 위에 얹은 추가 조건 하나를 더 보는 것).
                    if pos.breakeven_engaged and c <= new_stop:
                        breakeven_exit = True
                pos.stop_loss = new_stop
                if ticker == DEBUG_TICKER:
                    print(f"[DEBUG {ticker}] {today} close={c:.0f} units={pos.num_units} "
                          f"last_buy={pos.last_buy_price:.0f} trailing_high={pos.trailing_high:.0f} "
                          f"stop={new_stop} verdict={result.verdict}")
                # core.evaluate_holding()의 verdict를 그대로 청산 조건으로 쓴다 - "손절"·
                # "추세청산" 둘 다 core가 이미 정확히 판정해서 돌려주므로, 여기서 현재가와
                # 손절선을 다시 비교해 재판정하지 않는다(예전엔 "손절"만 보고 "추세청산"을
                # 누락했었다). breakeven_exit만 core 판정 바깥의 backtest.py 전용 추가 조건.
                if result.verdict in ("손절", "추세청산") or breakeven_exit:
                    if breakeven_exit and result.verdict not in ("손절", "추세청산"):
                        pos.exit_verdict = "본전하한"
                        pending_exits[ticker] = (
                            f"현재가 {c:,.0f} ≤ 손절선 {new_stop:,.0f} (본전 하한 적용됨)"
                        )
                    else:
                        pos.exit_verdict = result.verdict
                        pending_exits[ticker] = result.verdict_memo
                    if ticker == DEBUG_TICKER:
                        print(f"[DEBUG {ticker}] {today} 청산 트리거({pos.exit_verdict}) -> {pending_exits[ticker]}")
                    continue
                if pos.num_units < core.MAX_UNITS:
                    next_step = pos.pyramid_plan[pos.num_units]  # 0-idx: 다음 유닛 단계
                    if c >= next_step.buy_price:
                        if c > MAX_STOCK_PRICE:
                            rejections["가격상한"] += 1
                            if ticker == DEBUG_TICKER:
                                print(f"[DEBUG {ticker}] {today} 피라미딩 트리거했으나 가격상한 제외")
                        else:
                            unit_amount = pos.unit_shares * c
                            if unit_amount > account_size * MAX_UNIT_RATIO:
                                rejections["유닛금액"] += 1
                                if ticker == DEBUG_TICKER:
                                    print(f"[DEBUG {ticker}] {today} 피라미딩 트리거했으나 유닛금액 제외 "
                                          f"(unit_amount={unit_amount:.0f} > {account_size*MAX_UNIT_RATIO:.0f})")
                            else:
                                sector = sector_map.get(ticker, pos.sector)
                                group_units = sum(
                                    p.num_units for t2, p in positions.items()
                                    if sector and sector_map.get(t2, p.sector) == sector
                                )
                                total_units = sum(p.num_units for p in positions.values())
                                if (sector and group_units + 1 > core.MAX_UNITS_GROUP) or \
                                   (total_units + 1 > core.MAX_UNITS_TOTAL):
                                    rejections["상관군캡"] += 1
                                    if ticker == DEBUG_TICKER:
                                        print(f"[DEBUG {ticker}] {today} 피라미딩 트리거했으나 상관군캡 제외")
                                else:
                                    pending_adds[ticker] = {}
                                    if ticker == DEBUG_TICKER:
                                        print(f"[DEBUG {ticker}] {today} 피라미딩 트리거 accepted "
                                              f"close={c:.0f} >= {next_step.buy_price} (내일 시가체결 예약)")

            # 5) 신규 진입 후보 스캔 (미보유·유동성 충족 종목만) -> 갭 오름차순.
            k200_today = _k200_members_asof(k200_pit, today) if k200_pit is not None else None
            candidates = []
            for ticker in tickers:
                if ticker in positions:
                    continue
                df_all = _ALL_DFS[ticker]
                if ts not in df_all.index or not bool(df_all.loc[ts, "유동성충족"]):
                    continue
                sig = _entry_signal_for(ticker, today)
                if sig is not None:
                    sig["sector"] = sector_map.get(ticker, "")
                    candidates.append(sig)
            candidates = core.sort_by_gap(candidates)

            group_units_running: dict[str, int] = {}
            for t2, p in positions.items():
                s = sector_map.get(t2, p.sector)
                if s:
                    group_units_running[s] = group_units_running.get(s, 0) + p.num_units
            total_units_running = sum(p.num_units for p in positions.values())
            cash_running = cash
            entries_today = 0

            for c in candidates:
                ticker = c["ticker"]
                if k200_today is not None and ticker not in k200_today:
                    rejections["코스피200외"] += 1
                    continue
                if regime_gate:
                    market = ticker_market.get(ticker, "KOSDAQ")
                    market = market if market == "KOSPI" else "KOSDAQ"
                    is_up = regime_by_market.get(market, {}).get(today, False)
                    if not is_up:
                        rejections["국면필터"] += 1
                        continue
                if c["price"] > MAX_STOCK_PRICE:
                    rejections["가격상한"] += 1
                    continue
                shares = core.calc_position(c["atr"], account_size, risk).unit_shares
                if shares <= 0:
                    continue
                unit_amount = shares * c["price"]
                if unit_amount > account_size * MAX_UNIT_RATIO:
                    rejections["유닛금액"] += 1
                    continue
                sector = c["sector"]
                group_after = group_units_running.get(sector, 0) + 1
                total_after = total_units_running + 1
                if (sector and group_after > core.MAX_UNITS_GROUP) or \
                   (total_after > core.MAX_UNITS_TOTAL):
                    rejections["상관군캡"] += 1
                    continue
                if not unlimited_cash and unit_amount > cash_running:
                    rejections["현금부족"] += 1
                    continue
                if entries_today >= MAX_DAILY_ENTRIES:
                    rejections["우선순위밀림"] += 1
                    continue
                pending_entries[ticker] = {
                    "atr": c["atr"], "shares": shares, "account_size": account_size,
                    "sector": sector, "gap_atr": c["gap_atr"],
                }
                if sector:
                    group_units_running[sector] = group_units_running.get(sector, 0) + 1
                total_units_running += 1
                cash_running -= unit_amount
                entries_today += 1

    finally:
        core.fetch_ohlcv = original_fetch_ohlcv
        core.STOP_ATR_MULT = original_stop_mult
        core.MAX_UNITS, core.MAX_UNITS_GROUP, core.MAX_UNITS_TOTAL = original_caps
        _ENTRY_HIGH_PERIOD = original_high_period
        _ENTRY_VOL_MULT_MIN = original_vol_mult

    open_positions_value = 0.0
    for ticker, pos in positions.items():
        df_all = _ALL_DFS[ticker]
        hist = df_all[df_all.index <= pd.Timestamp(end)]
        if hist.empty:
            continue
        last_close = float(hist["종가"].iloc[-1])
        open_positions_value += last_close * pos.total_shares

    return {
        "trades": trades, "rejections": rejections, "equity_curve": equity_curve,
        "final_cash": cash, "open_positions_value": open_positions_value,
        "final_value": cash + open_positions_value, "open_positions": positions,
    }


def compute_portfolio_metrics(trades: list[PortfolioTrade],
                               equity_curve: list, starting_capital: float) -> dict:
    n = len(trades)
    pnls = [t.net_pnl for t in trades]
    rs = [t.r_multiple for t in trades]
    win_rate = (sum(1 for p in pnls if p > 0) / n * 100) if n else float("nan")
    avg_r = (sum(rs) / n) if n else float("nan")
    total_pnl = sum(pnls)

    peak = -float("inf")
    max_dd_amt = 0.0
    max_dd_pct = 0.0
    for _, eq in equity_curve:
        peak = max(peak, eq)
        dd = peak - eq
        max_dd_amt = max(max_dd_amt, dd)
        if peak > 0:
            max_dd_pct = max(max_dd_pct, dd / peak * 100)

    ordered = sorted(trades, key=lambda t: t.exit_date)
    max_streak = streak = 0
    for t in ordered:
        if t.net_pnl < 0:
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0

    profits = [p for p in pnls if p > 0]
    if profits:
        top_n = max(1, round(len(profits) * 0.05))
        concentration = sum(sorted(profits, reverse=True)[:top_n]) / sum(profits) * 100
    else:
        concentration = float("nan")

    if equity_curve:
        days_elapsed = (equity_curve[-1][0] - equity_curve[0][0]).days
        final_eq = equity_curve[-1][1]
        cagr = (((final_eq / starting_capital) ** (365 / days_elapsed) - 1) * 100
                if days_elapsed > 0 and final_eq > 0 else float("nan"))
    else:
        cagr = float("nan")

    by_units = {}
    for u in (1, 2, 3, 4):
        grp = [t for t in trades if t.final_units == u]
        if grp:
            by_units[u] = {"건수": len(grp),
                          "평균R": sum(t.r_multiple for t in grp) / len(grp),
                          "총손익": sum(t.net_pnl for t in grp)}
        else:
            by_units[u] = {"건수": 0, "평균R": float("nan"), "총손익": 0.0}

    return {
        "거래수": n, "승률": win_rate, "평균R": avg_r, "총손익": total_pnl,
        "MDD_금액": max_dd_amt, "MDD_비율": max_dd_pct, "최장연속손실": max_streak,
        "이익집중도": concentration, "연환산수익률": cagr, "유닛별": by_units,
    }


def print_portfolio_report(result: dict, start: date, end: date,
                            starting_capital: float, out_csv: Optional[Path],
                            capital_mode: str = "compound",
                            unit_caps: Optional[tuple[int, int, int]] = None,
                            breakout_period: Optional[int] = None,
                            market: str = "ALL") -> dict:
    trades = result["trades"]
    metrics = compute_portfolio_metrics(trades, result["equity_curve"], starting_capital)

    if capital_mode == "fixed":
        cap_desc = (f"자본모드 FIXED (유닛 크기 고정 {starting_capital:,.0f}원 기준 · "
                    f"현금부족 제외 없음 · 파라미터 비교용, 실계좌 아님)")
    else:
        cap_desc = "자본모드 COMPOUND (복리 · 현금 제약 반영 · 실제 운용 시뮬레이션)"

    if unit_caps is not None:
        s, g, t = unit_caps
        fmt = lambda v: "무제한" if v >= 10 ** 9 else str(v)
        caps_desc = f"유닛 캡: 종목당 {fmt(s)} / 업종당 {fmt(g)} / 전체 {fmt(t)}"
    else:
        caps_desc = (f"유닛 캡: 종목당 {core.MAX_UNITS} / 업종당 {core.MAX_UNITS_GROUP} "
                     f"/ 전체 {core.MAX_UNITS_TOTAL} (core 기본)")

    bp = breakout_period if breakout_period is not None else core.HIGH_PERIOD
    brk_desc = (f"진입 돌파 기간: {bp}일"
                + ("" if breakout_period is not None else " (core 기본)")
                + " · 추세청산 10일 저가 · ATR 20")

    print(f"\n=== 실운용 규칙 재현 백테스트: {start} ~ {end} ===")
    print(f"{cap_desc}")
    print(f"{caps_desc}")
    print(f"{brk_desc}")
    if market != "ALL":
        print(f"유니버스: {market}")
    print(f"계좌 시작 {starting_capital:,.0f}원 · 슬리피지 {SLIPPAGE*100:.1f}% · "
          f"매매비용 {COST_RATE*100:.2f}% · 리스크 {core.RISK_PER_TRADE*100:.1f}%\n")

    print(f"총 거래수            : {metrics['거래수']}건")
    print(f"승률                : {metrics['승률']:.2f}%")
    print(f"평균 R배수           : {metrics['평균R']:.2f}")
    print(f"총손익              : {metrics['총손익']:,.0f}원")
    if capital_mode == "fixed":
        print(f"최종 순자산(초기+누적손익): {result['final_value']:,.0f}원 "
              f"(미청산 평가 {result['open_positions_value']:,.0f}, "
              f"{len(result['open_positions'])}종목 보유중 · fixed 모드라 현금은 "
              f"음수 허용 회계값 {result['final_cash']:,.0f})")
    else:
        print(f"최종 계좌 잔고        : {result['final_value']:,.0f}원 "
              f"(현금 {result['final_cash']:,.0f} + 미청산 평가 {result['open_positions_value']:,.0f}, "
              f"{len(result['open_positions'])}종목 보유중)")
    print(f"연환산 수익률(CAGR)   : {metrics['연환산수익률']:.2f}%")
    print(f"MDD                : {metrics['MDD_금액']:,.0f}원 ({metrics['MDD_비율']:.2f}%)")
    print(f"최장 연속 손실        : {metrics['최장연속손실']}건")
    print(f"이익집중도(이익거래 상위5%): {metrics['이익집중도']:.2f}%")
    if capital_mode == "fixed":
        print("  ※ fixed 모드는 유닛을 고정 크기로 계속 잡으므로 순자산이 초기자본 밑으로 "
              "(음수까지) 내려갈 수 있다. 그 구간에서 CAGR·MDD비율은 의미가 깨진다 - "
              "fixed 모드 비교는 거래수·승률·평균R·총손익(원)·유닛별로 본다.")

    print("\n=== 유닛 단계별 성적 (몇 유닛까지 갔다가 손절됐는가) ===")
    print(f"{'단계':<8}{'건수':>8}{'평균R':>10}{'총손익':>16}")
    for u in (1, 2, 3, 4):
        row = metrics["유닛별"][u]
        print(f"{u}유닛{'':<4}{row['건수']:>8}{row['평균R']:>10.2f}{row['총손익']:>16,.0f}")

    print("\n=== 제외 사유별 건수 (신규 진입 + 추가매수 시도 합산) ===")
    for reason, count in result["rejections"].items():
        print(f"  {reason:<10}: {count:,}건")

    print("\n=== 청산 유형별 건수 (core.evaluate_holding 판정 그대로) ===")
    verdict_counts: dict[str, int] = {}
    for t in trades:
        verdict_counts[t.exit_verdict] = verdict_counts.get(t.exit_verdict, 0) + 1
    for v, cnt in sorted(verdict_counts.items(), key=lambda x: -x[1]):
        print(f"  {v or '(미기록)':<10}: {cnt:,}건")

    if out_csv is None:
        out_csv = DATA_DIR / "backtest_portfolio_trades.csv"
    rows = [{
        "종목코드": t.ticker, "진입일": t.entry_date, "청산일": t.exit_date,
        "최종유닛수": t.final_units, "총수량": t.total_shares,
        "매수원가(수수료포함)": t.total_invested, "매도대금(수수료차감후)": t.proceeds,
        "진입시ATR": t.entry_atr, "1유닛주수": t.unit_shares,
        "순손익": t.net_pnl, "R배수": t.r_multiple, "청산사유": t.exit_reason,
        "청산유형": t.exit_verdict, "진입시gap_atr": t.entry_gap_atr,
    } for t in trades]
    pd.DataFrame(rows).to_csv(out_csv, index=False, encoding="utf-8-sig")
    print(f"\n거래 단위 상세 저장: {out_csv} ({len(rows)}행)")

    eq_csv = out_csv.with_name(out_csv.stem + "_equity.csv")
    eq_rows = [{"날짜": d, "계좌평가액": v} for d, v in result["equity_curve"]]
    pd.DataFrame(eq_rows).to_csv(eq_csv, index=False, encoding="utf-8-sig")
    print(f"일별 계좌평가액 저장: {eq_csv} ({len(eq_rows)}행)")

    return metrics


def main() -> int:
    parser = argparse.ArgumentParser(description="백테스트 (1단계: 단일 종목 / 2단계: 모드 비교)")
    parser.add_argument("--ticker", default=DEFAULT_TICKER,
                        help="단일 종목 (기본 삼성전자). --tickers와 같이 쓰면 무시됨")
    parser.add_argument("--tickers", default=None,
                        help="쉼표로 구분한 복수 종목 (예: 006660,078930)")
    parser.add_argument("--period", choices=("dev", "holdout", "full"), default="dev",
                        help="실행 구간. dev(2019-09~2023-08, 기본): 파라미터 탐색·성적 확인 "
                             "전용. holdout(2023-09~2026-08): 손대지 않은 검증 구간 - 실행 시 "
                             "경고와 이력 기록. full: 전체(기준선 스냅샷 전용). dev/full에서는 "
                             "holdout 구간 데이터 로드가 예외로 막힌다")
    parser.add_argument("--start", default=None,
                        help="YYYYMMDD - 구간 안에서 더 좁힐 때만. 생략 시 --period 범위 전체")
    parser.add_argument("--end", default=None,
                        help="YYYYMMDD - 구간 안에서 더 좁힐 때만. 생략 시 --period 범위 전체")
    parser.add_argument("--risk", type=float, default=None,
                        help="1유닛 리스크 비율 (기본: core.RISK_PER_TRADE). 예: 0.01")
    parser.add_argument("--compare-modes", action="store_true",
                        help="전종목(유동성 필터 적용) close/intraday 모드 비교")
    parser.add_argument("--limit", type=int, default=None,
                        help="--compare-modes에서 앞 N종목만(스모크 테스트용)")
    parser.add_argument("--out", default=None, help="--compare-modes 결과 CSV 경로")
    parser.add_argument("--portfolio", action="store_true",
                        help="3단계: 계좌 하나 공유 + 피라미딩 + 상관군 캡 + 자금 게이트 + 복리")
    parser.add_argument("--capital-mode", choices=("fixed", "compound"), default=None,
                        help="--portfolio 전용 자본 모드. fixed: 유닛 크기를 항상 초기 자본"
                             "(--starting-capital) 기준으로 계산하고 현금부족 진입 제외를 "
                             "적용하지 않는다 - 모든 신호를 동일 조건에서 평가(파라미터 비교용). "
                             "compound: 복리 + 현금 제약 반영(실제 운용 시뮬레이션). "
                             "기본값은 --period dev면 fixed, holdout/full이면 compound. "
                             "피라미딩·상관군캡·일일상한은 두 모드 공통으로 그대로 적용된다")
    parser.add_argument("--unlimited-cash", action="store_true",
                        help="(구버전 별칭) --capital-mode fixed 와 동일")
    parser.add_argument("--starting-capital", type=float, default=STARTING_CAPITAL,
                        help="--portfolio 시작 계좌 (기본 10,000,000)")
    parser.add_argument("--stop-mult", type=float, default=None,
                        help="--portfolio 전용: 손절폭 ATR 배수 실험값 (기본: core.STOP_ATR_MULT=2.0). "
                             "실행 동안만 core.STOP_ATR_MULT를 바꿔치기하고 끝나면 복원")
    parser.add_argument("--unit-caps", default=None,
                        help="--portfolio 전용: 유닛 캡 실험값 '종목당/업종당/전체' "
                             "(기본 core 값 4/6/12). 각 자리에 정수 또는 none(무제한). "
                             "예: --unit-caps 4/8/16, --unit-caps 4/none/none. "
                             "실행 동안만 core.MAX_UNITS·MAX_UNITS_GROUP·MAX_UNITS_TOTAL을 "
                             "바꿔치기하고 끝나면 복원")
    parser.add_argument("--breakout-period", type=int, default=None,
                        help="--portfolio 전용: 진입 돌파 판정 기간 실험값 "
                             "(기본 core.HIGH_PERIOD=20). 예: --breakout-period 55. "
                             "ATR 기간·추세청산(10일 저가)은 그대로 - 돌파 기간만 바꾼다")
    parser.add_argument("--vol-mult", type=float, default=None,
                        help="--portfolio 전용: 진입 거래량 배수 하한 실험값 "
                             "(기본 1.0=필터 없음). 배수 = 신호일 거래량 / 직전 20일 평균. "
                             "예: --vol-mult 2.0 이면 신호일 거래량이 평소 2배 이상일 때만 진입")
    parser.add_argument("--breakeven-ratchet", action="store_true",
                        help="--portfolio 전용: 진입후최고가가 진입가 대비 +1ATR 이상 오르면 "
                             "손절선을 최소 본전(진입가)으로 올리는 규칙 추가 (core 트레일링과 별개, "
                             "backtest.py 안에서만 적용되는 추가 하한선)")
    parser.add_argument("--regime-gate", action="store_true",
                        help="--portfolio 전용: 신규 진입 종목이 속한 시장(코스피/코스닥) 지수가 "
                             "60일 이동평균 아래(하락/횡보 국면)면 신규 진입을 막는다 "
                             "(피라미딩 추가매수는 그대로 허용, data/index_regime_*.csv 필요)")
    parser.add_argument("--market", choices=("KOSPI", "KOSDAQ", "ALL"), default="ALL",
                        help="--portfolio 전용: 유니버스를 이 시장으로 제한한다 "
                             "(data/ticker_market.csv 기준, KOSDAQ에는 KOSDAQ GLOBAL 포함). "
                             "기본 ALL")
    parser.add_argument("--kospi200", action="store_true",
                        help="--portfolio 전용: 신규 진입을 진입일 시점 코스피200 편입 "
                             "종목으로 제한 (data/kospi200_history.csv 반기별 스냅샷, "
                             "시점별이라 생존편향 없음). 보유 종목 피라미딩은 계속 허용")
    args = parser.parse_args()

    # ── 구간 설정: --period가 [start, end] 기본 범위를 정하고, 이후 모든
    #    데이터 로드가 이 구간 상한을 넘지 못하게 가드가 걸린다. --start/--end는
    #    구간 안에서 더 좁힐 때만 쓴다.
    period_lo, period_hi = set_period(args.period)
    start = (datetime.strptime(args.start, "%Y%m%d").date() if args.start else period_lo)
    end = (datetime.strptime(args.end, "%Y%m%d").date() if args.end else period_hi)
    if not (period_lo <= start <= end <= period_hi):
        raise SystemExit(
            f"--start/--end({start}~{end})가 --period {args.period} 범위"
            f"({period_lo}~{period_hi}) 밖입니다."
        )
    if args.period == "holdout":
        print(HOLDOUT_BANNER)

    # ── 자본 모드 결정: dev는 "파라미터가 개선됐는가"만 보려는 것이므로 초반
    #    운(파산 여부)이 결과를 삼키지 않도록 fixed가 기본. holdout/full은 실제
    #    계좌를 재현해야 하므로 compound가 기본. --capital-mode로 명시 가능하고,
    #    구버전 --unlimited-cash는 fixed의 별칭.
    if args.capital_mode is not None:
        capital_mode = args.capital_mode
    elif args.unlimited_cash:
        capital_mode = "fixed"
    else:
        capital_mode = "fixed" if args.period == "dev" else "compound"

    def _log_holdout_if_needed(mode: str, summary: dict) -> None:
        if args.period != "holdout":
            return
        n = record_holdout_run(
            config={
                "mode": mode, "start": start, "end": end,
                "risk": core.RISK_PER_TRADE if args.risk is None else args.risk,
                "stop_mult": args.stop_mult if args.stop_mult is not None else core.STOP_ATR_MULT,
                "capital_mode": capital_mode,
                "unit_caps": args.unit_caps or f"{core.MAX_UNITS}/{core.MAX_UNITS_GROUP}/{core.MAX_UNITS_TOTAL}",
                "breakout_period": args.breakout_period or core.HIGH_PERIOD,
                "breakeven_ratchet": args.breakeven_ratchet,
                "regime_gate": args.regime_gate,
                "argv": " ".join(sys.argv[1:]),
            },
            summary=summary,
        )
        print(f"\n[holdout 이력] 이번이 {n}번째 검증 구간 실행입니다. 기록: {HOLDOUT_LOG_PATH}")
        if n > 1:
            print(f"[holdout 이력] 이미 {n}번 들여다봤습니다 - 이 구간은 그만큼 "
                  f"파라미터 선택에 오염됐다고 봐야 합니다.")

    def _parse_unit_caps(spec: Optional[str]) -> Optional[tuple[int, int, int]]:
        if not spec:
            return None
        parts = [p.strip().lower() for p in spec.split("/")]
        if len(parts) != 3:
            raise SystemExit(f"--unit-caps는 '종목당/업종당/전체' 3자리여야 합니다: {spec!r}")
        BIG = 10 ** 9
        out = []
        for p in parts:
            if p in ("none", "n", "-", "무제한", ""):
                out.append(BIG)
            else:
                out.append(int(p))
        return tuple(out)  # type: ignore[return-value]

    if args.portfolio:
        full = _load_full_raw()
        tickers = list_universe_tickers()
        logger.warning("유동성 필터 통과 종목: %d개 (전체 %d개 중)",
                       len(tickers), full["티커"].nunique())
        if args.market != "ALL":
            tm = pd.read_csv(TICKER_MARKET_PATH, dtype={"티커": str})
            want = {"KOSPI": {"KOSPI"},
                    "KOSDAQ": {"KOSDAQ", "KOSDAQ GLOBAL"}}[args.market]
            keep = set(tm.loc[tm["시장"].isin(want), "티커"])
            before = len(tickers)
            tickers = [t for t in tickers if t in keep]
            logger.warning("시장 필터(%s): %d개 → %d개", args.market, before, len(tickers))
        if args.kospi200:
            union = set().union(*[m for _, m in load_kospi200_pit()])
            before = len(tickers)
            tickers = [t for t in tickers if t in union]
            logger.warning("코스피200 필터(시점별 편입 union %d개): %d개 → %d개",
                           len(union), before, len(tickers))
        if args.limit:
            tickers = tickers[:args.limit]
        risk = core.RISK_PER_TRADE if args.risk is None else args.risk
        starting_capital = args.starting_capital
        is_fixed = capital_mode == "fixed"
        unit_caps = _parse_unit_caps(args.unit_caps)
        result = run_portfolio_backtest(
            tickers, start, end, risk=risk, starting_capital=starting_capital,
            unlimited_cash=is_fixed,
            fixed_account_size=starting_capital if is_fixed else None,
            stop_atr_mult=args.stop_mult, breakeven_ratchet=args.breakeven_ratchet,
            regime_gate=args.regime_gate, unit_caps=unit_caps,
            breakout_period=args.breakout_period, kospi200=args.kospi200,
            vol_mult_min=args.vol_mult,
        )
        out_csv = Path(args.out) if args.out else None
        market_desc = "코스피200(시점별)" if args.kospi200 else args.market
        metrics = print_portfolio_report(result, start, end, starting_capital, out_csv,
                                         capital_mode=capital_mode, unit_caps=unit_caps,
                                         breakout_period=args.breakout_period, market=market_desc)
        _log_holdout_if_needed("portfolio", {
            "거래수": metrics["거래수"], "승률": round(metrics["승률"], 2),
            "평균R": round(metrics["평균R"], 3), "총손익": round(metrics["총손익"]),
            "CAGR": round(metrics["연환산수익률"], 2),
            "MDD_비율": round(metrics["MDD_비율"], 2),
            "최종가치": round(result["final_value"]),
        })
        return 0

    if args.compare_modes:
        full = _load_full_raw()
        tickers = list_universe_tickers()
        logger.warning("유동성 필터 통과 종목: %d개 (전체 %d개 중)",
                       len(tickers), full["티커"].nunique())
        if args.limit:
            tickers = tickers[:args.limit]
        out_csv = Path(args.out) if args.out else None
        run_universe_compare(tickers, start, end, risk=args.risk, out_csv=out_csv)
        _log_holdout_if_needed("compare-modes", {"note": "지표는 콘솔 출력 참고"})
        return 0

    risk = core.RISK_PER_TRADE if args.risk is None else args.risk
    tickers = args.tickers.split(",") if args.tickers else [args.ticker]

    print(f"=== 백테스트: {', '.join(tickers)} | {start} ~ {end} ===")
    print(f"슬리피지 {SLIPPAGE*100:.1f}% · 매매비용 {COST_RATE*100:.2f}% · "
          f"계좌 {STARTING_CAPITAL:,.0f}원 · 리스크 {risk*100:.2f}%\n")

    all_single: list[Trade] = []
    for ticker in tickers:
        trades, open_position = run_backtest(ticker, start, end, risk=risk)
        print_trades(ticker, trades, open_position)
        all_single.extend(trades)
        print()
    m = compute_metrics(all_single)
    _log_holdout_if_needed("single", {
        "종목": ",".join(tickers), "거래수": m["거래수"],
        "승률": round(m["승률"], 2) if m["거래수"] else None,
        "평균R": round(m["평균R"], 3) if m["거래수"] else None,
        "총손익": round(m["총손익"]),
    })
    return 0


if __name__ == "__main__":
    sys.exit(main())
