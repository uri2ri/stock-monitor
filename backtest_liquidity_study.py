"""
backtest_liquidity_study.py – "유동성 때문에 최초 돌파를 놓친 뒤 늦게 진입하는 문제" 검증

백테스트·분석 전용이다. 라이브 매매 코드(core.py·kis_client.py·intraday_watch.py·
scan_all.py)는 읽기만 하고 고치지 않는다. 실행은 backtest.py CLI로:

    python backtest.py --liquidity-entry-study --period dev --mode intraday
    python backtest.py --liquidity-entry-study --ticker 171090 --period dev --mode intraday

[배경]
실운영은 20일 평균 거래대금 3억 이상을 감시하고(scan_all.WATCH_MIN_TRADING_VALUE),
5억 이상만 자동 신규매수한다(core.BUY_MIN_TRADING_VALUE). 3~5억 구간에서 첫 20일
돌파가 나온 종목은 매수하지 않지만, 주가가 오르며 거래대금이 커져 5억을 넘긴 뒤
다시 20일 고가를 돌파하면 그때 뒤늦게 매수된다. 그 늦은 진입이 실제로 성과가
나쁜지를 아래 세 전략으로 비교한다.

  strategy_baseline                    현재 구조. 5억 이상 + 정상 20일 돌파 + 추격금지 통과면
                                       진입. 과거 3~5억 돌파 여부는 보지 않는다.
  strategy_skip_lowliq_first_breakout  3~5억에서 최초 돌파가 나온 추세(사이클)가 끝날 때까지
                                       신규진입 금지.
  strategy_late_entry_guard            같은 추세라도 (진입검토가 - 최초 돌파선) / 최초 돌파 ATR
                                       ≤ core.CHASE_ATR_MULT면 허용, 넘으면 금지.

[추세 사이클 정의 - 전략 B·C의 "같은 추세"]
사이클은 가격만으로 정해진다(어느 전략이 보유 중인지와 무관). 그래야 세 전략이
같은 사건 집합을 놓고 비교된다.
  - 시작: 진행 중인 사이클이 없을 때 나온 20일 고가 돌파. 그 돌파가 사이클의
    "최초 돌파"이고, 그날의 돌파선·ATR·20일 평균 거래대금·종가를 기록한다.
    사이클 유동성 구간(band)은 이 최초 돌파 시점 거래대금으로 고정된다.
  - 종료: 최초 돌파 다음 거래일부터, 종가 ≤ 직전 10일 저가(당일 제외).
    터틀 시스템1의 청산 규칙(core.LOW_PERIOD=10)이자 core.evaluate_holding()의
    "추세청산" 판정과 같은 비교(`<=`)다. 즉 "최초 돌파에 샀다면 추세청산으로
    나왔을 날"까지를 같은 추세로 본다. 종료일 다음 거래일부터 나오는 20일
    돌파는 새 사이클의 최초 돌파다(Case 9).
  - 2×ATR 손절은 사이클 종료 조건에 넣지 않았다. 손절선은 진입가·트레일링
    (half 방식)에 따라 움직이는 포지션 관리 값이라 "추세 구조"의 정의로는
    진입가 가정이 하나 더 필요하다. 10일 저가 이탈은 가격만으로 정해지고
    라이브 청산 개념과 그대로 맞는다.
  - 최초 돌파 시 거래대금이 3억 미만이면(감시 대상 아님) 그 사이클은 저유동
    최초 돌파가 아니다(Case 4). 같은 사이클 안에서 나중에 3~5억 돌파가 나와도
    다시 분류하지 않는다 - "최초 20일 돌파 당시 거래대금"으로 집단을 나누기 위함.
  - 최초 돌파 시 5억 이상이면(정확히 5억 포함, core.is_buy_liquid와 같은 `>=`)
    일반 매수 영역이고 저유동 상태가 생기지 않는다(Case 3·6). 정확히 3억은
    3~5억 구간에 포함된다(Case 5).
  - 최초 돌파는 추격금지 여부와 무관하게 기록한다(가격이 20일 고가를 넘은
    사실 자체가 추세 시작이다). 추격금지였는지는 first_breakout_chase_ok로 남긴다.

[late_entry_gap_atr]
  (진입 검토 가격 - first_breakout_threshold) / first_breakout_atr
  - 진입 검토 가격: intraday는 max(당일 시가, 당일 돌파선)(= 기존 intraday 체결
    기준가), close는 신호일 종가. 슬리피지 전 가격이며, 결정 시점에 알 수 있는 값만 쓴다.
  - first_breakout_threshold: 최초 돌파 당시 실제 돌파선(두 모드 모두 직전 20거래일
    고가 = intraday는 전일까지 20일 고가 = 라이브 high20_next와 같은 값).
  - first_breakout_atr가 0·NaN·음수·숫자 아님이면 갭을 계산하지 않고(None)
    late-entry-guard는 진입을 막는다(fail-closed, Case 7). core.is_buy_liquid가
    거래대금을 모르면 매수를 막는 것과 같은 원칙이다. skip 전략은 ATR과 무관하게 막는다.

[거래대금 - 미래참조 없음]
일별 거래대금 = 종가 × 거래량, 최근 20거래일 평균 (scan_all.scan_row와 같은 식).
  - intraday: 전일 종가까지의 20일 평균. 라이브는 야간 스캔(전일 종가 포함) 값을
    다음날 장중 내내 쓴다.
  - close: 신호일 종가까지의 20일 평균. 종가 확정 후 스캔하는 시점과 같다.
기존 backtest.py의 screener.MIN_TRADING_VALUE(10억) 필터는 쓰지 않는다(그 값은
바꾸지 않는다). 유니버스는 구간 안에서 20일 평균이 한 번이라도 3억 이상이었던 종목.

[시뮬레이션 - 종목별 독립, 고정 유닛]
전략 비교에서 다른 종목과의 자금 경쟁(현금·상관군캡·일일상한)이 섞이면 필터 효과가
흐려진다. 그래서 종목마다 독립으로 돌리되 각 종목 안에서는 라이브 규칙을 따른다:
  - 진입: 위 정상 돌파 + 추격금지(core.breakout_verdict) + 주가 상한·유닛금액 상한
    (backtest.MAX_STOCK_PRICE / MAX_UNIT_RATIO = kis_client와 같은 값) + 전략 필터.
    intraday는 당일 체결(기존 _entry_signal_intraday와 같은 체결가·추격 판정),
    close는 다음 거래일 시가 체결.
  - 유닛 크기: core.calc_position(진입 ATR, 1000만원 고정, core.RISK_PER_TRADE).
  - 피라미딩: core.build_pyramid 계획, 종가가 다음 단계가 이상이면 다음날 시가 추가
    (backtest.run_portfolio_backtest와 같은 방식, 종목당 core.MAX_UNITS까지).
  - 청산: core.evaluate_holding(as_of=당일)의 "손절"·"추세청산" → 다음날 시가.
  - 보유 중에는 전략 필터가 아무 것도 하지 않는다 - 피라미딩·청산은 세 전략이
    동일하다(Case 8). 전략 필터는 미보유 상태의 신규 1유닛 진입에만 걸린다.
  - 계좌: 1000만원 기준 명목 계좌(현금 제약 없음 = backtest --capital-mode fixed와
    같은 성격). 최종 자산 = 1000만원 + 누적 손익(미청산은 마지막 종가 평가),
    MDD는 전 종목 일별 시가평가 손익 합계 곡선에서 잰다. 실계좌 재현이 아니라
    전략 간 비교용이다.

[지표 정의]
  R = 순손익 / (1유닛 주수 × core.STOP_ATR_MULT × 진입 ATR)  (backtest.PortfolioTrade와 같음)
  expectancy(원/거래) = 평균 순손익. 평균 R은 R 기준 기대값이다.
  +0.5 ATR / -1 ATR 선도달: 진입 체결일 다음 거래일부터 최대 20거래일 동안
    고가 ≥ 진입가 + core.PYRAMID_ATR_STEP×ATR(첫 추가매수 지점)과
    저가 ≤ 진입가 - 1×ATR 중 먼저 닿은 쪽. 같은 날 둘 다 닿으면 "동시"로 따로 센다.
  거래 통계는 청산 완료 거래만, 최종 자산·MDD는 미청산 평가까지 포함한다.
"""

