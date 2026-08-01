"""
daily_report.py – 매일 아침 포트폴리오 점검 진입점

로컬 테스트: python daily_report.py
GitHub Actions: workflow에서 직접 실행
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import date

# .env 지원 (로컬 테스트용, 없으면 무시)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from core import HoldingInput, HoldingResult, calc_portfolio_risk, evaluate_holding
from kakao import send_kakao_message
from mailer import send_report_mail
from notion_repo import fetch_holdings, update_holding

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


def _build_message(
    results: list[HoldingResult],
    total_capital: float,
    has_errors: bool,
) -> str:
    """카카오톡 메시지를 200자 이내로 생성한다.

    형식:
    [M/D] 조치 N건
    · 삼성전기 손절 (1,268,000)
    리스크 4.2%
    손절 대기 1건        ← 손절 판정이 있을 때만
    """
    today = date.today()
    header = f"[{today.month}/{today.day}]"

    # 조치 필요 종목 (유지가 아닌 것)
    actions = [r for r in results if r.is_action_needed]

    # 리스크 계산
    risk = calc_portfolio_risk(results, total_capital)

    lines: list[str] = []

    if actions:
        lines.append(f"{header} 조치 {len(actions)}건")
        for r in actions:
            price_str = f"{r.current_price:,.0f}" if r.current_price else "N/A"
            lines.append(f"· {r.name} {r.verdict} ({price_str})")
    else:
        lines.append(f"{header} 조치 없음")

    # 리스크 라인
    risk_line = f"리스크 {risk.total_risk_pct}%"
    if risk.risk_warning:
        risk_line += " ⚠️초과"
    lines.append(risk_line)

    # 손절 대기는 리스크 %와 별도로 표시한다.
    # 손절선을 이미 깬 종목은 리스크 합계에서 0으로 잡히기 때문이다.
    if risk.stop_pending:
        lines.append(f"손절 대기 {risk.stop_pending}건")

    if has_errors:
        lines.append("⚠️일부 조회 실패")

    msg = "\n".join(lines)

    # 200자 초과 시 종목 줄이기
    while len(msg) > 200 and len(lines) > 3:
        # 마지막 종목 라인 제거 (헤더, 리스크, 에러 보존)
        for i in range(len(lines) - 1, 0, -1):
            if lines[i].startswith("·"):
                lines.pop(i)
                # 헤더의 건수 유지, 생략 표시 추가
                if not any("외" in l for l in lines):
                    lines.insert(i, "  외 생략")
                msg = "\n".join(lines)
                break
        else:
            break

    # 최종 200자 보장
    if len(msg) > 200:
        msg = msg[:199] + "…"

    return msg


def main() -> None:
    logger.info("=== 일일 포트폴리오 점검 시작 ===")

    # 환경변수 확인
    total_capital = float(os.environ.get("TOTAL_CAPITAL", "0"))
    if total_capital <= 0:
        logger.error("TOTAL_CAPITAL 환경변수가 설정되지 않았거나 0 이하입니다.")
        sys.exit(1)

    # 1) 노션에서 보유 종목 읽기
    try:
        holdings = fetch_holdings()
    except Exception:
        logger.exception("노션 DB 조회 실패")
        try:
            send_kakao_message(
                f"[{date.today().month}/{date.today().day}] ❌ 노션 조회 실패"
            )
        except Exception:
            logger.exception("카카오톡 전송도 실패")
        sys.exit(1)

    if not holdings:
        logger.info("보유 종목 없음. 종료.")
        return

    # 2) 종목별 평가
    results: list[HoldingResult] = []
    rows: list[tuple[HoldingInput, HoldingResult]] = []  # 메일 표용
    has_errors = False

    for page_id, inp in holdings:
        logger.info("평가 중: %s (%s)", inp.name, inp.ticker)
        result = evaluate_holding(inp, total_capital)
        results.append(result)
        rows.append((inp, result))

        if result.error:
            has_errors = True
            logger.error("[%s] %s", inp.name, result.error)

        # 3) 노션에 결과 기록
        try:
            update_holding(page_id, result, inp)
        except Exception:
            logger.exception("[%s] 노션 업데이트 실패", inp.name)
            has_errors = True

    # 4) 리스크 요약
    risk = calc_portfolio_risk(results, total_capital)
    groups = (
        " | ".join(
            f"{g} {pct:.2f}%" for g, pct in risk.group_risk_pct.items()
        )
        or "없음"
    )
    logger.info(
        "전체 리스크: %.2f%% | 손절 대기: %d건 | 상관군: %s",
        risk.total_risk_pct,
        risk.stop_pending,
        groups,
    )
    if risk.risk_warning:
        logger.warning("⚠️ 전체 리스크 6%% 초과: %.2f%%", risk.total_risk_pct)

    # 5) 카카오톡 전송
    msg = _build_message(results, total_capital, has_errors)
    logger.info("카카오톡 메시지:\n%s", msg)

    try:
        send_kakao_message(msg)
    except Exception:
        logger.exception("카카오톡 전송 실패")
        # 카톡 실패해도 로그에는 남았으므로 종료하지 않음

    # 6) 상세 리포트 메일 전송 (실패해도 전체 실행은 계속)
    try:
        send_report_mail(rows, risk, has_errors)
    except Exception:
        logger.exception("메일 발송 실패")

    logger.info("=== 일일 포트폴리오 점검 완료 ===")


if __name__ == "__main__":
    main()
