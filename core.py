"""
core.py – 터틀 트레이딩 계산 로직

시세 조회(fetch_ohlcv)를 제외한 모든 함수는 DataFrame과 숫자만 받아
계산하는 순수 함수다. 아침 리포트(daily_report.py)와 웹앱(app.py)이
이 모듈을 공유하며, 계산을 다시 구현하지 않는다.

CLI 테스트:
    python core.py 240550
    python core.py 240550 --capital 10000000 --end 2026-04-04
"""

from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

import pandas as pd
from pykrx import stock as krx

logger = logging.getLogger(__name__)

# 상관군은 하드코딩하지 않는다. 노션 '상관군' 칸의 텍스트를 그대로 읽어
# 같은 값끼리 합산한다 (빈 값이면 상관군 없음).

# ── 계산 상수 ───────────────────────────────────────────────

ATR_PERIOD = 20          # ATR(N) 계산에 사용할 TR 개수
ATR_METHOD = "wilder"    # 기본 ATR 방식 (wilder | sma)
EDGE_DAYS = 20           # 기대값 계산에 쓰는 일간수익률 개수
HIGH_PERIOD = 20         # 진입 트리거: 20일 고가
LOW_PERIOD = 10          # 청산 트리거: 10일 저가
RISK_PER_TRADE = 0.01    # 1회 리스크 = 총자본의 1%
STOP_ATR_MULT = 2.0      # 손절폭 = 2×ATR
STOP_METHOD = "half"     # 기본 트레일링 방식 (half | 1to1)
PYRAMID_ATR_STEP = 0.5   # 1/2 ATR 오를 때마다 1유닛 추가
CHASE_ATR_MULT = 0.5     # 돌파 후 이만큼 더 진행했으면 추격 구간으로 본다
MAX_UNITS = 4            # 종목당 최대 유닛 수
MAX_UNITS_GROUP = 6      # 상관군당 최대 유닛 수
MAX_UNITS_TOTAL = 12     # 전체 포트폴리오 최대 유닛 수
DEFAULT_CAPITAL = 10_000_000
STOP_UPDATE_ATR_MULT = 0.5   # 손절선 갱신 알림 임계값 (× 진입시 ATR)
FETCH_DAYS = 120         # 기본 조회 거래일 – Wilder 재귀식 워밍업용

# 노션 점검표 '운용' 칸이 이 값이면 터틀 진입이 아니다 (하이닉스 등
# 별도 전략 보유분). mailer.py(R배수 계산 제외)와 evening_audit.py
# (신호 판정 대상 제외)가 함께 쓴다 - 두 곳에 문자열을 따로 두면 나중에
# 어느 한쪽만 바뀌는 사고가 난다.
MANAGED_NON_TURTLE = "터틀외"
MIN_TRADING_DAYS = 40    # 이보다 적으면 조회 실패로 본다

# ── 스크리너·스캔 공통 임계값 (표 색상 강조에도 쓰인다) ────────
VOL_MULT_MIN = 1.5      # 오늘 거래량 / 직전 20일 평균 거래량
VOL_AVG_DAYS = 20        # 거래량배수 계산에 쓰는 평균일수 (당일 제외)
# 전종목 스캔 1,108종목(2026-08-05, 돌파+임박 합계) 기준 ATR% 10분위수가
# 4.9%였다. 그보다 낮게 잡아 하위 ~5%만 회색 처리하는 넉넉한 초기값 —
# 실데이터 더 쌓이면 조정한다.
SCAN_ATR_PCT_MIN = 3.0


# ── 딥링크 ──────────────────────────────────────────────────

def build_stock_link(code: str) -> str:
    """스트림릿 종목분석 화면으로 바로 가는 URL. 카톡·메일이 공유한다.

    STREAMLIT_APP_URL 미설정이면 빈 문자열 – 호출부는 링크 없이
    표시해야 하며, 이 함수가 예외로 죽어서는 안 된다.
    """
    base = os.environ.get("STREAMLIT_APP_URL", "").strip().rstrip("/")
    if not base:
        return ""
    return f"{base}/?code={code}"


# ── 시세 조회·종목 메타 (I/O) ───────────────────────────────

def fetch_ohlcv(
    code: str,
    days: int = FETCH_DAYS,
    end: Optional[date] = None,
) -> pd.DataFrame:
    """pykrx로 최근 `days` 거래일의 OHLCV를 가져온다.

    거래일 수를 확보하려면 달력일로는 더 넓게 조회해야 하므로
    주말·공휴일을 감안해 넉넉히 요청한 뒤 뒤에서 days개만 남긴다.

    상장한 지 얼마 안 된 종목처럼 `days`를 못 채우면 있는 만큼 쓰되,
    MIN_TRADING_DAYS(40거래일) 미만이면 조회 실패로 처리한다.

    Args:
        code: 종목코드 6자리
        days: 확보할 거래일 수 (기본 120 – Wilder 워밍업)
        end: 기준일 (기본 오늘). 과거 검증용으로 지정할 수 있다.

    Returns:
        DataFrame (컬럼: 시가/고가/저가/종가/거래량, 인덱스: 날짜)

    Raises:
        ValueError: 데이터가 비었거나 거래일이 40일 미만일 때
    """
    end = end or date.today()
    # 거래일 ≈ 달력일 × 0.68 → 여유 있게 1.7배 + 15일
    start = end - timedelta(days=int(days * 1.7) + 15)

    df = krx.get_market_ohlcv(
        start.strftime("%Y%m%d"),
        end.strftime("%Y%m%d"),
        code,
    )
    if df is None or df.empty:
        raise ValueError(f"[{code}] OHLCV 데이터 없음")

    # 거래 정지일 등으로 0이 섞이면 계산이 왜곡되므로 제거
    df = df[df["종가"] > 0]
    if df.empty:
        raise ValueError(f"[{code}] 유효한 거래일 데이터 없음")

    df = df.tail(days)
    if len(df) < MIN_TRADING_DAYS:
        raise ValueError(
            f"[{code}] 거래일 부족: {len(df)}일 < 최소 {MIN_TRADING_DAYS}일"
        )
    return df


# 거래일 판정용 기준 종목. 항상 거래되는 초대형주라 상장폐지·거래정지로
# 흔들릴 위험이 사실상 없다.
TRADING_DAY_REFERENCE_TICKER = "005930"  # 삼성전자