from __future__ import annotations

import bisect
import logging
import math
import statistics
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable, Optional

import numpy as np
import pandas as pd

import backtest as bt
import core
import scan_all
import screener

logger = logging.getLogger(__name__)

# ── 유동성 기준 (라이브 값 재사용 - 새 숫자를 만들지 않는다) ────────
WATCH_MIN_TURNOVER = scan_all.WATCH_MIN_TRADING_VALUE   # 3억 - 감시 하한
BUY_MIN_TURNOVER = core.BUY_MIN_TRADING_VALUE           # 5억 - 자동매수 하한
TURNOVER_DAYS = scan_all.TRADING_VALUE_DAYS             # 20일
# 보고용 구간 경계일 뿐 매매 기준이 아니다(screener.MIN_TRADING_VALUE와 무관).
BAND_EDGE_HIGH = 1_000_000_000                          # 10억

BAND_UNKNOWN = "liquidity_band_unknown"
BAND_BELOW_3 = "liquidity_band_below_3"
BAND_3_5 = "liquidity_band_3_5"
BAND_5_10 = "liquidity_band_5_10"
BAND_10_PLUS = "liquidity_band_10_plus"
# unknown = 최초 돌파 시점 20일 거래대금이 0(장기 거래정지 직후 등)이라 구간을 못 정한 사이클.
# 저유동으로 분류하지 않으며(전략 필터 없음), 합계가 맞도록 표에는 같이 싣는다.
REPORT_BANDS = (BAND_BELOW_3, BAND_3_5, BAND_5_10, BAND_10_PLUS, BAND_UNKNOWN)

STRATEGY_BASELINE = "strategy_baseline"
STRATEGY_SKIP = "strategy_skip_lowliq_first_breakout"
STRATEGY_GUARD = "strategy_late_entry_guard"
STRATEGIES = (STRATEGY_BASELINE, STRATEGY_SKIP, STRATEGY_GUARD)

MODES = ("intraday", "close")

# 분석 지표용 (매매 규칙 아님)
FIRST_REACH_DOWN_ATR = 1.0
FIRST_REACH_HORIZON = 20
FWD_HORIZONS = (3, 5, 10)

CASE_TICKER = "171090"                 # 선익시스템
CASE_DATE = date(2026, 10, 2)          # 확인하려는 최초 돌파 추정일
CASE_WINDOW_DAYS = 10                  # 그 날짜 앞뒤 거래일 범위


# ── 순수 판정 함수 ───────────────────────────────────────────

def liquidity_band(value) -> str:
    """20일 평균 거래대금 → 유동성 구간 이름. 무효 값(None·NaN·0 이하)은 unknown."""
    v = core.valid_trading_value(value)
    if v is None:
        return BAND_UNKNOWN
    if v < WATCH_MIN_TURNOVER:
        return BAND_BELOW_3
    if v < BUY_MIN_TURNOVER:
        return BAND_3_5
    if v < BAND_EDGE_HIGH:
        return BAND_5_10
    return BAND_10_PLUS


@dataclass
class TrendCycle:
    """최초 20일 돌파부터 10일 저가 이탈까지의 한 추세."""

    first_breakout_date: date
    first_breakout_threshold: float
    first_breakout_atr: float
    first_breakout_turnover: float
    first_breakout_close: float
    band: str
    first_breakout_chase_ok: bool = True
    end_date: Optional[date] = None

    @property
    def is_low_liquidity(self) -> bool:
        """3억 이상 5억 미만에서 시작한 사이클 = 저유동 최초 돌파."""
        return self.band == BAND_3_5


def late_entry_gap_atr(candidate_price: float, cycle: TrendCycle) -> Optional[float]:
    """최초 돌파선 대비 진입 검토가의 거리(×최초 돌파 ATR). ATR이 무효면 None."""
    try:
        atr = float(cycle.first_breakout_atr)
        thr = float(cycle.first_breakout_threshold)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(atr) and atr > 0 and math.isfinite(thr)):
        return None
    return (candidate_price - thr) / atr


def strategy_allows_entry(strategy: str, *, turnover_20, cycle: Optional[TrendCycle],
                          candidate_price: float) -> tuple[bool, str]:
    """신규 1유닛 진입 허용 여부와 막힌 사유.

    정상 20일 돌파·추격금지 통과는 호출 전에 확인됐다고 본다 - 여기서는 세 전략이
    갈리는 부분(5억 게이트 + 저유동 최초 돌파 이력)만 판정한다.
    """
    if not core.is_buy_liquid(turnover_20):
        return False, "유동성미달"
    if strategy == STRATEGY_BASELINE:
        return True, ""
    if cycle is None or not cycle.is_low_liquidity:
        return True, ""
    if strategy == STRATEGY_SKIP:
        return False, "저유동최초돌파_동일추세"
    if strategy == STRATEGY_GUARD:
        gap = late_entry_gap_atr(candidate_price, cycle)
        if gap is None:
            return False, "최초돌파ATR무효"
        if gap <= core.CHASE_ATR_MULT:
            return True, ""
        return False, "늦은진입"
    raise ValueError(f"모르는 전략: {strategy!r}")


class CycleTracker:
    """종목 하나의 추세 사이클 상태. 하루에 observe_breakout → observe_close 순서로 부른다."""

    def __init__(self) -> None:
        self.active: Optional[TrendCycle] = None
        self.history: list[TrendCycle] = []

    def observe_breakout(self, day: date, *, threshold: float, atr: float,
                         turnover_20, close: float, chase_ok: bool = True) -> TrendCycle:
        """20일 돌파가 났다. 진행 중 사이클이 없으면 새 사이클을 연다."""
        if self.active is None:
            v = core.valid_trading_value(turnover_20)
            self.active = TrendCycle(
                first_breakout_date=day,
                first_breakout_threshold=float(threshold),
                first_breakout_atr=atr,
                first_breakout_turnover=v if v is not None else float("nan"),
                first_breakout_close=float(close),
                band=liquidity_band(turnover_20),
                first_breakout_chase_ok=chase_ok,
            )
            self.history.append(self.active)
        return self.active

    def observe_close(self, day: date, *, close: float, low10_prev) -> bool:
        """종가 ≤ 직전 10일 저가면 사이클 종료. 최초 돌파 당일은 보지 않는다."""
        c = self.active
        if c is None or day <= c.first_breakout_date:
            return False
        try:
            lv = float(low10_prev)
        except (TypeError, ValueError):
            return False
        if math.isfinite(lv) and close <= lv:
            c.end_date = day
            self.active = None
            return True
        return False


