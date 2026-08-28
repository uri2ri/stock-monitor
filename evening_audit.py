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
import os
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

MANAGED_AUTO = notion_repo.MANAGED_AUTO       # "자동" - 모의계좌 자동매매
MANAGED_MANUAL = notion_repo.MANAGED_MANUAL   # "수동" - 실계좌 사람이 실행


def _managed_label(inp) -> str:
    """운용 구분 표시값. 빈 값은 수동으로 본다.

    노션 '운용' 칸이 비어 있는 행이 과거에 여럿 있었는데, 그것들은 전부
    사람이 직접 산 실계좌 보유였다. 빈 값을 자동으로 오해하면 감사 대상
    (수동 실행)에서 통째로 빠지므로 수동 쪽으로 붙인다.
    """
    return inp.managed_by or MANAGED_MANUAL


@dataclass
class SignalRow:
    ticker: str
    name: str
    signal_type: str
    ref_price: float
    days: int             # 실행: 지연일수 / 미실행: 누적일수
    # 유닛추가일 때만 - 현재 보유 유닛과 현재가가 정당화하는 유닛.
    # 둘의 차이가 "몇 유닛 밀렸는지"다. 예전에는 held+1만 보여줘서
    # 코스맥스처럼 이미 4유닛 구간에 있어도 "2유닛째"로 표시됐다.
    held_units: Optional[int] = None
    justified_units: Optional[int] = None


@dataclass
class LevelChange:
    """오늘 종가로 손절선·청산선이 움직인 종목 한 줄.

    손절선 before는 노션에 저장된 값(= 오늘 아침 배치가 전일 종가로 쓴 값),
    after는 오늘 종가까지 반영해 다시 계산한 값이다. 즉 이 섹션은 "내일
    아침 배치가 기록할 값"의 예고다 - 아침 리포트 표만 보면 그 손절선이
    오늘 갱신된 값인지 며칠째 그대로인지 구분이 안 되기 때문에 따로 낸다.

    청산선(10일 저가)은 노션에 저장하는 칸이 없어 같은 OHLCV에서
    전일 기준값과 당일 기준값을 각각 계산해 비교한다.
    """
    ticker: str
    name: str
    managed: str
    stop_before: Optional[float] = None
    stop_after: Optional[float] = None
    exit_before: Optional[float] = None
    exit_after: Optional[float] = None

    @staticmethod
    def _moved(before: Optional[float], after: Optional[float]) -> bool:
        if before is None or after is None:
            return False
        return round(before) != round(after)

    @property
    def stop_moved(self) -> bool:
        return self._moved(self.stop_before, self.stop_after)

    @property
    def exit_moved(self) -> bool:
        return self._moved(self.exit_before, self.exit_after)

    @property
    def any_moved(self) -> bool:
        return self.stop_moved or self.exit_moved


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
    level_changes: list[LevelChange] = field(default_factory=list)


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


def _detect_signal(inp, df, result) -> Optional[tuple[str, float, Optional[int]]]:
    """이 종목의 오늘 종가 기준 신호. (신호유형, 참고가, 정당유닛) 또는 None.

    손절선이탈·10일저가이탈은 **직접 판정하지 않고** 아침 배치와 같은
    core.evaluate_holding()의 판정(result.verdict)을 그대로 옮긴다.
    예전에는 이 함수가 "종가 < 노션 손절선"을 자체 구현했는데, 아침
    배치는 같은 조건을 resolve_stop()으로 트레일링·래칫까지 반영해
    판정한다. 두 구현이 같은 종가 데이터로 서로 다른 답을 낼 수 있었고
    (저녁 18:05 판정 = 다음날 아침 07:35 판정이 같은 종가를 본다),
    실제로 이름만 다른 같은 사건이 두 메일에 따로 실렸다.

    유닛추가만 이 배치 고유 판정이다 - 아침 리포트는 "다음 추가매수
    지점"을 안내할 뿐 미실행 여부를 감사하지 않는다.

    우선순위: 손절선이탈 > 10일저가이탈 > 유닛추가 (evaluate_holding의
    판정 우선순위를 그대로 따른다 - 하락 신호가 상승 신호보다 급하다).
    """
    if result.verdict == "손절":
        return SIGNAL_STOP, result.stop_loss, None
    if result.verdict == "추세청산":
        return SIGNAL_10LOW, result.exit_level, None

    # 유닛추가 - 보유 유닛수는 노션 '유닛수'를 그대로 믿는다. 보유수량을
    # 오늘 기준 유닛주수로 나눠 역산하면 ATR·계좌평가액이 변한 만큼
    # 드리프트한다(kis_client의 상관군 캡에서 같은 이유로 노션 값으로
    # 바꿨다).
    if inp.units and inp.entry_atr and inp.buy_price:
        close = core.latest_close(df)
        justified = _pyramid_units_justified(inp.buy_price, inp.entry_atr, close)
        if justified > inp.units:
            # 참고가는 '정당화된 마지막 레벨'이다. 예전엔 다음 레벨
            # (보유+1)만 보여줘서, 코스맥스처럼 이미 4유닛 구간까지
            # 올라간 종목도 "2유닛째 레벨"로 표시돼 몇 유닛 밀렸는지
            # 알 수 없었다.
            level_price = (
                inp.buy_price
                + (justified - 1) * core.PYRAMID_ATR_STEP * inp.entry_atr
            )
            return SIGNAL_PYRAMID, level_price, justified

    return None


