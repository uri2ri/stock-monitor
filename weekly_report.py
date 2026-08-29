"""
weekly_report.py – 자동매매(운용="자동") 전용 주간 리포트 (토요일 발송)

daily_report.py·evening_audit.py는 실계좌(수동·터틀외) 관점이라
모의계좌(자동매매) 보유·손익을 일부러 뺀다. 이 배치는 반대로 자동매매
소관만 본다 - 실계좌와 자금 규모부터 다른 모의계좌 실적이 섞이면
숫자가 무의미해진다.

노션 DB 세 개를 모두 걸치지만 전부 읽기만 한다 - 이 배치는 아무것도
쓰지 않는다:
    보유종목 점검표 - 운용="자동" 행만 (현재 보유·평단가·손절선)
    자동주문 기록   - 이번 주 매매 내역(신규/추가/청산) + 거부·경고 사유
    매매일지        - 이번 주 실현손익 + 자동매매 시작 이후 누적 통계
                      (수동 매매도 같은 DB에 섞여 있어 '보유종목' relation을
                      점검표의 운용="자동" 집합과 대조해 걸러낸다)

카톡은 없다, 메일만 보낸다 (mailer.send_weekly_mail).

로컬 실행:
    python weekly_report.py            # 실제로 메일 발송
    python weekly_report.py --dry-run  # 메일 발송 생략, 본문을 표준출력에 출력
GitHub Actions: workflow_dispatch (weekly.yml). schedule 트리거는 없다 -
cron-job.org가 외부에서 매주 토요일 트리거한다(README/커밋 메시지 참고).
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import core
import kis_client
import notion_repo

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

KST = ZoneInfo("Asia/Seoul")

# 자동매매 실거래 시작일. 누적 통계(승률·R배수 등)는 이 날짜 이후
# 청산분만 센다 - 그 이전 매매일지 기록은 이 시스템과 무관하다.
AUTO_TRADE_START = date(2026, 8, 18)


@dataclass
class HoldingRow:
    """보유 현황 섹션 한 줄."""
    ticker: str
    name: str
    avg_price: float
    shares: int
    entry_atr: Optional[float]
    units: Optional[int]
    stop_loss: Optional[float]
    current_price: Optional[float] = None


@dataclass
class WeeklyReport:
    week_start: date
    week_end: date
    has_errors: bool = False

    # 1) 이번 주 요약
    realized_pl: float = 0.0
    win_count: int = 0
    loss_count: int = 0
    unrealized_pl: float = 0.0
    account_size: Optional[float] = None
    account_pct: Optional[float] = None

    # 2) 이번 주 매매 내역 (자동주문 기록 DB, 상태='성공' 기준)
    entries: list[dict] = field(default_factory=list)   # 신규 진입
    adds: list[dict] = field(default_factory=list)       # 추가매수
    exits: list[dict] = field(default_factory=list)      # 청산(매도)

    # 3) 보유 현황
    holdings: list[HoldingRow] = field(default_factory=list)

    # 4) 규칙 작동 점검 - 자동주문 기록 DB의 사유 칸 집계.
    # 주의: 사전 필터(가격상한/유닛금액과다/상관군캡/현금부족/우선순위밀림)는
    # 지금 카톡 알림으로만 나가고 이 DB에 안 남아 여기 안 잡힌다 - 실제
    # 주문 시도까지 간 거부·경고만 집계한다.
    rejected_by_reason: dict[str, int] = field(default_factory=dict)
    warned_by_reason: dict[str, int] = field(default_factory=dict)

    # 5) 누적 통계 (AUTO_TRADE_START 이후, 자동매매 청산분만)
    cum_count: int = 0
    cum_win_rate: Optional[float] = None
    cum_avg_r: Optional[float] = None
    cum_avg_hold_days: Optional[float] = None
    cum_avg_delay_days: Optional[float] = None


def _week_range(today: date) -> tuple[date, date]:
    """이번 주 월요일부터 오늘(토요일 실행 기준)까지."""
    monday = today - timedelta(days=today.weekday())
    return monday, today


def _tally_reason(bucket: dict[str, int], reason: str) -> None:
    key = reason.strip() or "(사유 없음)"
    bucket[key] = bucket.get(key, 0) + 1


def build_report(today: date) -> WeeklyReport:
    week_start, week_end = _week_range(today)
    report = WeeklyReport(week_start=week_start, week_end=week_end)

    # 매매일지는 수동 매매와 섞여 있어(카카오·삼현 등 기존 수기 기록),
    # '보유종목' relation을 이 집합과 대조해야 자동매매분만 걸러진다.
    try:
        auto_ids = notion_repo.fetch_auto_holding_page_ids()
    except Exception:
        logger.exception("점검표(운용=자동) page_id 조회 실패")
        report.has_errors = True
        auto_ids = set()

    # ── 2) 이번 주 매매 내역 + 4) 규칙 작동 점검 ──
    # 자동주문 기록 DB 한 번 조회로 두 섹션을 같이 채운다 (상태별로
    # 따로 쿼리하지 않는다).
    try:
        orders = notion_repo.fetch_orders_in_range(
            week_start, week_end, kis_client.ACCOUNT_TYPE,
        )
    except Exception:
        logger.exception("자동주문 기록(이번 주) 조회 실패")
        report.has_errors = True
        orders = []

    for o in orders:
        status = o["status"]
        if status == "성공":
            if o["side"] == notion_repo.SIDE_SELL:
                report.exits.append(o)
            elif o["order_type"] == notion_repo.ORDER_ADD:
                report.adds.append(o)
            else:
                report.entries.append(o)
        elif status == "거부":
            _tally_reason(report.rejected_by_reason, o["reason"])
        elif status == "경고":
            _tally_reason(report.warned_by_reason, o["reason"])

    # ── 1) 이번 주 실현손익 (매매일지, 자동매매분만) ──
    try:
        ledger_week = [
            e for _, e in notion_repo.fetch_ledger_closed_range(week_start, week_end)
            if e["holding_page_id"] in auto_ids
        ]
    except Exception:
        logger.exception("매매일지(이번 주) 조회 실패")
        report.has_errors = True
        ledger_week = []

    for e in ledger_week:
        if e["entry_price"] is None or e["exit_price"] is None or not e["shares"]:
            continue
        pl = (e["exit_price"] - e["entry_price"]) * e["shares"]
        report.realized_pl += pl
        if pl > 0:
            report.win_count += 1
        elif pl < 0:
            report.loss_count += 1

    # ── 3) 보유 현황 + 1)의 평가손익·계좌잔고 ──
    try:
        holdings = notion_repo.fetch_holdings(managed_by=notion_repo.MANAGED_AUTO)
    except Exception:
        logger.exception("보유종목 점검표(운용=자동) 조회 실패")
        report.has_errors = True
        holdings = []

    # 보유가 0건이어도 계좌 잔고 추이는 보여줘야 하므로 항상 토큰을 받는다.
    token: Optional[str] = None
    try:
        token = kis_client.get_access_token()
    except Exception:
        logger.exception("KIS 접근토큰 발급 실패 - 현재가·잔고 조회를 건너뜁니다")
        report.has_errors = True

    for page_id, inp in holdings:
        try:
            avg_price = notion_repo.fetch_holding_avg_price(page_id)
        except Exception:
            logger.warning("[%s] 평단가 조회 실패", inp.name)
            avg_price = None
        if avg_price is None:
            avg_price = inp.buy_price

        current_price: Optional[float] = None
        if token:
            try:
                current_price = float(kis_client.get_current_price(token, inp.ticker))
            except Exception:
                logger.warning("[%s] KIS 현재가 조회 실패 - pykrx 종가로 대체", inp.name)
        if current_price is None:
            # KIS 토큰 발급 자체가 실패했거나 현재가 조회만 실패한 경우 -
            # 이 배치는 토요일(장 마감 후)에 도니 "현재가"는 어차피 금요일
            # 종가와 같다. daily_report.py·evening_audit.py가 판정에 쓰는
            # 것과 같은 pykrx 경로라 KIS 상태와 무관하게 동작한다.
            try:
                current_price = core.latest_close(core.fetch_ohlcv(inp.ticker, days=5))
            except Exception:
                logger.warning("[%s] pykrx 종가 조회도 실패", inp.name)
                report.has_errors = True

        report.holdings.append(HoldingRow(
            ticker=inp.ticker, name=inp.name, avg_price=avg_price,
            shares=inp.shares, entry_atr=inp.entry_atr, units=inp.units,
            stop_loss=inp.prev_stop_loss, current_price=current_price,
        ))
        if current_price is not None:
            report.unrealized_pl += (current_price - avg_price) * inp.shares

    if token:
        try:
            balance = kis_client.get_account_balance(token)
            report.account_size = balance["account_size"]
            report.account_pct = (
                (report.account_size - core.DEFAULT_CAPITAL)
                / core.DEFAULT_CAPITAL * 100
            )
        except Exception:
            logger.exception("계좌 잔고 조회 실패")
            report.has_errors = True

    # ── 5) 누적 통계 (자동매매 시작 이후, 자동매매분만) ──
    try:
        ledger_all = [
            e for _, e in notion_repo.fetch_ledger_closed_range(AUTO_TRADE_START)
            if e["holding_page_id"] in auto_ids
        ]
    except Exception:
        logger.exception("매매일지(누적) 조회 실패")
        report.has_errors = True
        ledger_all = []

    report.cum_count = len(ledger_all)
    if ledger_all:
        decided = [
            e for e in ledger_all
            if e["entry_price"] is not None and e["exit_price"] is not None
        ]
        if decided:
            wins = sum(1 for e in decided if e["exit_price"] > e["entry_price"])
            report.cum_win_rate = wins / len(decided) * 100

        r_values = [e["r_multiple"] for e in ledger_all if e["r_multiple"] is not None]
        if r_values:
            report.cum_avg_r = sum(r_values) / len(r_values)

        hold_days = [
            (e["exit_date"] - e["entry_date"]).days
            for e in ledger_all
            if e["entry_date"] is not None and e["exit_date"] is not None
        ]
        if hold_days:
            report.cum_avg_hold_days = sum(hold_days) / len(hold_days)

        delay_values = [
            e["delay_days"] for e in ledger_all if e["delay_days"] is not None
        ]
        if delay_values:
            report.cum_avg_delay_days = sum(delay_values) / len(delay_values)

    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                         help="메일 발송을 생략하고 본문을 표준출력에 출력한다")
    args = parser.parse_args()

    today = datetime.now(KST).date()
    logger.info("=== 자동매매 주간 리포트 시작 (dry_run=%s) ===", args.dry_run)

    report = build_report(today)

    if args.dry_run:
        from mailer import build_weekly_subject, build_weekly_text
        print(build_weekly_subject(report))
        print()
        print(build_weekly_text(report))
        logger.info("=== dry-run 종료 (메일 미발송) ===")
        return 0

    from mailer import send_weekly_mail
    try:
        send_weekly_mail(report)
    except Exception:
        logger.exception("주간 리포트 메일 발송 실패")
        return 1

    logger.info("=== 자동매매 주간 리포트 완료 ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