def last_trading_date(
    reference_ticker: str = TRADING_DAY_REFERENCE_TICKER,
) -> Optional[date]:
    """가장 최근 실제 거래일. pykrx 데이터가 실제로 갖고 있는 마지막 날짜를
    그대로 쓴다 - 토·일요일은 물론 임시 휴장일까지 하드코딩 없이 걸러진다.

    조회 자체가 실패하면 None을 돌려준다 (호출부가 실패 시 동작을 정한다 -
    kis_client.py는 fail-open으로, evening_audit.py는 안전하게 건너뛰는
    쪽으로 쓴다).
    """
    try:
        df = fetch_ohlcv(reference_ticker)
        return df.index[-1].date()
    except Exception as e:                      # noqa: BLE001
        logger.error("거래일 판정용 시세 조회 실패: %s", e)
        return None


def fetch_etf_map() -> dict[str, str]:
    """상장 ETF 전체의 티커 → 종목명.

    pykrx의 공식 ETF 함수(get_etf_ticker_list/get_etf_ticker_name)는 KRX가
    로그인 뒤로 옮긴 전종목 통계 endpoint(MDCSTAT04601)를 쓴다. 이 앱은
    KRX_ID/KRX_PW를 설정하지 않으므로 그 경로는 항상 "LOGOUT" 응답으로
    실패한다 (screener.py가 전종목 조회에서 같은 문제를 겪는 것과 동일한
    원인). 대신 로그인이 필요 없는 종목검색(파인더) endpoint를 직접 써서
    ETF 코드→이름을 가져온다. 호출부가 캐시해서 매번 조회하지 않게 한다.
    """
    from pykrx.website.krx.etx.core import 상장종목검색

    df = 상장종목검색().fetch(market="ETF")
    return dict(zip(df["short_code"], df["codeName"]))


def latest_close(df: pd.DataFrame) -> float:
    """마지막 거래일 종가."""
    return float(df["종가"].iloc[-1])


# ── ATR ─────────────────────────────────────────────────────

def true_ranges(df: pd.DataFrame) -> list[float]:
    """일별 TR 목록.

        TR = max(고가-저가, abs(고가-전일종가), abs(저가-전일종가))

    첫 행은 전일종가가 없으므로 제외된다 (결과 길이 = len(df) - 1).
    """
    highs = df["고가"].tolist()
    lows = df["저가"].tolist()
    closes = df["종가"].tolist()

    trs: list[float] = []
    for i in range(1, len(closes)):
        prev_close = closes[i - 1]
        trs.append(
            float(
                max(
                    highs[i] - lows[i],
                    abs(highs[i] - prev_close),
                    abs(lows[i] - prev_close),
                )
            )
        )
    return trs


def round_atr(atr: float) -> int:
    """ATR을 정수로 반올림한다.

    화면 지표·유닛 진행표·노션 기록값이 항상 같아야 하므로, 이 시점부터
    모든 하위 계산(손절선·추가매수가·손실액)을 정수 ATR로 수행한다.
    실제 예약 주문과 매매일지에 들어가는 숫자라 소수점 차이가 남으면
    나중에 R배수 검증이 어긋난다.
    """
    return int(round(atr))


def calc_atr(
    df: pd.DataFrame,
    period: int = ATR_PERIOD,
    method: str = ATR_METHOD,
) -> float:
    """ATR(= 터틀 원본의 N).

    method="wilder" (기본, 터틀 원본 룰):

        N = (19 × 전일N + 당일TR) / 20        (period=20 일 때)

      초기값은 첫 `period`개 TR의 단순평균으로 잡고 이후 재귀 적용한다.
      과거 TR이 지수적으로 감쇠하며 계속 반영되므로, 같은 기준일이라도
      조회 구간(fetch_ohlcv의 days)이 길어지면 값이 조금씩 달라진다.

    method="sma" (옵션):

        최근 `period`개 TR의 단순평균. 정수 호가 기준이므로 결과는
        항상 1/period 배수로 떨어진다.

    Raises:
        ValueError: TR을 period개 만들 수 없을 때 (데이터 부족),
            또는 모르는 method
    """
    trs = true_ranges(df)
    if len(trs) < period:
        raise ValueError(
            f"데이터 부족: TR {len(trs)}개 < 필요 {period}개 "
            f"(거래일 {len(df)}일)"
        )

    if method == "sma":
        return sum(trs[-period:]) / period

    if method == "wilder":
        atr = sum(trs[:period]) / period          # 시드: 첫 period개 단순평균
        for tr in trs[period:]:
            atr = ((period - 1) * atr + tr) / period
        return atr

    raise ValueError(f"모르는 ATR 방식: {method!r} (wilder | sma)")


# ── 기대값 (최근 20일 일간수익률) ───────────────────────────


@dataclass
class Edge:
    """최근 일간수익률로 계산한 기대값. 단위는 %."""

    days: int = 0                # 계산에 쓴 일간수익률 개수
    win_days: int = 0
    loss_days: int = 0
    flat_days: int = 0           # 보합 – 승·패 어디에도 넣지 않는다
    win_rate: float = 0.0        # 승률 %
    loss_rate: float = 0.0       # 패율 % (승률+패율 < 100 일 수 있음)
    avg_win: float = 0.0         # 평균수익 % (양수)
    avg_loss: float = 0.0        # 평균손실 % (음수)
    te: float = 0.0              # 기대값 %
    rr: Optional[float] = None   # 손익비. 평균손실이 0이면 None