# ── 종목별 사전 계산 ─────────────────────────────────────────

@dataclass
class TickerDays:
    """종목 하나의 일별 판정 재료. i번째 원소 = i번째 거래일."""

    ticker: str
    df: pd.DataFrame                 # 시가/고가/저가/종가/거래량, 날짜 인덱스
    dates: list
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    tv20: np.ndarray                 # 당일 종가까지 20일 평균 거래대금
    threshold: np.ndarray            # 직전 20거래일 고가(당일 제외)
    low10_prev: np.ndarray           # 직전 10거래일 저가(당일 제외)
    _atr_cache: dict = field(default_factory=dict)

    def atr_through(self, i: int) -> int:
        """i번째 날 종가까지 데이터로 계산한 ATR - backtest의 fetch 대체와 같은 슬라이스."""
        if i not in self._atr_cache:
            lo = max(0, i + 1 - core.FETCH_DAYS)
            window = self.df.iloc[lo:i + 1]
            try:
                self._atr_cache[i] = core.round_atr(core.calc_atr(window))
            except ValueError:
                self._atr_cache[i] = 0
        return self._atr_cache[i]


def prepare_ticker_days(ticker: str, df: pd.DataFrame) -> TickerDays:
    df = df[["시가", "고가", "저가", "종가", "거래량"]].astype(float)
    turnover = df["종가"] * df["거래량"]
    return TickerDays(
        ticker=ticker, df=df, dates=[ts.date() for ts in df.index],
        open=df["시가"].to_numpy(), high=df["고가"].to_numpy(),
        low=df["저가"].to_numpy(), close=df["종가"].to_numpy(),
        tv20=turnover.rolling(TURNOVER_DAYS).mean().to_numpy(),
        threshold=df["고가"].rolling(core.HIGH_PERIOD).max().shift(1).to_numpy(),
        low10_prev=df["저가"].rolling(core.LOW_PERIOD).min().shift(1).to_numpy(),
    )


@dataclass
class DaySignal:
    """i번째 날의 돌파 판정 (모드별 시점 기준)."""

    triggered: bool
    threshold: float = float("nan")
    atr: int = 0
    turnover_20: float = float("nan")
    chase_ok: bool = False
    candidate_price: float = float("nan")
    gap_atr: float = float("nan")


def day_signal(td: TickerDays, i: int, mode: str) -> DaySignal:
    """모드별로 그날 알 수 있는 값만으로 20일 돌파를 판정한다.

    intraday: 당일 고가 > 돌파선이면 돌파. ATR·거래대금은 전일 종가까지.
              추격 판정은 당일 시가 기준(backtest._entry_signal_intraday와 같음).
    close:    당일 종가 > 돌파선이면 돌파. ATR·거래대금은 당일 종가까지.
    두 모드 모두 돌파선 = 직전 20거래일 고가(당일 제외).
    core.MIN_TRADING_DAYS 미만 이력이면 판정하지 않는다(fetch 대체와 같은 조건).
    """
    thr = td.threshold[i]
    if not np.isfinite(thr):
        return DaySignal(False)
    if mode == "intraday":
        basis = i - 1                     # 전일 종가까지가 판단 재료
        if basis + 1 < core.MIN_TRADING_DAYS or td.high[i] <= thr:
            return DaySignal(False)
        atr = td.atr_through(basis)
        tv = td.tv20[basis]
        today_open = td.open[i]
        gap = (today_open - thr) / atr if atr > 0 else float("nan")
        chase_ok = atr > 0 and core.breakout_verdict(gap) != "추격금지"
        candidate = today_open if today_open >= thr else thr
    elif mode == "close":
        basis = i
        if basis + 1 < core.MIN_TRADING_DAYS or td.close[i] <= thr:
            return DaySignal(False)
        atr = td.atr_through(basis)
        tv = td.tv20[basis]
        candidate = td.close[i]
        gap = (candidate - thr) / atr if atr > 0 else float("nan")
        chase_ok = atr > 0 and core.breakout_verdict(gap) == "진입가능"
    else:
        raise ValueError(f"모르는 모드: {mode!r}")
    return DaySignal(True, threshold=float(thr), atr=int(atr), turnover_20=float(tv),
                     chase_ok=bool(chase_ok), candidate_price=float(candidate),
                     gap_atr=float(gap))


def build_cycles(td: TickerDays, mode: str) -> tuple[list[DaySignal], list[Optional[TrendCycle]], list[TrendCycle]]:
    """가격만으로 추세 사이클을 만든다(전략·보유와 무관).

    반환: (일별 DaySignal, 일별 '진입 판단 시점의 사이클', 전체 사이클 목록).
    진입 판단 시점의 사이클 = 당일 돌파까지 반영한 뒤, 당일 종가 종료 판정 전 상태.
    """
    tracker = CycleTracker()
    signals: list[DaySignal] = []
    cycle_at: list[Optional[TrendCycle]] = []
    for i, day in enumerate(td.dates):
        sig = day_signal(td, i, mode)
        if sig.triggered:
            tracker.observe_breakout(day, threshold=sig.threshold,
                                     atr=sig.atr if sig.atr > 0 else float("nan"),
                                     turnover_20=sig.turnover_20, close=td.close[i],
                                     chase_ok=sig.chase_ok)
        signals.append(sig)
        cycle_at.append(tracker.active)
        tracker.observe_close(day, close=td.close[i], low10_prev=td.low10_prev[i])
    return signals, cycle_at, tracker.history


# ── 시뮬레이션 ───────────────────────────────────────────────

@dataclass
class StudyTrade:
    strategy: str
    mode: str
    ticker: str
    name: str
    signal_date: date
    entry_date: date
    entry_price: float              # 1유닛 체결가(슬리피지 반영)
    entry_atr: int
    unit_shares: int
    entry_turnover_20: float
    entry_gap_atr: float            # 당일 돌파선 대비 갭(추격 판정에 쓴 값)
    band: str                       # 진입 시점 사이클의 최초 돌파 유동성 구간
    cycle_first_breakout_date: Optional[date]
    cycle_gap_atr: Optional[float]  # 사이클 최초 돌파선 대비 거리(= late_entry_gap_atr)
    entry_idx: int = 0
    exit_idx: Optional[int] = None
    exit_date: Optional[date] = None
    exit_price: Optional[float] = None
    exit_verdict: str = ""
    exit_reason: str = ""
    final_units: int = 1
    total_shares: int = 0
    total_invested: float = 0.0
    proceeds: float = 0.0
    open_at_end: bool = False
    first_reach: str = ""           # up | down | both | none
    mtm_deltas: list = field(default_factory=list, repr=False)  # [(date, Δ시가평가손익)]

    @property
    def net_pnl(self) -> float:
        return self.proceeds - self.total_invested

    @property
    def r_multiple(self) -> float:
        risk_1r = self.unit_shares * core.STOP_ATR_MULT * self.entry_atr
        return self.net_pnl / risk_1r if risk_1r else float("nan")

    @property
    def holding_days(self) -> Optional[int]:
        return None if self.exit_idx is None else self.exit_idx - self.entry_idx


@dataclass
class _Pos:
    trade: StudyTrade
    plan: list
    units: list                      # [(fill, shares)]
    trailing_high: Optional[float] = None
    stop_loss: Optional[float] = None
    last_mtm: float = 0.0

    @property
    def total_shares(self) -> int:
        return sum(s for _, s in self.units)

    @property
    def last_buy_price(self) -> float:
        return self.units[-1][0]


