"""
evening_audit.py – 저녁 실행 감사 배치 (18:00 KST)

아침 리포트는 "오늘 뭘 할지"를 본다. 이 배치는 "규칙이 시킨 것과 실제로
한 것이 얼마나 달랐는지"만 본다. 보유 현황 표·트레일링 갱신·손절선
임박 순위는 아침 리포트와 같은 값이라 여기서는 다시 넣지 않는다.

판단·권유 문구를 쓰지 않는다 - 신호 유무와 실행 여부, 지연일수만
보여준다. 카톡은 없다, 메일만 보낸다.

노션 쓰기는 다음 3칸으로만 한정한다:
    점검표    - 저녁판정 신호유형 / 저녁판정 발생일
    매매일지  - 신호 발생일
아침 배치(daily_report.py)가 쓰는 손절선·진입후 최고가·최근 판정·
신호 최초 발생일 등은 절대 건드리지 않는다 (과거 두 주체가 같은 칸을
써서 판정이 덮인 전례가 있다).

신호 판정은 '오늘 종가' 기준이다 (장중 실시간이 아니다). 손절선
이탈은 kis_client.run_auto_sell()이 10분마다 이미 실시간으로 잡고
실주문까지 낸다 - 이 배치는 그 결과를 감사할 뿐 별도로 매매하지
않는다.

로컬 실행:
    python evening_audit.py
    python evening_audit.py --dry-run   # 노션 쓰기 생략, 메일 제목에 [DRY]
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional
from zoneinfo import ZoneInfo

import core
import notion_repo
from mailer import send_evening_mail

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

KST = ZoneInfo("Asia/Seoul")

SIGNAL_STOP = "손절선이탈"
SIGNAL_10LOW = "10일저가이탈"
SIGNAL_PYRAMID = "유닛추가"

# 매매일지 '✱ 청산 사유' -> 이 배치가 판정하는 신호 유형. 매핑에 없는
# 사유(익절/철수조건 발생/임의 매도)는 '신호 없이 실행'으로 본다.
EXIT_REASON_TO_SIGNAL = {
    "손절": SIGNAL_STOP,
    "10일 저가 이탈": SIGNAL_10LOW,
}

@dataclass
class SignalRow:
    ticker: str
    name: str
    signal_type: str
    ref_price: float
    days: int             # 실행: 지연일수 / 미실행: 누적일수
    target_unit: Optional[int] = None   # 유닛추가일 때만 - 목표 유닛 번호


@dataclass
class EveningReport:
    today: date
    dry_run: bool
    buys: list[dict] = field(default_factory=list)
    exits: list[dict] = field(default_factory=list)
    executed: list[SignalRow] = field(default_factory=list)
    unexecuted: list[SignalRow] = field(default_factory=list)
    random_trades: list[dict] = field(default_factory=list)
    ledger_signal_writes: list[dict] = field(default_factory=list)
    holding_signal_writes: list[dict] = field(default_factory=list)


def _trading_days_between(df, start: date, end: date) -> int:
    """df의 거래일 인덱스 기준으로 (start, end] 사이 거래일 수."""
    count = 0
    for ts in df.index:
        d = ts.date() if hasattr(ts, "date") else ts
        if start < d <= end:
            count += 1
    return count


def _pyramid_units_justified(buy_price: float, entry_atr: float,
                              close: float, max_units: int = core.MAX_UNITS) -> int:
    """buy_price + n×(0.5×entry_atr) ≤ close 를 만족하는 최대 n에 대응하는 유닛수.

    n=0(1유닛)부터 max_units-1(마지막 유닛)까지 순서대로 검사한다 -
    가격이 그 사이 레벨을 만족하지 않고 뛰어넘는 경우는 없다고 본다
    (터틀 그리드는 등간격이라 통과하려면 그 아래 레벨을 전부 넘었어야
    한다).
    """
    justified = 0
    for n in range(max_units):
        threshold = buy_price + n * core.PYRAMID_ATR_STEP * entry_atr
        if close >= threshold:
            justified = n + 1
        else:
            break
    return justified


def _detect_signal(inp, df) -> Optional[tuple[str, float]]:
    """이 종목의 오늘 종가 기준 신호. (신호유형, 참고가) 또는 None.

    우선순위: 손절선이탈 > 10일저가이탈 > 유닛추가 (아침 배치의 판정
    우선순위와 같다 - 하락 신호가 상승 신호보다 급하다).
    """
    close = core.latest_close(df)

    if inp.prev_stop_loss and close < inp.prev_stop_loss:
        return SIGNAL_STOP, inp.prev_stop_loss

    try:
        sig = core.trend_signals(df)
        if sig.exit_triggered:
            return SIGNAL_10LOW, sig.low_10_prev
    except ValueError as e:
        logger.debug("[%s] 10일 저가 판정 불가: %s", inp.ticker, e)

    if inp.units and inp.entry_atr and inp.buy_price:
        justified = _pyramid_units_justified(inp.buy_price, inp.entry_atr, close)
        if justified > inp.units:
            level_price = (
                inp.buy_price + inp.units * core.PYRAMID_ATR_STEP * inp.entry_atr
            )
            return SIGNAL_PYRAMID, level_price

    return None


def _update_signal_tracking(
    page_id: str, inp, detected: Optional[tuple[str, float]],
    today: date, dry_run: bool, writes: list[dict],
) -> Optional[tuple[str, date]]:
    """저녁판정 신호유형/발생일 갱신 로직. (신호유형, 발생일)을 돌려준다 (신호 없으면 None).

    - 신호 있음 + 기존과 동일 유형 -> 발생일 유지
    - 신호 있음 + 유형 다르거나 발생일 공란 -> 유형 기록, 발생일=오늘
    - 신호 없음 -> 두 칸 비움
    """
    existing_type = inp.evening_signal_type or ""
    existing_date = inp.evening_signal_date

    if detected is None:
        if existing_type or existing_date:
            if not dry_run:
                notion_repo.update_evening_signal(page_id, None, None)
            writes.append({"name": inp.name, "signal_type": None})
        return None

    signal_type, _ref_price = detected
    if signal_type == existing_type and existing_date:
        new_date = existing_date
    else:
        new_date = today

    if signal_type != existing_type or new_date != existing_date:
        if not dry_run:
            notion_repo.update_evening_signal(page_id, signal_type, new_date)
        writes.append({"name": inp.name, "signal_type": signal_type,
                       "date": new_date})

    return signal_type, new_date


def _resolve_ledger_signal_date(entry: dict, today: date) -> date:
    """청산된 종목의 '신호 발생일'을 정한다.

    청산 전 점검표 행이 추적하던 저녁판정 발생일이 남아 있으면 그걸
    쓰고, 없으면(이 배치의 첫 실행 등) 청산일 자체를 기본값으로 쓴다 -
    과거를 역산하지 않는다.
    """
    if entry.get("holding_page_id"):
        tracked = notion_repo.fetch_holding_evening_signal_date(
            entry["holding_page_id"]
        )
        if tracked:
            return tracked
    return today


def build_report(today: date, dry_run: bool) -> EveningReport:
    report = EveningReport(today=today, dry_run=dry_run)

    # ── 1. 오늘의 매매 ──────────────────────────────────────
    orders = notion_repo.fetch_orders_today(today)
    holdings = notion_repo.fetch_holdings()
    holdings_by_ticker = {inp.ticker: (pid, inp) for pid, inp in holdings}

    for o in orders:
        if o["order_type"] == notion_repo.ORDER_ADD:
            cur = holdings_by_ticker.get(o["ticker"])
            unit_label = f"{cur[1].units}u" if cur and cur[1].units else "?u"
        else:
            unit_label = "1u"
        report.buys.append({**o, "unit_label": unit_label})

    ledger_today = notion_repo.fetch_ledger_closed_today(today)
    for page_id, entry in ledger_today:
        report.exits.append(entry)

        reason = entry.get("exit_reason") or ""
        signal_type = EXIT_REASON_TO_SIGNAL.get(reason)
        if signal_type is None:
            report.random_trades.append(entry)
            continue

        if entry.get("signal_date"):
            continue  # 이미 채워져 있음 - 다시 쓰지 않는다

        signal_date = _resolve_ledger_signal_date(entry, today)
        if not dry_run:
            notion_repo.update_ledger_signal_date(page_id, signal_date)
        report.ledger_signal_writes.append(
            {"name": entry["name"], "date": signal_date}
        )

        try:
            df = core.fetch_ohlcv(entry["ticker"])
            days = _trading_days_between(df, signal_date, today)
        except Exception:                        # noqa: BLE001
            days = 0
        report.executed.append(SignalRow(
            ticker=entry["ticker"], name=entry["name"], signal_type=signal_type,
            ref_price=entry.get("exit_price") or 0.0, days=days,
        ))

    # 추가매수는 시스템이 신호를 만족할 때만 넣으므로 '실행'으로 바로 잡는다
    # (신호 없이 임의로 추가매수하는 경로가 코드에 없다).
    for o in orders:
        if o["order_type"] == notion_repo.ORDER_ADD:
            report.executed.append(SignalRow(
                ticker=o["ticker"], name=o["name"], signal_type=SIGNAL_PYRAMID,
                ref_price=o.get("price") or 0.0, days=0,
            ))

    # ── 2. 터틀 대상 보유 종목 신호 재판정 ──────────────────
    for page_id, inp in holdings:
        if inp.managed_by == core.MANAGED_NON_TURTLE:
            continue
        try:
            df = core.fetch_ohlcv(inp.ticker)
        except Exception as e:                    # noqa: BLE001
            logger.warning("[%s] 시세 조회 실패 - 신호 판정 건너뜀: %s",
                           inp.ticker, e)
            continue

        detected = _detect_signal(inp, df)
        resolved = _update_signal_tracking(
            page_id, inp, detected, today, dry_run, report.holding_signal_writes,
        )
        if resolved is None:
            continue
        signal_type, signal_date = resolved
        days = _trading_days_between(df, signal_date, today)
        ref_price = detected[1] if detected else core.latest_close(df)
        target_unit = (inp.units + 1) if signal_type == SIGNAL_PYRAMID and inp.units else None
        report.unexecuted.append(SignalRow(
            ticker=inp.ticker, name=inp.name, signal_type=signal_type,
            ref_price=ref_price, days=days, target_unit=target_unit,
        ))

    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                         help="노션 쓰기 생략, 메일 제목에 [DRY]")
    args = parser.parse_args()

    today = datetime.now(KST).date()

    last_trading = core.last_trading_date()
    if last_trading is None:
        logger.error("휴장일 판정 실패 - 안전하게 이번 회차를 건너뜁니다.")
        return 1
    if last_trading != today:
        logger.info("휴장일(%s) - 저녁 감사를 건너뜁니다.", today)
        return 0

    logger.info("=== 저녁 실행 감사 시작 (dry_run=%s) ===", args.dry_run)
    report = build_report(today, args.dry_run)

    try:
        send_evening_mail(report)
    except Exception:
        logger.exception("저녁 감사 메일 발송 실패")
        return 1

    logger.info("=== 저녁 실행 감사 완료 ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