def calc_edge(df: pd.DataFrame, days: int = EDGE_DAYS) -> Edge:
    """최근 `days`일 일간수익률 기준 기대값.

        승률 = 상승일 / 전체일,  패율 = 하락일 / 전체일
        보합일은 승·패 어느 쪽에도 넣지 않는다 (합이 100%가 안 될 수 있음)
        TE = 승률 × 평균수익 - 패율 × abs(평균손실)
        RR = 평균수익 / abs(평균손실)

    Raises:
        ValueError: 일간수익률을 1개도 만들 수 없을 때
    """
    closes = df["종가"].tolist()
    if len(closes) < 2:
        raise ValueError(f"데이터 부족: 종가 {len(closes)}개 < 필요 2개")

    returns = [
        (closes[i] - closes[i - 1]) / closes[i - 1] * 100
        for i in range(1, len(closes))
    ][-days:]

    wins = [r for r in returns if r > 0]
    losses = [r for r in returns if r < 0]
    flats = [r for r in returns if r == 0]

    e = Edge(
        days=len(returns),
        win_days=len(wins),
        loss_days=len(losses),
        flat_days=len(flats),
    )
    e.win_rate = len(wins) / len(returns) * 100
    e.loss_rate = len(losses) / len(returns) * 100
    e.avg_win = sum(wins) / len(wins) if wins else 0.0
    e.avg_loss = sum(losses) / len(losses) if losses else 0.0

    # 비율은 소수(0~1)로 되돌려 계산해야 단위가 %로 유지된다
    e.te = (e.win_rate / 100) * e.avg_win - (e.loss_rate / 100) * abs(e.avg_loss)
    e.rr = (e.avg_win / abs(e.avg_loss)) if e.avg_loss != 0 else None
    return e


# ── 포지션 / 손절선 ─────────────────────────────────────────


@dataclass
class Position:
    """1유닛 사이징 결과."""

    atr: float = 0.0
    capital: float = 0.0
    risk_amount: float = 0.0     # 1회 리스크액 = 총자본 × risk
    unit_shares: int = 0         # 1유닛 주수 (내림)


def calc_position(
    atr: float,
    capital: float,
    risk: float = RISK_PER_TRADE,
) -> Position:
    """1유닛 주수 = (총자본 × risk) / ATR, 내림.

    ATR이 0 이하면 주수는 0으로 둔다 (추정하지 않는다).
    """
    risk_amount = capital * risk
    unit_shares = math.floor(risk_amount / atr) if atr > 0 else 0
    return Position(
        atr=atr,
        capital=capital,
        risk_amount=risk_amount,
        unit_shares=unit_shares,
    )


def calc_stop(
    entry: float,
    atr: float,
    trailing_high: Optional[float] = None,
    method: str = STOP_METHOD,
) -> float:
    """손절선을 계산한다.

    method="half" (기본, 노트 원문 방식 – 상승분의 1/2를 반납):

        손절선 = (진입가 - 2×ATR) + (진입후최고가 - 진입가) / 2

      최고가와의 간격이 2×ATR로 고정되지 않고 상승분의 절반만
      따라 올라가므로, 되돌림에 덜 털린다. 상승분이 4×ATR이 되면
      손절선이 진입가(본전)에 닿는다.

    method="1to1" (이전 방식, 비교용):

        손절선 = 진입후최고가 - 2×ATR

    Args:
        entry: 현재 유닛 기준가. 최초 진입가가 아니라 마지막 매수가다
            (추가매수하면 갱신되고 진입후최고가도 그 시점부터 다시 잡는다).
        atr: ATR
        trailing_high: 진입후최고가. None이면 진입가와 같다고 본다
            (= 진입 직후, 두 방식 모두 진입가 - 2×ATR).
        method: "half" | "1to1"

    Raises:
        ValueError: 모르는 method
    """
    high = entry if trailing_high is None else max(entry, trailing_high)

    if method == "half":
        return (entry - STOP_ATR_MULT * atr) + (high - entry) / 2
    if method == "1to1":
        return high - STOP_ATR_MULT * atr
    raise ValueError(f"모르는 손절선 방식: {method!r} (half | 1to1)")


def apply_trailing_stop(
    new_stop: float,
    prev_stop: Optional[float],
) -> float:
    """손절선은 올라가기만 한다. 새 값이 더 낮으면 기존값을 유지."""
    if prev_stop is not None and prev_stop > new_stop:
        return prev_stop
    return new_stop


def resolve_stop(
    entry: float,
    atr: float,
    trailing_high: Optional[float] = None,
    last_buy_price: Optional[float] = None,
    prev_stop: Optional[float] = None,
    method: str = STOP_METHOD,
) -> float:
    """두 기준을 합쳐 최종 손절선을 낸다.

    손절선 우선순위 (위에서부터 적용):

      1. 진입후최고가 기준 (calc_stop) 과 마지막매수가 기준
         (마지막매수가 - 2×ATR) 중 **항상 더 높은 값**을 쓴다.
         1유닛만 들고 주가가 3×ATR 이상 오른 상황처럼 두 기준이
         갈릴 때, 더 높은 쪽이 이익을 더 지키므로 우선한다.
      2. 그렇게 구한 값이 노션의 기존 손절선보다 낮으면 기존값을
         유지한다. 손절선은 절대 내려가지 않는다.

    Args:
        entry: 현재 유닛 기준가 (= 마지막 매수가)
        atr: ATR
        trailing_high: 진입후최고가
        last_buy_price: 마지막 매수가. None이면 entry와 같다고 본다
        prev_stop: 노션에 기록된 기존 손절선
        method: 트레일링 방식 ("half" | "1to1")
    """
    high_basis = calc_stop(entry, atr, trailing_high, method=method)

    # 마지막매수가 기준: 추가매수할 때마다 전체 유닛의 손절선을
    # (마지막 매수가 - 2×ATR)로 올린다는 피라미딩 규칙에서 나온다.
    last_buy = entry if last_buy_price is None else last_buy_price
    last_buy_basis = last_buy - STOP_ATR_MULT * atr

    return apply_trailing_stop(max(high_basis, last_buy_basis), prev_stop)


# ── 피라미딩 ────────────────────────────────────────────────


@dataclass
class PyramidStep:
    """추가매수 단계 한 칸."""

    unit: int                    # 1~4
    buy_price: float             # 이 단계 매수가
    shares: int                  # 이 단계에서 사는 주수 (1유닛)
    stop_loss: float             # 이 단계 진입 후 전체 유닛에 적용되는 손절선
    cum_shares: int              # 누적 주수
    cum_cost: float              # 누적 투입금액
    cum_weight_pct: float        # 누적 투입비중 (총자본 대비 %)
    loss_if_stopped: float       # 이 단계에서 손절 시 총손실액
    loss_pct: float              # 총손실액의 총자본 대비 %