_CUR_DF: Optional[pd.DataFrame] = None
_CUR_CODE: Optional[str] = None


def _fetch_current(code: str, days: int = core.FETCH_DAYS,
                   end: Optional[date] = None) -> pd.DataFrame:
    """core.fetch_ohlcv 대체 - 지금 시뮬레이션 중인 종목의 end 이전 데이터만."""
    if end is None:
        raise ValueError("백테스트에서는 end(as_of) 없는 fetch_ohlcv 호출을 허용하지 않는다")
    bt._guard_period_end(end, "liquidity_study._fetch_current")
    if code != _CUR_CODE or _CUR_DF is None:
        raise ValueError(f"[{code}] 준비된 데이터 없음")
    pos = _CUR_DF.index.searchsorted(pd.Timestamp(end), side="right")
    df = _CUR_DF.iloc[max(0, pos - days):pos]
    if len(df) < core.MIN_TRADING_DAYS:
        raise ValueError(f"[{code}] 거래일 부족: {len(df)}일")
    return df


def _first_reach(td: TickerDays, entry_idx: int, entry_price: float, atr: float) -> str:
    up = entry_price + core.PYRAMID_ATR_STEP * atr
    down = entry_price - FIRST_REACH_DOWN_ATR * atr
    for j in range(entry_idx + 1, min(len(td.dates), entry_idx + 1 + FIRST_REACH_HORIZON)):
        hit_up = td.high[j] >= up
        hit_down = td.low[j] <= down
        if hit_up and hit_down:
            return "both"
        if hit_up:
            return "up"
        if hit_down:
            return "down"
    return "none"


def simulate_strategy(td: TickerDays, name: str, mode: str, strategy: str,
                      signals: list[DaySignal], cycle_at: list[Optional[TrendCycle]],
                      start: date, end: date, *,
                      capital: float = bt.STARTING_CAPITAL,
                      risk: Optional[float] = None) -> list[StudyTrade]:
    """한 종목·한 전략. core.fetch_ohlcv는 호출 쪽(simulate_ticker)이 바꿔치기해 둔다."""
    risk = core.RISK_PER_TRADE if risk is None else risk
    trades: list[StudyTrade] = []
    pos: Optional[_Pos] = None
    pending_entry: Optional[dict] = None
    pending_exit: Optional[tuple[str, str]] = None
    pending_add = False

    def _mark(i: int, value: float) -> None:
        pos.trade.mtm_deltas.append((td.dates[i], value - pos.last_mtm))
        pos.last_mtm = value

    def _open(i: int, fill: float, sig: DaySignal, signal_idx: int, shares: int) -> _Pos:
        cyc = cycle_at[signal_idx]
        t = StudyTrade(
            strategy=strategy, mode=mode, ticker=td.ticker, name=name,
            signal_date=td.dates[signal_idx], entry_date=td.dates[i], entry_price=fill,
            entry_atr=sig.atr, unit_shares=shares, entry_turnover_20=sig.turnover_20,
            entry_gap_atr=sig.gap_atr,
            band=cyc.band if cyc else BAND_UNKNOWN,
            cycle_first_breakout_date=cyc.first_breakout_date if cyc else None,
            cycle_gap_atr=late_entry_gap_atr(sig.candidate_price, cyc) if cyc else None,
            entry_idx=i,
        )
        t.total_invested = fill * shares * (1 + bt.COST_RATE)
        t.first_reach = _first_reach(td, i, fill, sig.atr)
        plan = core.build_pyramid(fill, sig.atr, shares, capital=capital, max_units=core.MAX_UNITS)
        return _Pos(trade=t, plan=plan, units=[(fill, shares)])

    def _entry_ok(i: int, sig: DaySignal) -> Optional[int]:
        """정상 돌파·추격금지·공통 게이트·전략 필터 통과 시 1유닛 주수."""
        if not sig.triggered or not sig.chase_ok or sig.atr <= 0:
            return None
        ok, _ = strategy_allows_entry(strategy, turnover_20=sig.turnover_20,
                                      cycle=cycle_at[i], candidate_price=sig.candidate_price)
        if not ok:
            return None
        if sig.candidate_price > bt.MAX_STOCK_PRICE:
            return None
        shares = core.calc_position(sig.atr, capital, risk).unit_shares
        if shares <= 0 or shares * sig.candidate_price > capital * bt.MAX_UNIT_RATIO:
            return None
        return shares

    lo = bisect_dates(td.dates, start)
    hi = bisect_dates(td.dates, end, right=True)
    for i in range(lo, hi):
        today = td.dates[i]
        o = td.open[i]

        # 1) 어제 확정된 청산·진입·추가매수를 오늘 시가로.
        if pending_exit is not None and pos is not None:
            fill = o * (1 - bt.SLIPPAGE)
            t = pos.trade
            t.proceeds = fill * pos.total_shares * (1 - bt.COST_RATE)
            t.exit_idx, t.exit_date, t.exit_price = i, today, fill
            t.exit_verdict, t.exit_reason = pending_exit
            t.final_units, t.total_shares = len(pos.units), pos.total_shares
            _mark(i, t.net_pnl)
            trades.append(t)
            pos = None
        pending_exit = None

        if pending_entry is not None and pos is None:
            fill = o * (1 + bt.SLIPPAGE)
            pos = _open(i, fill, pending_entry["sig"], pending_entry["idx"], pending_entry["shares"])
        pending_entry = None

        if pending_add and pos is not None and len(pos.units) < core.MAX_UNITS:
            fill = o * (1 + bt.SLIPPAGE)
            shares = pos.trade.unit_shares
            pos.units.append((fill, shares))
            pos.trade.total_invested += fill * shares * (1 + bt.COST_RATE)
        pending_add = False

        # 2) intraday 신규 진입 - 당일 체결.
        sig = signals[i]
        if mode == "intraday" and pos is None:
            shares = _entry_ok(i, sig)
            if shares is not None:
                pos = _open(i, sig.candidate_price * (1 + bt.SLIPPAGE), sig, i, shares)

        # 3) 종가 판정 - 보유 중이면 청산·피라미딩(전략과 무관), 아니면 close 모드 신호.
        if pos is not None:
            c = td.close[i]
            inp = core.HoldingInput(
                ticker=td.ticker, name=td.ticker, market="",
                buy_price=pos.last_buy_price, shares=pos.total_shares,
                prev_trailing_high=pos.trailing_high, prev_stop_loss=pos.stop_loss,
                entry_atr=pos.trade.entry_atr, last_buy_price=pos.last_buy_price,
            )
            result = core.evaluate_holding(inp, capital, as_of=today)
            if result.verdict != "조회실패":
                pos.trailing_high = result.trailing_high
                pos.stop_loss = result.stop_loss
            if result.verdict in ("손절", "추세청산"):
                pending_exit = (result.verdict, result.verdict_memo)
            elif len(pos.units) < core.MAX_UNITS and len(pos.plan) > len(pos.units):
                step = pos.plan[len(pos.units)]
                if (c >= step.buy_price and c <= bt.MAX_STOCK_PRICE
                        and pos.trade.unit_shares * c <= capital * bt.MAX_UNIT_RATIO):
                    pending_add = True
            _mark(i, pos.total_shares * c - pos.trade.total_invested)
        elif mode == "close":
            shares = _entry_ok(i, sig)
            if shares is not None:
                pending_entry = {"sig": sig, "idx": i, "shares": shares}

    if pos is not None:   # 구간 끝 미청산 - 마지막 종가로 평가만 한다
        t = pos.trade
        last = hi - 1
        t.open_at_end = True
        t.final_units, t.total_shares = len(pos.units), pos.total_shares
        t.exit_price = td.close[last]
        t.proceeds = td.close[last] * pos.total_shares
        trades.append(t)
    return trades


