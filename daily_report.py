"""
daily_report.py – 매일 아침 포트폴리오 점검 진입점

로컬 테스트: python daily_report.py
GitHub Actions: workflow에서 직접 실행
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import date
from pathlib import Path

# .env 지원 (로컬 테스트용, 없으면 무시)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import core
from core import HoldingInput, HoldingResult, calc_portfolio_risk, evaluate_holding
from kakao import send_kakao_message
from mailer import send_report_mail
from notion_repo import fetch_favorites, fetch_holdings, update_holding

# intraday_watch.py가 10분마다 읽는다. 장중에 노션을 매번 조회하지 않게
# 아침 배치가 하루 한 번 계산해 파일로 남긴다.
DATA_DIR = Path(__file__).resolve().parent / "data"
CORR_UNITS_PATH = DATA_DIR / "corr_units.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


def _write_corr_units(holdings: list[HoldingInput], total_capital: float) -> None:
    """상관군별 누적 유닛을 파일로 저장한다 (intraday_watch.py 공용).

    실패해도 아침 리포트 전체를 막지 않는다 – 로그만 남기고 직전
    파일을 그대로 둔다 (scan_latest.csv와 같은 태도).
    """
    corr = core.calc_corr_units(holdings, total_capital)
    payload = {
        "date": date.today().isoformat(),
        "total_units": round(corr.total_units, 3),
        "groups": {g: round(u, 3) for g, u in corr.group_units.items()},
    }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CORR_UNITS_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    logger.info(
        "상관군 유닛 저장: 전체 %.2f · 상관군 %d개",
        corr.total_units, len(corr.group_units),
    )


def _build_message(
    results: list[HoldingResult],
    total_capital: float,
    has_errors: bool,
    favorites: list[dict] | None = None,
    stop_updates: list[tuple[HoldingInput, HoldingResult]] | None = None,
) -> str:
    """카카오톡 메시지를 200자 이내로 생성한다.

    형식:
    [M/D] 조치 N건
    · 삼성전기 손절 (1,268,000)
    리스크 4.2%
    손절 대기 1건        ← 손절 판정이 있을 때만
    손절선 갱신 N건: 종목1 종목2  ← 갱신 알림 대상이 있을 때만
    즐겨찾기 진입가능 N건  ← 즐겨찾기 중 진입가능 판정이 있을 때만

    즐겨찾기는 200자 제한 탓에 종목 내용은 넣지 않고 건수 한 줄만
    붙인다. 딥링크도 넣지 않는다 – 자세한 내용은 메일에서 본다.
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

    if stop_updates:
        names = " ".join(res.name for _, res in stop_updates)
        lines.append(f"손절선 갱신 {len(stop_updates)}건: {names}")

    if has_errors:
        lines.append("⚠️일부 조회 실패")

    fav_enterable = sum(
        1 for f in (favorites or []) if f.get("verdict") == "진입가능"
    )
    if fav_enterable:
        lines.append(f"즐겨찾기 진입가능 {fav_enterable}건")

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

    # 4.5) 즐겨찾기 (보유종목 점검표와 완전히 별개 DB). 실패해도 아침
    # 리포트 전체를 막지 않는다 – 빈 목록으로 진행한다.
    try:
        favorites = [item for _, item in fetch_favorites()]
    except Exception:
        logger.exception("즐겨찾기 조회 실패")
        favorites = []

    # 4.6) 손절선 갱신 알림 대상 – evaluate_holding이 종목별로 이미 판정했다.
    # 메일 표에 '기존' 값(inp.last_alerted_stop)이 필요해 쌍으로 들고 다닌다.
    stop_updates = [(inp, res) for inp, res in rows if res.stop_update_alert]
    if stop_updates:
        logger.info(
            "손절선 갱신 알림 %d건: %s",
            len(stop_updates), ", ".join(res.name for _, res in stop_updates),
        )

    # 4.7) 상관군 유닛 저장 (intraday_watch.py가 장중에 읽는다). 실패해도
    # 아침 리포트 전체를 막지 않는다 – 로그만 남기고 계속한다.
    try:
        _write_corr_units([inp for inp, _ in rows], total_capital)
    except Exception:
        logger.exception("상관군 유닛 파일 저장 실패")

    # 5) 카카오톡 전송
    msg = _build_message(
        results, total_capital, has_errors, favorites, stop_updates
    )
    logger.info("카카오톡 메시지:\n%s", msg)

    try:
        send_kakao_message(msg)
    except Exception:
        logger.exception("카카오톡 전송 실패")
        # 카톡 실패해도 로그에는 남았으므로 종료하지 않음

    # 6) 상세 리포트 메일 전송 (실패해도 전체 실행은 계속)
    try:
        send_report_mail(
            rows, risk, has_errors, favorites=favorites, stop_updates=stop_updates
        )
    except Exception:
        logger.exception("메일 발송 실패")

    logger.info("=== 일일 포트폴리오 점검 완료 ===")


if __name__ == "__main__":
    main()