def _update_signal_tracking(
    page_id: str, inp, detected: Optional[tuple[str, float, Optional[int]]],
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

    signal_type = detected[0]
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


def _build_level_change(inp, df, result, managed: str) -> LevelChange:
    """오늘 종가 기준 손절선·청산선과, 그 직전 값을 짝지어 담는다.

    손절선 before = 노션 저장값(오늘 아침 배치가 전일 종가로 쓴 값),
    after = evaluate_holding()이 오늘 종가까지 반영해 낸 값.

    청산선은 노션에 저장 칸이 없어 같은 OHLCV로 두 번 계산한다:
    당일 기준(core.trend_signals(df))과 전일 기준(마지막 행을 뺀 df).
    둘 다 '당일 제외 10일 저가'(low_10_prev)를 쓰므로 기준이 일관된다.
    데이터가 모자라 전일 기준을 못 내면 청산선 비교만 건너뛴다.
    """
    exit_before: Optional[float] = None
    try:
        exit_before = core.trend_signals(df.iloc[:-1]).low_10_prev
    except (ValueError, IndexError) as e:
        logger.debug("[%s] 전일 기준 청산선 계산 불가: %s", inp.ticker, e)

    return LevelChange(
        ticker=inp.ticker,
        name=inp.name,
        managed=managed,
        stop_before=inp.prev_stop_loss,
        stop_after=result.stop_loss or None,
        exit_before=exit_before,
        exit_after=result.exit_level or None,
    )


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

    # 리포트에 쓰지는 않지만 core.evaluate_holding()이 요구한다(유닛주수·
    # 리스크액 계산용). 이 배치는 그 두 값을 표시하지 않으므로, 미설정이면
    # 기본값으로 진행한다 - 판정(손절/추세청산)과 손절선·청산선은
    # total_capital에 영향받지 않는다.
    total_capital = float(os.environ.get("TOTAL_CAPITAL") or 0) or core.DEFAULT_CAPITAL

    # ── 1. 오늘의 매매 ──────────────────────────────────────
    orders = notion_repo.fetch_orders_today(today)
    holdings = notion_repo.fetch_holdings()
    holdings_by_ticker = {inp.ticker: (pid, inp) for pid, inp in holdings}

    # 매매일지는 자동·수동이 한 DB에 섞여 있다. 점검표에서 운용="자동"인
    # 행의 page_id를 미리 받아 청산 기록의 '보유종목' relation과 대조해야
    # 어느 계좌 건인지 갈린다 (weekly_report.py와 같은 방식).
    try:
        auto_ids = notion_repo.fetch_auto_holding_page_ids()
    except Exception:                            # noqa: BLE001
        logger.exception("점검표(운용=자동) page_id 조회 실패 - 청산 계좌 구분 생략")
        auto_ids = set()

    # 자동주문 기록 DB는 모의계좌(자동매매) 전용이라 여기 매수는 전부 자동이다.
    for o in orders:
        if o["order_type"] == notion_repo.ORDER_ADD:
            cur = holdings_by_ticker.get(o["ticker"])
            unit_label = f"{cur[1].units}u" if cur and cur[1].units else "?u"
        else:
            unit_label = "1u"
        report.buys.append({**o, "unit_label": unit_label, "managed": MANAGED_AUTO})

    ledger_today = notion_repo.fetch_ledger_closed_today(today)
    for page_id, entry in ledger_today:
        entry["managed"] = (
            MANAGED_AUTO if entry.get("holding_page_id") in auto_ids
            else MANAGED_MANUAL
        )
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
    # 운용=자동(모의투자)은 여기서 뺀다 - kis_client.run_auto_sell()이 신호가
    # 뜨는 즉시 같은 회차에 실주문으로 처리하므로 "미실행"이 사실상 나올 수
    # 없고(나온다면 그건 자동매매 자체의 장애지 사람의 실행 지연이 아니다),
    # 이 리포트의 원래 목적(실계좌 수동 실행 감사)과 자금 규모부터 다른
    # 모의계좌 신호가 같은 목록에 섞이면 몇 건 중 몇 건 실행했는지 비율도
    # 왜곡된다. 저녁판정 신호유형/발생일 노션 기록도 자동 보유분은 건드리지
    # 않는다 - 이 두 칸은 이 리포트 전용이라 다른 코드가 읽지 않는다.
    for page_id, inp in holdings:
        managed = _managed_label(inp)

        try:
            df = core.fetch_ohlcv(inp.ticker)
            result = core.evaluate_holding(inp, total_capital)
        except Exception as e:                    # noqa: BLE001
            logger.warning("[%s] 시세 조회 실패 - 신호 판정 건너뜀: %s",
                           inp.ticker, e)
            continue
        if result.error:
            logger.warning("[%s] 평가 실패 - 신호 판정 건너뜀: %s",
                           inp.ticker, result.error)
            continue

        # ── 3. 손절선·청산선 변동 (보유 전 종목 대상) ──────
        # 자동·수동·터틀외를 가리지 않는다 - 손절선은 세 경우 모두
        # 아침 배치가 관리하고, 자동매매는 이 값으로 실제 매도까지 낸다.
        report.level_changes.append(_build_level_change(inp, df, result, managed))

        # ── 2. 신호 재판정은 수동(터틀 대상)만 ─────────────
        # 운용=자동(모의투자)은 뺀다 - kis_client.run_auto_sell()이 신호가
        # 뜨는 즉시 같은 회차에 실주문으로 처리하므로 "미실행"이 사실상 나올 수
        # 없고(나온다면 그건 자동매매 자체의 장애지 사람의 실행 지연이 아니다),
        # 이 리포트의 원래 목적(실계좌 수동 실행 감사)과 자금 규모부터 다른
        # 모의계좌 신호가 같은 목록에 섞이면 몇 건 중 몇 건 실행했는지 비율도
        # 왜곡된다. 저녁판정 신호유형/발생일 노션 기록도 자동 보유분은 건드리지
        # 않는다 - 이 두 칸은 이 리포트 전용이라 다른 코드가 읽지 않는다.
        if managed in (core.MANAGED_NON_TURTLE, MANAGED_AUTO):
            continue

        detected = _detect_signal(inp, df, result)
        resolved = _update_signal_tracking(
            page_id, inp, detected, today, dry_run, report.holding_signal_writes,
        )
        if resolved is None:
            continue
        signal_type, signal_date = resolved
        days = _trading_days_between(df, signal_date, today)
        ref_price = detected[1] if detected else core.latest_close(df)
        justified = detected[2] if detected else None
        report.unexecuted.append(SignalRow(
            ticker=inp.ticker, name=inp.name, signal_type=signal_type,
            ref_price=ref_price, days=days,
            held_units=inp.units if signal_type == SIGNAL_PYRAMID else None,
            justified_units=justified,
        ))

    # 움직인 종목만 남긴다 - 변동 없는 종목까지 나열하면 아침 리포트의
    # 전체 현황과 다를 게 없어져 이 섹션의 의미가 사라진다.
    report.level_changes = [c for c in report.level_changes if c.any_moved]

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