def bisect_dates(dates: list, d: date, right: bool = False) -> int:
    return (bisect.bisect_right if right else bisect.bisect_left)(dates, d)


@dataclass
class TickerResult:
    ticker: str
    name: str
    mode: str
    td: TickerDays
    cycles: list[TrendCycle]
    signals: list[DaySignal]
    trades: dict[str, list[StudyTrade]]


def simulate_ticker(ticker: str, df: pd.DataFrame, mode: str, start: date, end: date, *,
                    name: str = "", strategies=STRATEGIES,
                    risk: Optional[float] = None) -> TickerResult:
    """한 종목을 세 전략으로. 사이클은 한 번만 만들어 공유한다."""
    global _CUR_DF, _CUR_CODE
    td = prepare_ticker_days(ticker, df)
    signals, cycle_at, cycles = build_cycles(td, mode)
    _CUR_DF, _CUR_CODE = td.df, ticker
    original = core.fetch_ohlcv
    core.fetch_ohlcv = _fetch_current
    try:
        trades = {s: simulate_strategy(td, name, mode, s, signals, cycle_at, start, end, risk=risk)
                  for s in strategies}
    finally:
        core.fetch_ohlcv = original
    return TickerResult(ticker, name, mode, td, cycles, signals, trades)


# ── 지표 ─────────────────────────────────────────────────────

def equity_curve(trades: list[StudyTrade], capital: float = bt.STARTING_CAPITAL) -> pd.Series:
    """전 거래의 일별 시가평가 손익 변화를 합친 명목 계좌 곡선."""
    acc: dict = {}
    for t in trades:
        for d, delta in t.mtm_deltas:
            acc[d] = acc.get(d, 0.0) + delta
    if not acc:
        return pd.Series(dtype=float)
    s = pd.Series(acc).sort_index()
    return capital + s.cumsum()


def compute_study_metrics(trades: list[StudyTrade],
                          capital: float = bt.STARTING_CAPITAL) -> dict:
    closed = [t for t in trades if not t.open_at_end]
    n = len(closed)
    rs = [t.r_multiple for t in closed]
    pnls = [t.net_pnl for t in closed]
    gross_win = sum(p for p in pnls if p > 0)
    gross_loss = -sum(p for p in pnls if p < 0)

    eq = equity_curve(trades, capital)
    if eq.empty:
        final_value, mdd_amt, mdd_pct = capital, 0.0, 0.0
    else:
        final_value = float(eq.iloc[-1])
        peak = eq.cummax().clip(lower=capital)
        dd = peak - eq
        mdd_amt = float(dd.max())
        mdd_pct = float((dd / peak).max() * 100)
    # 미청산 포지션의 마지막 평가는 mtm_deltas에 이미 들어 있다.

    def _ratio(kind: str) -> float:
        return sum(1 for t in closed if t.first_reach == kind) / n * 100 if n else float("nan")

    return {
        "거래수": n,
        "미청산": len(trades) - n,
        "승률": sum(1 for p in pnls if p > 0) / n * 100 if n else float("nan"),
        "평균R": sum(rs) / n if n else float("nan"),
        "중앙R": statistics.median(rs) if n else float("nan"),
        # 평균 R의 표준오차. R 분포 꼬리가 두꺼워 그룹 간 평균 차이를 볼 때 같이 본다.
        "R_SE": statistics.stdev(rs) / math.sqrt(n) if n > 1 else float("nan"),
        "기대값원": sum(pnls) / n if n else float("nan"),
        "총손익": sum(pnls),
        "총수익률": (final_value - capital) / capital * 100,
        "최종자산": final_value,
        "MDD금액": mdd_amt,
        "MDD비율": mdd_pct,
        "손절수": sum(1 for t in closed if t.exit_verdict == "손절"),
        "추세청산수": sum(1 for t in closed if t.exit_verdict == "추세청산"),
        "평균보유일": (sum(t.holding_days for t in closed) / n) if n else float("nan"),
        "PF": gross_win / gross_loss if gross_loss > 0 else float("nan"),
        "+0.5ATR선도달": _ratio("up"),
        "-1ATR선도달": _ratio("down"),
        "동시도달": _ratio("both"),
    }


METRIC_ROWS = [
    ("총 거래 수(청산)", "거래수", "{:,.0f}"),
    ("미청산(구간 끝)", "미청산", "{:,.0f}"),
    ("승률 %", "승률", "{:.1f}"),
    ("평균 R", "평균R", "{:.3f}"),
    ("median R", "중앙R", "{:.3f}"),
    ("평균 R 표준오차", "R_SE", "{:.3f}"),
    ("expectancy 원/거래", "기대값원", "{:,.0f}"),
    ("총 수익률 %", "총수익률", "{:.1f}"),
    ("최종 자산 원", "최종자산", "{:,.0f}"),
    ("MDD 원", "MDD금액", "{:,.0f}"),
    ("MDD %(고점 대비)", "MDD비율", "{:.1f}"),
    ("손절 거래 수", "손절수", "{:,.0f}"),
    ("추세청산 거래 수", "추세청산수", "{:,.0f}"),
    ("평균 보유기간(거래일)", "평균보유일", "{:.1f}"),
    ("profit factor", "PF", "{:.2f}"),
    ("+0.5ATR 선도달 %", "+0.5ATR선도달", "{:.1f}"),
    ("-1ATR 선도달 %", "-1ATR선도달", "{:.1f}"),
    ("같은날 동시 %", "동시도달", "{:.1f}"),
]


def _fmt(fmt: str, v) -> str:
    try:
        if v is None or (isinstance(v, float) and math.isnan(v)):
            return "-"
        return fmt.format(v)
    except (TypeError, ValueError):
        return str(v)


def print_metrics_block(title: str, metrics_by_col: dict[str, dict]) -> None:
    cols = list(metrics_by_col)
    w = 38
    print(f"\n--- {title} ---")
    print(f"{'지표':<24}" + "".join(f"{c:>{w}}" for c in cols))
    for label, key, fmt in METRIC_ROWS:
        print(f"{label:<24}" + "".join(f"{_fmt(fmt, metrics_by_col[c].get(key)):>{w}}"
                                        for c in cols))


# ── 저유동 최초 돌파 이벤트 표 ─────────────────────────────────

def _fwd_stats(td: TickerDays, idx: int, price: float) -> dict:
    out = {}
    n = len(td.dates)
    for h in FWD_HORIZONS:
        j = idx + h
        out[f"ret_{h}d_pct"] = (td.close[j] / price - 1) * 100 if j < n else float("nan")
    for h in (5, 10):
        seg = slice(idx + 1, min(n, idx + 1 + h))
        if idx + 1 < n:
            out[f"mfe_{h}d_pct"] = (td.high[seg].max() / price - 1) * 100
            out[f"mae_{h}d_pct"] = (td.low[seg].min() / price - 1) * 100
        else:
            out[f"mfe_{h}d_pct"] = out[f"mae_{h}d_pct"] = float("nan")
    return out