def build_pyramid_from_prices(
    buy_prices: list[float],
    atr: float,
    unit_shares: int,
    capital: float = DEFAULT_CAPITAL,
) -> list[PyramidStep]:
    """지정된 매수가 목록으로 유닛 진행표를 만든다.

    build_pyramid()의 하위 구현이다. 표준 0.5×ATR 그리드(진입가 하나에서
    전부 파생)가 아니라 실제/추정 매수가를 섞어 써야 할 때(예: 보유중인
    종목은 1U만 매수단가를 쓰고 나머지는 마지막매수가 기준으로 계산)
    이 함수를 직접 쓴다 — 계산 로직(손절선·누적 주수·누적 투입·손실액)은
    build_pyramid()와 완전히 동일하다.

    추가매수할 때마다 손절선을 (마지막 매수가 - 2×ATR)로 함께 올리므로,
    이미 산 유닛들의 손절선도 같이 올라간다. 각 단계의 총손실액은
    그 단계 손절선을 전체 유닛에 적용해 계산한다.

    Args:
        buy_prices: 단계별 매수가 목록 (1번째가 1유닛)
        atr: ATR
        unit_shares: 1유닛 주수
        capital: 총자본 (투입비중·손실비중 계산용)

    Returns:
        단계별 PyramidStep 목록. ATR이 0 이하거나 목록이 비면 빈 목록.
    """
    if atr <= 0 or unit_shares <= 0 or not buy_prices:
        return []

    steps: list[PyramidStep] = []

    for unit, buy_price in enumerate(buy_prices, start=1):
        # 추가매수 시점에는 최고가 = 그 매수가이므로 calc_stop()의
        # half 방식이 곧 (매수가 - 2×ATR)와 같아진다 (trailing_high 생략).
        stop = round(calc_stop(buy_price, atr))

        cum_prices = buy_prices[:unit]
        cum_shares = unit_shares * unit
        cum_cost = sum(p * unit_shares for p in cum_prices)
        loss_if_stopped = sum((p - stop) * unit_shares for p in cum_prices)

        steps.append(
            PyramidStep(
                unit=unit,
                buy_price=buy_price,
                shares=unit_shares,
                stop_loss=stop,
                cum_shares=cum_shares,
                cum_cost=cum_cost,
                cum_weight_pct=(cum_cost / capital * 100) if capital > 0 else 0.0,
                loss_if_stopped=loss_if_stopped,
                loss_pct=(
                    (loss_if_stopped / capital * 100) if capital > 0 else 0.0
                ),
            )
        )

    return steps


def build_pyramid(
    entry: float,
    atr: float,
    unit_shares: int,
    capital: float = DEFAULT_CAPITAL,
    max_units: int = MAX_UNITS,
) -> list[PyramidStep]:
    """1/2 ATR 오를 때마다 1유닛씩 max_units까지 쌓는 계획표.

    표준 0.5×ATR 그리드를 만들어 build_pyramid_from_prices()에 넘긴다.
    계산 로직 자체는 그쪽에 있다 (재사용 — 다시 구현하지 않는다).

    Args:
        entry: 1유닛 진입가
        atr: ATR
        unit_shares: 1유닛 주수
        capital: 총자본 (투입비중·손실비중 계산용)
        max_units: 최대 유닛 수

    Returns:
        단계별 PyramidStep 목록. ATR이 0 이하면 빈 목록.
    """
    if atr <= 0 or unit_shares <= 0 or max_units <= 0:
        return []

    # 실제 주문에 넣는 값이라 정수로 맞춘다 (round_atr 참고)
    buy_prices = [
        round(entry + (unit - 1) * PYRAMID_ATR_STEP * atr)
        for unit in range(1, max_units + 1)
    ]
    return build_pyramid_from_prices(buy_prices, atr, unit_shares, capital)


# ── 추세 신호 ───────────────────────────────────────────────


@dataclass
class TrendSignal:
    """돌파/청산 트리거."""

    current_price: float = 0.0
    high_20: float = 0.0          # 최근 20일 고가 (당일 포함)
    low_10: float = 0.0           # 최근 10일 저가 (당일 포함)
    high_20_prev: float = 0.0     # 직전 20일 고가 (당일 제외) – 돌파 판정용
    low_10_prev: float = 0.0      # 직전 10일 저가 (당일 제외) – 청산 판정용
    breakout: bool = False        # 종가 > 직전 20일 고가
    exit_triggered: bool = False  # 종가 < 직전 10일 저가
    # 현재가에서 '청산선'(= low_10_prev)까지 남은 거리 %. 청산을 실제로
    # 발동시키는 선을 기준으로 재야 한다. 당일 포함 low_10으로 재면,
    # 당일 저가가 10일 최저를 새로 찍은 날에 여유가 실제보다 넉넉해 보인다.
    dist_to_exit_pct: float = 0.0


def trend_signals(
    df: pd.DataFrame,
    high_period: int = HIGH_PERIOD,
    low_period: int = LOW_PERIOD,
) -> TrendSignal:
    """20일 고가 / 10일 저가와 돌파 여부.

    돌파 판정은 '당일을 제외한' 직전 20일 고가와 비교한다.
    당일 고가를 포함하면 현재가가 그 값을 넘는 일이 구조적으로
    불가능해 돌파가 영원히 False가 되기 때문이다.
    표시용으로 당일 포함 값(high_20/low_10)도 함께 담는다.

    Raises:
        ValueError: 기간을 채울 데이터가 없을 때
    """
    if len(df) < high_period + 1:
        raise ValueError(
            f"데이터 부족: {len(df)}일 < 필요 {high_period + 1}일"
        )

    highs = df["고가"].tolist()
    lows = df["저가"].tolist()
    current = latest_close(df)

    sig = TrendSignal(
        current_price=current,
        high_20=float(max(highs[-high_period:])),
        low_10=float(min(lows[-low_period:])),
        high_20_prev=float(max(highs[-high_period - 1:-1])),
        low_10_prev=float(min(lows[-low_period - 1:-1])),
    )
    sig.breakout = current > sig.high_20_prev
    sig.exit_triggered = current < sig.low_10_prev
    sig.dist_to_exit_pct = (
        (current - sig.low_10_prev) / current * 100 if current > 0 else 0.0
    )
    return sig


# ── 스크리너·스캔 지표 ──────────────────────────────────────
#
# 오늘의 돌파(screener.py)와 전종목 스캔(scan_all.py)이 같은 정의를
# 쓰게 하려고 여기 모아둔다. 두 모듈이 각자 계산식을 다시 쓰면
# 조용히 갈라질 수 있다 (예: 평균일수 하나만 달라도 배수가 어긋난다).

