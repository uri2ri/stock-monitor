"""
core.py – 포트폴리오 점검 계산 로직
웹앱과 공유할 수 있도록 I/O 없이 순수 계산만 담당한다.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

from pykrx import stock as krx

logger = logging.getLogger(__name__)

# ── 상관군 상수 ─────────────────────────────────────────────
SEMICONDUCTOR_GROUP: set[str] = {
    "005930",  # 삼성전자
    "000660",  # SK하이닉스
    "042700",  # 한미반도체
    "403870",  # HPSP
    "036930",  # 주성엔지니어링
    "460850",  # 한화비전
    "058470",  # 리노공업
    "357780",  # 솔브레인
    "185750",  # 종근당
    "222160",  # NPX반도체
    "131970",  # 테스나
}

# ── ATR 계산 ────────────────────────────────────────────────

ATR_LOOKBACK_DAYS = 30   # OHLCV 조회 기간 (거래일)
ATR_PERIOD = 20          # ATR 계산에 사용할 TR 개수


def _get_ohlcv(ticker: str, lookback_calendar_days: int = 60) -> "pd.DataFrame":
    """pykrx로 OHLCV를 가져온다.
    거래일 기준 ATR_LOOKBACK_DAYS 이상 확보하기 위해
    달력일 기준으로 넉넉히 조회한다.
    """
    import pandas as pd  # noqa: F811 – lazy import

    end = date.today()
    start = end - timedelta(days=lookback_calendar_days)
    df = krx.get_market_ohlcv(
        start.strftime("%Y%m%d"),
        end.strftime("%Y%m%d"),
        ticker,
    )
    if df.empty:
        raise ValueError(f"[{ticker}] OHLCV 데이터 없음")
    return df


def calc_atr(ticker: str) -> tuple[float, float]:
    """ATR과 현재(최근 종가)가를 반환한다.

    Returns:
        (atr, current_price)
    """
    df = _get_ohlcv(ticker)

    # pykrx 컬럼: 시가, 고가, 저가, 종가, 거래량, ...
    highs = df["고가"].values
    lows = df["저가"].values
    closes = df["종가"].values

    if len(closes) < ATR_PERIOD + 1:
        raise ValueError(
            f"[{ticker}] 데이터 부족: {len(closes)}일 < 필요 {ATR_PERIOD + 1}일"
        )

    # TR 계산 (첫 날은 전일종가 없으므로 제외)
    trs: list[float] = []
    for i in range(1, len(closes)):
        prev_close = closes[i - 1]
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - prev_close),
            abs(lows[i] - prev_close),
        )
        trs.append(float(tr))

    # 최근 ATR_PERIOD개 TR의 단순평균
    atr = sum(trs[-ATR_PERIOD:]) / ATR_PERIOD
    current_price = float(closes[-1])
    return atr, current_price


# ── 포지션 계산 (터틀 방식) ─────────────────────────────────


@dataclass
class HoldingInput:
    """노션 DB에서 읽은 종목 한 건."""

    ticker: str           # 종목코드 (6자리)
    name: str             # 종목명
    market: str           # 시장 (KOSPI / KOSDAQ)
    buy_price: float      # 매수단가
    shares: int           # 보유수량
    take_profit_1: Optional[float] = None   # 1차 익절가
    take_profit_2: Optional[float] = None   # 2차 익절가
    reeval_date: Optional[date] = None      # 재평가 기한
    # 노션에 저장된 기존 값 (없으면 None)
    prev_trailing_high: Optional[float] = None  # 진입후 최고가
    prev_stop_loss: Optional[float] = None      # 손절선


@dataclass
class HoldingResult:
    """한 종목의 계산 결과."""

    ticker: str
    name: str
    current_price: float = 0.0
    atr: float = 0.0
    unit_shares: int = 0         # 1유닛 주수
    trailing_high: float = 0.0   # 진입후 최고가 (갱신 후)
    stop_loss: float = 0.0       # 손절선 (갱신 후)
    risk_amount: float = 0.0     # 종목 리스크액
    current_units: float = 0.0   # 현재 유닛수
    verdict: str = ""            # 판정
    verdict_memo: str = ""       # 판정 메모
    error: Optional[str] = None  # 조회 실패 시 메시지

    @property
    def is_action_needed(self) -> bool:
        return self.verdict not in ("유지", "", "조회실패")


def evaluate_holding(
    inp: HoldingInput,
    total_capital: float,
) -> HoldingResult:
    """한 종목을 평가한다. 예외 시 error가 채워진 Result를 반환."""
    result = HoldingResult(ticker=inp.ticker, name=inp.name)

    try:
        atr, current_price = calc_atr(inp.ticker)
    except Exception as e:
        logger.exception("[%s] 시세 조회 실패", inp.ticker)
        result.error = str(e)
        result.verdict = "조회실패"
        result.verdict_memo = f"시세 조회 실패: {e}"
        return result

    result.atr = round(atr, 2)
    result.current_price = current_price

    # 1유닛 주수 (내림)
    if atr > 0:
        result.unit_shares = math.floor((total_capital * 0.01) / atr)
    else:
        result.unit_shares = 0

    # 진입후 최고가
    if inp.prev_trailing_high is not None:
        result.trailing_high = max(inp.prev_trailing_high, current_price)
    else:
        result.trailing_high = max(inp.buy_price, current_price)

    # 손절선 = 진입후최고가 - 2*ATR
    new_stop = result.trailing_high - (2 * atr)

    # 트레일링: 손절선은 올라가기만 한다
    if inp.prev_stop_loss is not None and inp.prev_stop_loss > new_stop:
        result.stop_loss = round(inp.prev_stop_loss, 2)
    else:
        result.stop_loss = round(new_stop, 2)

    # 종목 리스크액
    result.risk_amount = (current_price - result.stop_loss) * inp.shares

    # 현재 유닛수
    if result.unit_shares > 0:
        result.current_units = round(inp.shares / result.unit_shares, 2)
    else:
        result.current_units = 0.0

    # ── 판정 (위에서부터 첫 번째로 걸리는 것) ──
    if current_price <= result.stop_loss:
        result.verdict = "손절"
        result.verdict_memo = (
            f"현재가 {current_price:,.0f} ≤ 손절선 {result.stop_loss:,.0f}"
        )
    elif inp.take_profit_2 is not None and current_price >= inp.take_profit_2:
        result.verdict = "분할익절 2차"
        result.verdict_memo = (
            f"현재가 {current_price:,.0f} ≥ 2차 익절가 {inp.take_profit_2:,.0f}"
        )
    elif inp.take_profit_1 is not None and current_price >= inp.take_profit_1:
        result.verdict = "분할익절 1차"
        result.verdict_memo = (
            f"현재가 {current_price:,.0f} ≥ 1차 익절가 {inp.take_profit_1:,.0f}"
        )
    elif inp.reeval_date is not None and inp.reeval_date < date.today():
        result.verdict = "기한도래"
        result.verdict_memo = f"재평가 기한 {inp.reeval_date} 경과"
    else:
        result.verdict = "유지"
        result.verdict_memo = ""

    return result


# ── 전체 리스크 ─────────────────────────────────────────────


@dataclass
class PortfolioRisk:
    """전체 포트폴리오 리스크 요약."""

    total_risk_amount: float = 0.0
    total_risk_pct: float = 0.0
    semi_risk_amount: float = 0.0
    semi_risk_pct: float = 0.0
    risk_warning: bool = False     # 6% 초과
    semi_warning: bool = False     # 상관군 경고 (참고용)


def calc_portfolio_risk(
    results: list[HoldingResult],
    total_capital: float,
) -> PortfolioRisk:
    """전체 리스크와 반도체 상관군 리스크를 계산한다."""
    pr = PortfolioRisk()

    for r in results:
        if r.error:
            continue
        pr.total_risk_amount += r.risk_amount
        if r.ticker in SEMICONDUCTOR_GROUP:
            pr.semi_risk_amount += r.risk_amount

    if total_capital > 0:
        pr.total_risk_pct = round(
            (pr.total_risk_amount / total_capital) * 100, 2
        )
        pr.semi_risk_pct = round(
            (pr.semi_risk_amount / total_capital) * 100, 2
        )

    pr.risk_warning = pr.total_risk_pct > 6.0
    return pr