def _first_trade_in_cycle(trades: list[StudyTrade], cyc: TrendCycle,
                          period_end: date) -> Optional[StudyTrade]:
    stop = cyc.end_date or period_end
    for t in trades:
        if cyc.first_breakout_date <= t.signal_date <= stop:
            return t
    return None


def low_liquidity_events(res: TickerResult, start: date, end: date) -> list[dict]:
    """3~5억 최초 돌파 사이클마다 한 줄. 최초 돌파일이 [start, end] 안인 것만."""
    td = res.td
    rows = []
    for cyc in res.cycles:
        if not cyc.is_low_liquidity or not (start <= cyc.first_breakout_date <= end):
            continue
        i0 = bisect_dates(td.dates, cyc.first_breakout_date)
        stop_idx = (bisect_dates(td.dates, cyc.end_date, right=True) if cyc.end_date
                    else bisect_dates(td.dates, end, right=True))
        reach_idx = next((j for j in range(i0, stop_idx)
                          if core.is_buy_liquid(td.tv20[j])), None)
        row = {
            "ticker": res.ticker, "name": res.name, "mode": res.mode,
            "first_breakout_date": cyc.first_breakout_date,
            "first_breakout_threshold": cyc.first_breakout_threshold,
            "first_breakout_atr": cyc.first_breakout_atr,
            "first_breakout_turnover_20": cyc.first_breakout_turnover,
            "first_breakout_close": cyc.first_breakout_close,
            "first_breakout_chase_ok": cyc.first_breakout_chase_ok,
            "cycle_end_date": cyc.end_date,
            "first_5eok_date": td.dates[reach_idx] if reach_idx is not None else None,
            "first_5eok_close": td.close[reach_idx] if reach_idx is not None else float("nan"),
            "first_5eok_gap_atr": (late_entry_gap_atr(td.close[reach_idx], cyc)
                                   if reach_idx is not None else None),
        }
        base = _first_trade_in_cycle(res.trades.get(STRATEGY_BASELINE, []), cyc, end)
        row.update({
            "baseline_signal_date": base.signal_date if base else None,
            "baseline_entry_date": base.entry_date if base else None,
            "baseline_entry_price": base.entry_price if base else float("nan"),
            "baseline_entry_gap_atr": base.cycle_gap_atr if base else None,
            "baseline_r": base.r_multiple if base and not base.open_at_end else float("nan"),
            "baseline_net_pnl": base.net_pnl if base and not base.open_at_end else float("nan"),
            "baseline_exit_verdict": base.exit_verdict if base else "",
            "baseline_open_at_end": base.open_at_end if base else None,
        })
        fwd = _fwd_stats(td, base.entry_idx, base.entry_price) if base else {}
        for h in FWD_HORIZONS:
            row[f"ret_{h}d_pct"] = fwd.get(f"ret_{h}d_pct", float("nan"))
        for h in (5, 10):
            row[f"mfe_{h}d_pct"] = fwd.get(f"mfe_{h}d_pct", float("nan"))
            row[f"mae_{h}d_pct"] = fwd.get(f"mae_{h}d_pct", float("nan"))
        skip = _first_trade_in_cycle(res.trades.get(STRATEGY_SKIP, []), cyc, end)
        guard = _first_trade_in_cycle(res.trades.get(STRATEGY_GUARD, []), cyc, end)
        row["skip_entered"] = skip is not None
        row["guard_entered"] = guard is not None
        row["guard_entry_date"] = guard.entry_date if guard else None
        rows.append(row)
    return rows


# ── 데이터 로드 (구간 가드 적용) ────────────────────────────────

def load_universe_frames(tickers: Optional[list[str]] = None,
                         limit: Optional[int] = None) -> dict[str, pd.DataFrame]:
    """backtest_ohlcv.parquet → 종목별 DataFrame. 현재 구간 상한 이후는 잘라낸다.

    구간 안에서 20일 평균 거래대금이 한 번이라도 3억 이상이었던 종목만 남긴다
    (그 아래는 감시·매수 어느 쪽에도 안 걸리므로 결과에 영향이 없다).
    """
    full = bt._load_full_raw()
    if tickers:
        full = full[full["티커"].isin(tickers)]
    full = full.copy()
    full["날짜"] = pd.to_datetime(full["날짜"].astype(str), format="%Y%m%d")
    full = full[full["종가"] > 0]
    if bt._PERIOD_MAX is not None:
        full = full[full["날짜"] <= bt._PERIOD_MAX]
    out: dict[str, pd.DataFrame] = {}
    for t, g in full.sort_values(["티커", "날짜"]).groupby("티커", sort=True):
        g = g.set_index("날짜")[["시가", "고가", "저가", "종가", "거래량"]]
        tv20 = (g["종가"] * g["거래량"]).rolling(TURNOVER_DAYS).mean()
        if not (tv20 >= WATCH_MIN_TURNOVER).any():
            continue
        out[str(t)] = g
        if limit and len(out) >= limit:
            break
    return out


def load_names() -> dict[str, str]:
    try:
        uni = screener.load_universe()
    except Exception:                       # noqa: BLE001
        return {}
    if uni is None:
        return {}
    return {t: str(n) for t, n in uni["종목명"].items()}


# ── 실행·보고 ────────────────────────────────────────────────

def _pct(xs, q):
    return float(np.percentile(xs, q)) if xs else float("nan")


def run_study(frames: dict[str, pd.DataFrame], mode: str, start: date, end: date, *,
              names: Optional[dict] = None, risk: Optional[float] = None,
              progress: Optional[Callable[[int, int, str], None]] = None) -> dict:
    names = names or {}
    results: list[TickerResult] = []
    for k, (t, df) in enumerate(frames.items(), 1):
        if progress:
            progress(k, len(frames), t)
        try:
            results.append(simulate_ticker(t, df, mode, start, end,
                                           name=names.get(t, ""), risk=risk))
        except Exception as e:              # noqa: BLE001
            logger.warning("[%s] 시뮬레이션 실패 - 건너뜀: %s", t, e)
    trades = {s: [x for r in results for x in r.trades[s]] for s in STRATEGIES}
    events = [row for r in results for row in low_liquidity_events(r, start, end)]
    return {"results": results, "trades": trades, "events": events}


def summarize_events(events: list[dict], trades_by_strategy: dict) -> dict:
    n = len(events)
    reached = [e for e in events if e["first_5eok_date"] is not None]
    entered = [e for e in events if e["baseline_entry_date"] is not None]
    gaps = [e["baseline_entry_gap_atr"] for e in entered
            if e["baseline_entry_gap_atr"] is not None]
    lim = core.CHASE_ATR_MULT
    late = [e for e in entered if e["baseline_entry_gap_atr"] is not None
            and e["baseline_entry_gap_atr"] > lim]
    near = [e for e in entered if e["baseline_entry_gap_atr"] is not None
            and e["baseline_entry_gap_atr"] <= lim]

    def _grp(rows):
        rs = [e["baseline_r"] for e in rows if not math.isnan(e["baseline_r"])]
        return {"n": len(rows), "closed": len(rs),
                "avg_r": sum(rs) / len(rs) if rs else float("nan"),
                "se": statistics.stdev(rs) / math.sqrt(len(rs)) if len(rs) > 1 else float("nan"),
                "win": sum(1 for e in rows if not math.isnan(e["baseline_net_pnl"])
                           and e["baseline_net_pnl"] > 0) / len(rs) * 100 if rs else float("nan")}

    return {
        "events": n, "tickers": len({e["ticker"] for e in events}),
        "reached_5eok": len(reached), "baseline_entered": len(entered),
        "gap_mean": float(np.mean(gaps)) if gaps else float("nan"),
        "gap_median": float(np.median(gaps)) if gaps else float("nan"),
        "gap_p25": _pct(gaps, 25), "gap_p75": _pct(gaps, 75),
        "late_ratio": len(late) / len(entered) * 100 if entered else float("nan"),
        "late": _grp(late), "near": _grp(near),
        "skip_entered": sum(1 for e in events if e["skip_entered"]),
        "guard_entered": sum(1 for e in events if e["guard_entered"]),
    }