def calc_vol_mult(hist: pd.DataFrame, avg_days: int = VOL_AVG_DAYS) -> Optional[float]:
    """거래량배수 = 당일 거래량 / 직전 avg_days일 평균 거래량.

    당일을 평균에서 뺀다 — 포함하면 급증한 당일이 스스로 기준을
    끌어올려 배수가 실제보다 작게 나온다 (20일 고가 돌파를 당일
    제외로 보는 것과 같은 이유).

    Returns:
        None: 데이터가 모자라거나 평균이 0 이하 (배수가 성립하지 않음)
    """
    volumes = hist["거래량"].tolist()
    if len(volumes) < avg_days + 1:
        return None
    today_vol = float(volumes[-1])
    prev = volumes[-avg_days - 1:-1]
    vol_avg = sum(prev) / len(prev) if prev else 0.0
    if vol_avg <= 0:
        return None
    return today_vol / vol_avg


def calc_atr_pct(atr: float, price: float) -> float:
    """ATR을 현재가 대비 %로. 원화 ATR은 종목 간 비교가 안 되므로 쓴다."""
    return atr / price * 100 if price > 0 else 0.0


def breakout_verdict(gap_atr: float, chase_mult: float = CHASE_ATR_MULT) -> str:
    """갭(×ATR) 하나로 규칙 판정 라벨을 낸다. 수익 가능성 예측이 아니다.

    - 대기: 아직 20일 고가를 못 넘음 (gap_atr <= 0)
    - 진입가능: 막 넘어선 구간 (0 < gap_atr <= chase_mult)
    - 추격금지: chase_mult를 넘게 더 진행 (돌파 시점에서 이미 벗어남)
    """
    if gap_atr <= 0:
        return "대기"
    if gap_atr <= chase_mult:
        return "진입가능"
    return "추격금지"


def gap_atr_key(row: dict) -> float:
    """갭(×ATR) 정렬 키. gap_atr 필드가 있으면 그대로 쓰고, 없으면(예전 스캔
    결과) price/high_20_prev/atr로부터 계산한다."""
    if "gap_atr" in row:
        return row["gap_atr"]
    atr = row.get("atr", 0)
    return (row.get("price", 0) - row.get("high_20_prev", 0)) / atr if atr else 0.0


def sort_by_gap(rows: list[dict]) -> list[dict]:
    """갭(×ATR) 오름차순 정렬 – 아직 덜 뛴(추격위험 낮은) 종목이 앞에 온다."""
    return sorted(rows, key=gap_atr_key)


# ── 보유 종목 판정 ──────────────────────────────────────────


@dataclass
class HoldingInput:
    """노션 DB에서 읽은 종목 한 건."""

    ticker: str           # 종목코드 (6자리)
    name: str             # 종목명
    market: str           # 시장 (KOSPI / KOSDAQ)
    buy_price: float      # 매수단가
    shares: int           # 보유수량
    # 운용 구분 (자동/수동/터틀외). 터틀외는 터틀 진입이 아닌 종목이라
    # 아침 리포트에서 R배수 계산·정렬 대상에서 뺀다 (mailer.py).
    managed_by: str = ""
    # 1차·2차 익절가는 노션에 두되 읽지 않는다. 청산은 10일 저가로 판정한다.
    reeval_date: Optional[date] = None      # 재평가 기한
    corr_group: str = ""                    # 상관군 (빈 값이면 상관군 없음)
    # 노션에 저장된 기존 값 (없으면 None)
    prev_trailing_high: Optional[float] = None  # 진입후 최고가
    prev_stop_loss: Optional[float] = None      # 손절선
    notion_atr: Optional[float] = None          # 노션 'ATR' 칸 (매일 갱신되는 참고값)
    entry_atr: Optional[float] = None           # 진입시 ATR – 배치가 덮어쓰지 않는다
    last_buy_price: Optional[float] = None      # 마지막 매수가 (추가매수 기준)
    signal_first_date: Optional[date] = None    # 신호 최초 발생일
    last_alerted_stop: Optional[float] = None   # 마지막으로 갱신 알림을 보낸 손절선
    # 아침 배치(daily_report.py)가 기록해둔 직전 판정과 그 판정을 낸 날.
    # 자동매도(kis_client.run_auto_sell)가 "오늘 낸 판정"만 신뢰하는 데 쓴다 –
    # 배치가 못 돈 날의 묵은 판정으로 팔아버리면 안 되므로 둘을 함께 읽는다.
    # 다음 추가매수 레벨을 재는 기준. 보통 마지막매수가와 같지만, 급등으로
    # 매수 창을 지나치면 산 것 없이 이 값만 올려 다음 창을 연다 - 마지막매수가는
    # 실제 체결 기록이자 손절선 계산의 기준이라 건드리면 안 되기 때문이다.
    pyramid_anchor: Optional[float] = None      # 추가매수 기준가
    recent_verdict: str = ""                    # 최근 판정 (손절/추세청산/…)
    checked_date: Optional[date] = None         # 확인일 (배치가 마지막으로 판정한 날)
    units: Optional[int] = None                 # 유닛수 (1~4, 추가매수 때마다 갱신)
    # 저녁 실행감사 배치(evening_audit.py) 전용 - 아침 배치는 절대 읽거나
    # 쓰지 않는다. 위 '신호 최초 발생일'(아침 배치 전용)과는 별개 칸이다.
    evening_signal_type: str = ""               # 저녁판정 신호유형
    evening_signal_date: Optional[date] = None  # 저녁판정 발생일
    # 공시·뉴스 (Cowork가 매일 07:00에 기록) – 계산에는 쓰지 않고 리포트에만 사용
    news_memo: str = ""                      # 공시·뉴스
    exit_signal: bool = False                # 철수신호
    news_date: Optional[date] = None         # 뉴스 확인일
    # 사람이 적어두는 판단 근거 – 메일 카드에만 쓴다 (계산에 쓰지 않음)
    buy_reason: str = ""                     # 산 이유
    bull_case: str = ""                      # 강세론
    bear_case: str = ""                      # 약세론
    next_event: str = ""                     # 다음 확인 이벤트