def print_case(res_by_ticker: dict[str, TickerResult], events: list[dict],
               ticker: str, mode: str, period_name: str, period_end: date,
               data_last: Optional[date]) -> None:
    print(f"\n=== 사례: {ticker} {load_names().get(ticker, '')} "
          f"(목표일 {CASE_DATE} 전후, 모드 {mode}, 구간 {period_name}) ===")
    res = res_by_ticker.get(ticker)
    if res is None:
        print(f"  {ticker}: 현재 구간 데이터에 없음 (또는 20일 평균 거래대금이 한 번도 3억을 "
              f"넘지 않음) - 확인 불가")
    else:
        print(f"  데이터 범위: {res.td.dates[0]} ~ {res.td.dates[-1]}")
    limit = min(d for d in (period_end, data_last) if d is not None) if data_last else period_end
    if CASE_DATE > limit:
        why = (f"--period {period_name} 상한 {period_end}" if CASE_DATE > period_end
               else f"백테스트 데이터 최종일 {data_last}")
        print(f"  {CASE_DATE}: 데이터 범위 밖 ({why}). 추정하지 않는다.")
    elif res is not None:
        td = res.td
        lo = max(0, bisect_dates(td.dates, CASE_DATE) - CASE_WINDOW_DAYS)
        hi = min(len(td.dates), bisect_dates(td.dates, CASE_DATE) + CASE_WINDOW_DAYS + 1)
        hits = [i for i in range(lo, hi) if res.signals[i].triggered]
        if not hits:
            print(f"  {CASE_DATE} ±{CASE_WINDOW_DAYS}거래일 안에 20일 돌파 없음")
        for i in hits:
            s = res.signals[i]
            print(f"  {td.dates[i]} 돌파: 돌파선 {s.threshold:,.0f} · ATR {s.atr:,} · "
                  f"20일 평균 거래대금 {s.turnover_20/1e8:.2f}억 ({liquidity_band(s.turnover_20)})")
    tick_events = [e for e in events if e["ticker"] == ticker]
    if tick_events:
        print(f"  구간 안 3~5억 최초 돌파 사이클 {len(tick_events)}건:")
        for e in tick_events:
            g = e["baseline_entry_gap_atr"]
            base = "-"
            if e["baseline_entry_date"] is not None:
                base = f"{e['baseline_entry_date']} @ {e['baseline_entry_price']:,.0f}"
                if g is not None:
                    base += f" (최초 돌파선 대비 {g:+.2f} ATR)"
            print(f"   · 최초돌파 {e['first_breakout_date']} 돌파선 {e['first_breakout_threshold']:,.0f} "
                  f"ATR {e['first_breakout_atr']} 거래대금 {e['first_breakout_turnover_20']/1e8:.2f}억"
                  f" | 5억 도달 {e['first_5eok_date'] or '-'}"
                  f" | baseline 진입 {base}"
                  f" | skip 진입 {'O' if e['skip_entered'] else 'X'}"
                  f" | guard 진입 {'O' if e['guard_entered'] else 'X'}"
                  f" | 사이클 종료 {e['cycle_end_date'] or '진행중'}")
    elif res is not None:
        print("  구간 안 3~5억 최초 돌파 사이클 없음")


def trades_frame(trades_by_strategy: dict) -> pd.DataFrame:
    rows = []
    for s, ts in trades_by_strategy.items():
        for t in ts:
            rows.append({
                "strategy": s, "mode": t.mode, "ticker": t.ticker, "name": t.name,
                "signal_date": t.signal_date, "entry_date": t.entry_date,
                "entry_price": round(t.entry_price, 1), "entry_atr": t.entry_atr,
                "unit_shares": t.unit_shares, "entry_turnover_20": round(t.entry_turnover_20),
                "entry_gap_atr": round(t.entry_gap_atr, 4), "liquidity_band": t.band,
                "cycle_first_breakout_date": t.cycle_first_breakout_date,
                "cycle_gap_atr": None if t.cycle_gap_atr is None else round(t.cycle_gap_atr, 4),
                "exit_date": t.exit_date, "exit_price": t.exit_price,
                "exit_verdict": t.exit_verdict, "final_units": t.final_units,
                "holding_days": t.holding_days, "net_pnl": round(t.net_pnl),
                "r_multiple": round(t.r_multiple, 4), "first_reach": t.first_reach,
                "open_at_end": t.open_at_end,
            })
    return pd.DataFrame(rows)


def output_paths(period: str, mode: str, ticker: Optional[str]) -> tuple[Path, Path]:
    """기본 실행(dev·intraday·전종목)은 data/bt_liquidity_entry_study_dev.csv,
    data/bt_low_liquidity_breakouts.csv. 다른 조합은 덮어쓰지 않게 접미사를 붙인다."""
    suffix = ("" if mode == "intraday" else f"_{mode}") + (f"_{ticker}" if ticker else "")
    study = bt.DATA_DIR / f"bt_liquidity_entry_study_{period}{suffix}.csv"
    ev_period = "" if period == "dev" else f"_{period}"
    events = bt.DATA_DIR / f"bt_low_liquidity_breakouts{ev_period}{suffix}.csv"
    return study, events


def report(study: dict, mode: str, period_name: str, start: date, end: date) -> dict:
    trades = study["trades"]
    lim = core.CHASE_ATR_MULT
    print(f"\n{'=' * 78}\n유동성 진입 연구 | 모드 {mode.upper()} | 구간 {period_name} ({start} ~ {end})"
          f"\n{'=' * 78}")
    print(f"감시 하한 {WATCH_MIN_TURNOVER/1e8:.0f}억 · 매수 하한 {BUY_MIN_TURNOVER/1e8:.0f}억 · "
          f"추격 한도 core.CHASE_ATR_MULT={lim} · 슬리피지 {bt.SLIPPAGE*100:.1f}% · "
          f"비용 {bt.COST_RATE*100:.2f}% · 명목계좌 {bt.STARTING_CAPITAL:,.0f}원(고정 유닛, 종목별 독립)")
    print(f"유니버스: {len(study['results'])}종목 (구간 중 20일 평균 거래대금 3억 이상이었던 적 있는 종목)")

    overall = {s: compute_study_metrics(trades[s]) for s in STRATEGIES}
    print_metrics_block(f"[{mode}] 전체", overall)
    cohort = {s: compute_study_metrics([t for t in trades[s] if t.band == BAND_3_5])
              for s in STRATEGIES}
    print_metrics_block(f"[{mode}] {BAND_3_5} (최초 돌파 당시 3~5억 사이클 안의 진입)", cohort)
    print("※ 명목계좌는 현금 제약 없이 고정 유닛을 계속 잡는다(--capital-mode fixed와 같은 성격). "
          "누적 손익이 1000만원을 넘게 마이너스면 자산이 음수가 되고 총 수익률 %·MDD %는 의미가 "
          "깨진다 - 그때는 거래수·승률·평균R(±SE)·expectancy·PF·MDD 원으로 비교한다.")

    print(f"\n--- [{mode}] 유동성 구간별 (최초 돌파 당시 20일 평균 거래대금 기준) ---")
    hdr = f"{'strategy':<38}{'band':<26}{'거래':>6}{'승률':>7}{'평균R':>8}{'±SE':>7}{'medR':>8}" \
          f"{'기대값원':>11}{'PF':>6}{'MDD%':>7}{'손절':>6}{'보유':>6}{'+.5↑':>7}{'-1↓':>7}"
    print(hdr)
    for s in STRATEGIES:
        for b in REPORT_BANDS:
            m = compute_study_metrics([t for t in trades[s] if t.band == b])
            print(f"{s:<38}{b:<26}{m['거래수']:>6}{_fmt('{:.1f}', m['승률']):>7}"
                  f"{_fmt('{:.3f}', m['평균R']):>8}{_fmt('{:.3f}', m['R_SE']):>7}"
                  f"{_fmt('{:.3f}', m['중앙R']):>8}"
                  f"{_fmt('{:,.0f}', m['기대값원']):>11}{_fmt('{:.2f}', m['PF']):>6}"
                  f"{_fmt('{:.1f}', m['MDD비율']):>7}{m['손절수']:>6}"
                  f"{_fmt('{:.1f}', m['평균보유일']):>6}{_fmt('{:.1f}', m['+0.5ATR선도달']):>7}"
                  f"{_fmt('{:.1f}', m['-1ATR선도달']):>7}")

    ev = summarize_events(study["events"], trades)
    print(f"\n--- [{mode}] 3~5억 최초 돌파 표본 요약 ---")
    print(f" 1. 3~5억 최초 돌파 사이클: {ev['events']}건 ({ev['tickers']}종목)")
    print(f" 2. 그중 사이클 안에서 20일 평균 5억 이상 도달: {ev['reached_5eok']}건")
    print(f" 3. 그중 baseline이 실제 진입: {ev['baseline_entered']}건")
    print(f" 4. baseline 진입가의 최초 돌파선 대비 거리(ATR): 평균 {_fmt('{:.2f}', ev['gap_mean'])} · "
          f"중앙 {_fmt('{:.2f}', ev['gap_median'])} · p25 {_fmt('{:.2f}', ev['gap_p25'])} · "
          f"p75 {_fmt('{:.2f}', ev['gap_p75'])}")
    print(f" 5. +{lim} ATR보다 늦게 진입한 비율: {_fmt('{:.1f}', ev['late_ratio'])}%")
    for key, label in (("late", f"6. +{lim} ATR 초과(늦은) 진입군"),
                       ("near", f"7. +{lim} ATR 이내 진입군")):
        g = ev[key]
        print(f" {label}: {g['n']}건(청산 {g['closed']}) · 평균R {_fmt('{:.3f}', g['avg_r'])}"
              f" ±{_fmt('{:.3f}', g['se'])}(SE) · "
              f"승률 {_fmt('{:.1f}', g['win'])}%")
    print(f"    (참고) 같은 사이클에서 skip 진입 {ev['skip_entered']}건 · guard 진입 {ev['guard_entered']}건")

    print(f"\n--- [{mode}] 8. baseline 대비 변화 (전체 거래 기준) ---")
    print("  ※ 기대값이 음수인 체계에서는 거래를 빼기만 해도 총손익·MDD 원이 줄어든다 - "
          "필터의 질은 거래당 지표(평균R·expectancy·PF) 변화로 본다.")
    base = overall[STRATEGY_BASELINE]
    for s in (STRATEGY_SKIP, STRATEGY_GUARD):
        m = overall[s]
        d_n = m["거래수"] - base["거래수"]
        print(f" {s}: 거래 {d_n:+,}건({_fmt('{:+.1f}', d_n / base['거래수'] * 100 if base['거래수'] else float('nan'))}%) · "
              f"평균R {_fmt('{:+.4f}', m['평균R'] - base['평균R'])} · "
              f"expectancy {_fmt('{:+,.0f}', m['기대값원'] - base['기대값원'])}원 · "
              f"총손익 {_fmt('{:+,.0f}', m['총손익'] - base['총손익'])}원 · "
              f"MDD {_fmt('{:+,.0f}', m['MDD금액'] - base['MDD금액'])}원"
              f"({_fmt('{:+.1f}', m['MDD비율'] - base['MDD비율'])}%p) · "
              f"PF {_fmt('{:+.3f}', m['PF'] - base['PF'])}")
    return {"overall": overall, "cohort": cohort, "events": ev}


def run_cli(args, start: date, end: date, *, bt_module=None,
            log_holdout: Optional[Callable[[str, dict], None]] = None) -> int:
    """backtest.py --liquidity-entry-study 진입점.

    bt_module: 실행 중인 backtest 모듈. `python backtest.py`로 돌면 그 모듈은
    __main__이라, 여기서 import한 backtest와 다른 객체다 - set_period가 건 구간
    가드가 실제로 적용되도록 실행 중인 모듈을 넘겨받아 쓴다.
    """
    global bt
    if bt_module is not None:
        bt = bt_module
    period_name = bt._PERIOD_NAME or "?"
    mode = args.mode
    ticker = args.ticker.zfill(6) if getattr(args, "ticker", None) else None
    tickers = [ticker] if ticker else None

    frames = load_universe_frames(tickers, limit=getattr(args, "limit", None))
    full = bt._load_full_raw()
    data_last = None
    if ticker is not None:
        sub = full.loc[full["티커"] == ticker, "날짜"]
        data_last = pd.to_datetime(sub.astype(str), format="%Y%m%d").max().date() if len(sub) else None
    names = load_names()

    def _progress(k, n, t):
        if k % 200 == 0 or k == n:
            logger.warning("진행률 %d/%d (%s)", k, n, t)

    study = run_study(frames, mode, start, end, names=names, risk=args.risk, progress=_progress)
    summary = report(study, mode, period_name, start, end)

    res_by_ticker = {r.ticker: r for r in study["results"]}
    case_ticker = ticker or CASE_TICKER
    if ticker is None:
        sub = full.loc[full["티커"] == case_ticker, "날짜"]
        data_last = pd.to_datetime(sub.astype(str), format="%Y%m%d").max().date() if len(sub) else None
    print_case(res_by_ticker, study["events"], case_ticker, mode, period_name,
               bt.PERIOD_BOUNDS[period_name][1] if period_name in bt.PERIOD_BOUNDS else end,
               data_last)

    study_csv, events_csv = output_paths(period_name, mode, ticker)
    trades_frame(study["trades"]).to_csv(study_csv, index=False, encoding="utf-8-sig")
    pd.DataFrame(study["events"]).to_csv(events_csv, index=False, encoding="utf-8-sig")
    print(f"\n거래 상세 저장: {study_csv}")
    print(f"저유동 최초 돌파 이벤트 저장: {events_csv} ({len(study['events'])}행)")

    if log_holdout is not None:
        ov = summary["overall"]
        log_holdout("liquidity-entry-study", {
            "mode_entry": mode,
            **{f"{s}_거래수": ov[s]["거래수"] for s in STRATEGIES},
            **{f"{s}_평균R": round(ov[s]["평균R"], 4) for s in STRATEGIES},
        })
    return 0