@dataclass
class HoldingResult:
    """한 종목의 계산 결과."""

    ticker: str
    name: str
    corr_group: str = ""         # 노션 '상관군' 값 (빈 값이면 상관군 없음)
    current_price: float = 0.0
    atr: float = 0.0
    unit_shares: int = 0         # 1유닛 주수
    trailing_high: float = 0.0   # 진입후 최고가 (갱신 후)
    stop_loss: float = 0.0       # 손절선 (갱신 후)
    risk_amount: float = 0.0     # 종목 리스크액
    current_units: float = 0.0   # 현재 유닛수
    # 터틀 청산 신호 (웹앱과 같은 값)
    exit_level: float = 0.0        # 청산선 = 직전 10일 저가 (당일 제외)
    dist_to_exit_pct: float = 0.0  # 현재가에서 청산선까지 남은 거리 %
    # 진입시 고정값 / 추가매수
    entry_atr: int = 0             # 트레일링 계산에 쓴 진입시 ATR
    entry_atr_is_new: bool = False  # 노션에 처음 기록해야 하는가
    last_buy_price: float = 0.0    # 마지막 매수가 (없으면 매수단가로 시작)
    next_add_price: Optional[float] = None  # 다음 추가매수 지점 (4유닛이면 None)
    # 신호 최초 발생일 – 값이 있으면 유지, 신호 해소 시 None
    signal_first_date: Optional[date] = None
    signal_active: bool = False    # 손절선/10일 저가 이탈 상태인가
    verdict: str = ""            # 판정
    verdict_memo: str = ""       # 판정 메모
    error: Optional[str] = None  # 조회 실패 시 메시지
    # 손절선 갱신 알림 – stop_update_alert가 True일 때만 카톡·메일에 노출
    stop_update_alert: bool = False
    # 노션 '마지막 알린 손절선'에 쓸 새 값. None이면 그 칸을 건드리지 않는다
    # (임계값 미달로 알리지 않은 경우, 값을 남겨두면 차이가 계속 리셋돼
    # 영원히 임계값에 도달하지 못한다).
    last_alerted_stop: Optional[float] = None

    @property
    def is_action_needed(self) -> bool:
        return self.verdict not in ("유지", "", "조회실패")


def evaluate_holding(
    inp: HoldingInput,
    total_capital: float,
) -> HoldingResult:
    """한 종목을 평가한다. 예외 시 error가 채워진 Result를 반환."""
    result = HoldingResult(
        ticker=inp.ticker,
        name=inp.name,
        corr_group=inp.corr_group,
    )

    try:
        df = fetch_ohlcv(inp.ticker)
        atr = calc_atr(df)
        current_price = latest_close(df)
    except Exception as e:
        logger.exception("[%s] 시세 조회 실패", inp.ticker)
        result.error = str(e)
        result.verdict = "조회실패"
        result.verdict_memo = f"시세 조회 실패: {e}"
        return result

    # ATR을 정수로 반올림한 시점부터 모든 하위 계산을 정수로 수행한다 (A-3)
    atr = round_atr(atr)
    result.atr = atr
    result.current_price = current_price

    # 진입시 ATR은 진입 때 1회만 기록하고 배치가 덮어쓰지 않는다 (B-1).
    # 매일 갱신되는 ATR을 트레일링 첫 항에 쓰면 급등 시 ATR이 커지며
    # 손절선이 내려가고, 1R(= 2 × 진입시ATR) 기준도 사라진다.
    if inp.entry_atr:
        result.entry_atr = round_atr(inp.entry_atr)
    else:
        result.entry_atr = atr
        result.entry_atr_is_new = True

    # 마지막 매수가 – 없으면 매수단가에서 시작한다 (1유닛 보유 상태)
    result.last_buy_price = inp.last_buy_price or inp.buy_price

    # 1유닛 주수 (내림). 유닛은 진입 시점 ATR로 산정된다.
    result.unit_shares = calc_position(result.entry_atr, total_capital).unit_shares

    # 진입후 최고가
    if inp.prev_trailing_high is not None:
        result.trailing_high = max(inp.prev_trailing_high, current_price)
    else:
        result.trailing_high = max(inp.buy_price, current_price)

    # 손절선: 노트 원문 방식(상승분의 1/2). 두 기준 중 높은 값을 쓰고
    # 기존값보다 낮아지지 않는다 – 우선순위는 resolve_stop() 참고.
    # 노션 '매수단가'를 현재 유닛 기준가(= 마지막 매수가)로 본다.
    # 추가매수 시에는 매수단가를 마지막 매수가로 갱신해야 값이 맞는다.
    # 진입시 ATR을 쓰므로 1R(= 2 × 진입시ATR)이 보유 기간 내내 고정된다.
    # resolve_stop 안에서 기존 손절선과 max를 취하므로 어떤 경로로도
    # 손절선이 내려가지 않는다 (B-2).
    new_stop = resolve_stop(
        entry=inp.buy_price,
        atr=result.entry_atr,
        trailing_high=result.trailing_high,
        last_buy_price=result.last_buy_price,
        prev_stop=inp.prev_stop_loss,
    )
    result.stop_loss = int(round(new_stop))
    if inp.prev_stop_loss is not None:
        # 기존값을 정수로 바꿀 때는 올림한다. 반올림하면 소수점이 있던
        # 기존 손절선이 최대 0.5원 내려가는데, 손절선은 어떤 경우에도
        # 내려가면 안 된다 (float → int 전환 때 1회 발생).
        result.stop_loss = max(result.stop_loss, math.ceil(inp.prev_stop_loss))

    # 종목 리스크액
    result.risk_amount = (current_price - result.stop_loss) * inp.shares

    # 현재 유닛수
    if result.unit_shares > 0:
        result.current_units = round(inp.shares / result.unit_shares, 2)
    else:
        result.current_units = 0.0

    # 터틀 청산 신호 – 웹앱과 같은 trend_signals()를 그대로 쓴다.
    # 데이터가 모자라 신호를 못 내면 청산 판정만 건너뛴다 (손절·기한은 유효).
    try:
        sig = trend_signals(df)
        # 판정 기준은 '당일을 제외한' 직전 10일 저가다. 당일을 포함하면
        # 종가는 항상 당일 저가 이상이라 청산이 사실상 발동하지 않는다
        # (20일 고가 돌파 판정과 같은 이유). 표시도 이 값으로 통일한다 —
        # 판정선과 다른 값을 보여주면 여유 %가 선과 맞지 않는다.
        exit_level: Optional[float] = sig.low_10_prev
        result.exit_level = sig.low_10_prev
        result.dist_to_exit_pct = sig.dist_to_exit_pct
    except ValueError as e:
        logger.warning("[%s] 청산 신호 계산 불가: %s", inp.ticker, e)
        exit_level = None

    # ── 판정 (위에서부터 첫 번째로 걸리는 것) ──
    if current_price <= result.stop_loss:
        result.verdict = "손절"
        result.verdict_memo = (
            f"현재가 {current_price:,.0f} ≤ 손절선 {result.stop_loss:,.0f}"
        )
    elif exit_level is not None and current_price <= exit_level:
        result.verdict = "추세청산"
        result.verdict_memo = (
            f"현재가 {current_price:,.0f} ≤ 10일 저가 {exit_level:,.0f}"
        )
    elif inp.reeval_date is not None and inp.reeval_date < date.today():
        result.verdict = "기한도래"
        result.verdict_memo = f"재평가 기한 {inp.reeval_date} 경과"
    else:
        result.verdict = "유지"
        result.verdict_memo = ""

    # ── 신호 최초 발생일 (B-3) ──
    # 손절선/10일 저가를 처음 이탈한 날을 보존한다. 이미 값이 있으면
    # 덮어쓰지 않고, 신호가 해소되면 비운다 (매매일지 지연일수 계산용).
    result.signal_active = result.verdict in ("손절", "추세청산")
    if result.signal_active:
        result.signal_first_date = inp.signal_first_date or date.today()
    else:
        result.signal_first_date = None

    # ── 다음 추가매수 지점 (B-4) ──
    # 1/2 ATR 오를 때마다 1유닛, 최대 4유닛. 4유닛이면 추가매수 종료.
    if result.current_units < MAX_UNITS and result.entry_atr > 0:
        result.next_add_price = int(
            round(result.last_buy_price + PYRAMID_ATR_STEP * result.entry_atr)
        )

    # ── 손절선 갱신 알림 ──
    # entry_atr이 진입 시점에 고정된 값이 아니면(= inp.entry_atr이 비어
    # 이번에 처음 잡힌 값이면) R배수 기준이 아직 안정적이지 않으므로
    # 대상에서 제외한다.
    if inp.entry_atr is None:
        logger.info("[%s] 진입시 ATR 없음 – 손절선 갱신 알림 대상 제외", inp.name)
    elif inp.last_alerted_stop is None:
        # 신규 편입 – 알리지 않고 현재 손절선으로 초기화만 한다
        result.last_alerted_stop = result.stop_loss
    else:
        diff = result.stop_loss - inp.last_alerted_stop
        if diff >= STOP_UPDATE_ATR_MULT * result.entry_atr:
            result.stop_update_alert = True
            result.last_alerted_stop = result.stop_loss

    return result


# ── 전체 리스크 ─────────────────────────────────────────────


@dataclass
class PortfolioRisk:
    """전체 포트폴리오 리스크 요약."""

    total_risk_amount: float = 0.0
    total_risk_pct: float = 0.0
    # 상관군 이름 → 리스크액 / 비중 (노션 '상관군' 값 기준, 빈 값은 제외)
    group_risk_amount: dict[str, float] = field(default_factory=dict)
    group_risk_pct: dict[str, float] = field(default_factory=dict)
    risk_warning: bool = False     # 6% 초과
    stop_pending: int = 0          # 손절 판정이 난 종목 수 (리스크와 별개)


def calc_portfolio_risk(
    results: list[HoldingResult],
    total_capital: float,
) -> PortfolioRisk:
    """전체 리스크와 상관군별 리스크를 계산한다.

    종목 리스크액은 종목별로 max(0, (현재가-손절선)×수량)으로 본다.
    이미 손절선을 깬 종목은 음수가 되는데, 그대로 더하면 다른 종목의
    리스크를 상쇄해 여력이 있는 것처럼 보이기 때문이다. 손절 판정
    자체는 건드리지 않고 집계에서만 0으로 끊는다 (건수는 stop_pending).
    """
    pr = PortfolioRisk()

    for r in results:
        if r.error:
            continue

        if r.verdict == "손절":
            pr.stop_pending += 1

        amount = max(0.0, r.risk_amount)
        pr.total_risk_amount += amount

        group = (r.corr_group or "").strip()
        if group:
            pr.group_risk_amount[group] = (
                pr.group_risk_amount.get(group, 0.0) + amount
            )

    if total_capital > 0:
        pr.total_risk_pct = round(
            (pr.total_risk_amount / total_capital) * 100, 2
        )
        pr.group_risk_pct = {
            g: round((amt / total_capital) * 100, 2)
            for g, amt in pr.group_risk_amount.items()
        }

    # 리스크액이 큰 상관군부터
    pr.group_risk_amount = dict(
        sorted(pr.group_risk_amount.items(), key=lambda kv: -kv[1])
    )
    pr.group_risk_pct = {
        g: pr.group_risk_pct.get(g, 0.0) for g in pr.group_risk_amount
    }

    pr.risk_warning = pr.total_risk_pct > 6.0
    return pr


# ── 상관군 유닛 집계 ────────────────────────────────────────
#
# app.py(진입 점검 화면)와 daily_report.py(장중 감시용 파일 저장)가
# 같은 정의를 공유한다. 유닛은 진입 시점 ATR 기준이므로 '진입시 ATR'을
# 우선 쓰고, 없으면 노션 'ATR' 칸(매일 갱신되는 값)으로 대신한다.


@dataclass
class CorrUnits:
    """보유 종목의 상관군별 누적 유닛수."""

    group_units: dict[str, float] = field(default_factory=dict)
    total_units: float = 0.0
    skipped: list[str] = field(default_factory=list)   # 유닛을 셀 수 없었던 종목명
    known_groups: list[str] = field(default_factory=list)  # 노션에 실제 쓰인 상관군 이름


def calc_corr_units(holdings: list[HoldingInput], capital: float) -> CorrUnits:
    """상관군 이름 목록은 유닛 집계 성패와 무관하게 노션에 실제 쓰인 값을
    전부 모은 것이다 (드롭다운 선택지·참고 표시용). 그래서 그 이름들은
    유닛이 하나도 안 잡혀도 0.0으로 결과에 포함된다.
    """
    known: set[str] = set()
    for inp in holdings:
        group = (inp.corr_group or "").strip()
        if group:
            known.add(group)

    group_units: dict[str, float] = {g: 0.0 for g in known}
    total = 0.0
    skipped: list[str] = []

    for inp in holdings:
        group = (inp.corr_group or "").strip()
        atr = inp.entry_atr or inp.notion_atr
        if not atr:
            skipped.append(f"{inp.name}(ATR 없음)")
            continue
        if not inp.shares:
            skipped.append(f"{inp.name}(보유수량 없음)")
            continue
        unit_shares = calc_position(atr, capital).unit_shares
        if unit_shares <= 0:
            # 1ATR이 계좌 1%보다 커서 1유닛이 0주 – 계좌 규모에 안 맞는 종목
            skipped.append(f"{inp.name}(1유닛 0주)")
            continue
        units = inp.shares / unit_shares
        total += units
        if group:
            group_units[group] = group_units.get(group, 0.0) + units

    return CorrUnits(
        group_units=group_units,
        total_units=total,
        skipped=skipped,
        known_groups=sorted(known),
    )


# ── CLI 테스트 ──────────────────────────────────────────────


def _print_report(
    code: str,
    capital: float,
    days: int,
    end: Optional[date],
) -> None:
    """python core.py <종목코드> 출력."""
    df = fetch_ohlcv(code, days=days, end=end)

    try:
        name = krx.get_market_ticker_name(code)
    except Exception:
        name = code   # 이름 조회 실패는 계산에 영향 없음

    first_day = df.index[0].strftime("%Y-%m-%d")
    last_day = df.index[-1].strftime("%Y-%m-%d")
    price = latest_close(df)

    print(f"\n{name} ({code})")
    print(f"조회 구간: {first_day} ~ {last_day}  ({len(df)} 거래일)")
    print(f"총자본: {capital:,.0f}원  |  1회 리스크: {RISK_PER_TRADE:.0%}")

    print("\n[ATR]")
    atr = calc_atr(df)
    atr_sma = calc_atr(df, method="sma")
    trs = true_ranges(df)
    print(f"  현재가        {price:,.0f}원")
    print(f"  ATR({ATR_PERIOD}) wilder {atr:,.2f}원   ← 적용 (TR {len(trs)}개 재귀)")
    print(f"  ATR({ATR_PERIOD}) sma    {atr_sma:,.2f}원      (최근 20개 단순평균, "
          f"차이 {atr - atr_sma:+,.2f})")
    print(f"  2×ATR         {2 * atr:,.2f}원")

    print("\n[포지션]")
    pos = calc_position(atr, capital)
    print(f"  1회 리스크액  {pos.risk_amount:,.0f}원")
    print(f"  1유닛 주수    {pos.unit_shares:,}주 "
          f"= floor({pos.risk_amount:,.0f} / {atr:,.2f})")
    print(f"  1유닛 투입금  {price * pos.unit_shares:,.0f}원 "
          f"({price * pos.unit_shares / capital * 100:.1f}%)")
    print(f"  손절선        {calc_stop(price, atr):,.0f}원 (현재가 - 2×ATR)")

    print("\n[기대값] 최근 20일 일간수익률")
    try:
        edge = calc_edge(df)
        rr = f"{edge.rr:.2f}" if edge.rr is not None else "N/A"
        print(f"  TE            {edge.te:+.3f}%")
        print(f"  RR            {rr}")
        print(f"  승률          {edge.win_rate:.1f}%  ({edge.win_days}일)")
        print(f"  패율          {edge.loss_rate:.1f}%  ({edge.loss_days}일)")
        print(f"  보합          {edge.flat_days}일  (승·패 제외)")
        print(f"  평균수익      {edge.avg_win:+.3f}%")
        print(f"  평균손실      {edge.avg_loss:+.3f}%")
    except ValueError as e:
        print(f"  계산 실패: {e}")

    print("\n[추세 신호]")
    try:
        sig = trend_signals(df)
        print(f"  20일 고가     {sig.high_20:,.0f}원 (당일 포함)")
        print(f"  직전 20일고가 {sig.high_20_prev:,.0f}원 → 돌파 "
              f"{'O' if sig.breakout else 'X'}")
        print(f"  10일 저가     {sig.low_10:,.0f}원 (당일 포함)")
        print(f"  직전 10일저가 {sig.low_10_prev:,.0f}원 → 청산 "
              f"{'O' if sig.exit_triggered else 'X'} "
              f"(청산선까지 {sig.dist_to_exit_pct:.1f}%)")
    except ValueError as e:
        print(f"  계산 실패: {e}")

    print("\n[유닛 진행표] 진입가 = 현재가 기준")
    steps = build_pyramid(price, atr, pos.unit_shares, capital)
    if not steps:
        print("  1유닛 주수가 0이라 표를 만들 수 없습니다.")
    else:
        print(f"  {'단계':<5}{'매수가':>10}{'손절선':>10}"
              f"{'누적주수':>9}{'누적투입':>13}{'비중':>8}"
              f"{'손절시 손실':>14}{'비중':>8}")
        for s in steps:
            print(
                f"  {s.unit}U   {s.buy_price:>10,.0f}{s.stop_loss:>10,.0f}"
                f"{s.cum_shares:>9,}{s.cum_cost:>13,.0f}"
                f"{s.cum_weight_pct:>7.1f}%{s.loss_if_stopped:>14,.0f}"
                f"{s.loss_pct:>7.2f}%"
            )
    print()


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="터틀 트레이딩 계산 CLI 테스트"
    )
    parser.add_argument("code", help="종목코드 6자리 (예: 240550)")
    parser.add_argument(
        "--capital", type=float, default=DEFAULT_CAPITAL,
        help=f"총자본 (기본 {DEFAULT_CAPITAL:,})",
    )
    parser.add_argument(
        "--days", type=int, default=FETCH_DAYS,
        help=f"조회 거래일 수 (기본 {FETCH_DAYS})",
    )
    parser.add_argument(
        "--end", default=None,
        help="기준일 YYYY-MM-DD (기본 오늘). 과거 검증용",
    )
    args = parser.parse_args()

    end = date.fromisoformat(args.end) if args.end else None

    try:
        _print_report(args.code, args.capital, args.days, end)
    except Exception as e:
        # 한 종목 실패가 전체를 멈추지 않도록 메시지만 남긴다
        print(f"조회 실패 [{args.code}]: {e}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
