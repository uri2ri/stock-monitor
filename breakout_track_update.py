"""
breakout_track_update.py – 돌파 후 미진입 추적 일별 갱신 진입점

야간 전종목 스캔(scan_all.py)이 끝난 뒤 하루 한 번 돈다. 확정 종가
(data/scan_latest.csv)만 읽고, 장중 현재가는 쓰지 않는다 - 추적 화면의
가격은 항상 "어느 날 종가인지"가 분명해야 한다.

최초 기준선·최초 ATR은 건드리지 않는다. 갱신되는 건 최신 종가·가격
기준일·최신 돌파선과 거기서 파생되는 값(최초 기준선 대비 %·ATR 배수)뿐이다.

주문·알림·매매 판단을 하지 않는다. 노션의 '돌파 추적' DB 외에는
아무것도 쓰지 않는다.

로컬 실행:
    python breakout_track_update.py
    python breakout_track_update.py --dry-run   # 계산만 하고 쓰지 않는다
"""

from __future__ import annotations

import argparse
import logging
import sys

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import breakout_tracker
import scan_all

logger = logging.getLogger(__name__)


def _preview(scan_frame) -> int:
    """드라이런 - 저장 없이 계산 결과만 출력한다."""
    tracks = breakout_tracker.list_tracks()
    if not tracks:
        print("활성 추적 기록이 없습니다.")
        return 0

    rows = breakout_tracker.scan_rows_by_ticker(scan_frame)
    for track in tracks:
        row = rows.get(track["ticker"])
        fields = breakout_tracker.compute_daily(track, row)
        print("─" * 60)
        print(f"{track['name']}({track['ticker']}) "
              f"최초 감지 {track.get('first_detected_at')}")
        print(f"  최초 기준선 {track['first_threshold']:,.0f} · "
              f"최초 ATR {track['first_atr']:,.0f}")
        for key in ("latest_close", "latest_date", "change_pct", "atr_mult",
                    "level_state", "new_breakout", "dist_to_line",
                    "dist_to_line_atr", "refresh_state", "refresh_note"):
            if key in fields:
                print(f"  {key}: {fields[key]}")
    return 0


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
    )

    parser = argparse.ArgumentParser(description="돌파 후 미진입 추적 일별 갱신")
    parser.add_argument("--dry-run", action="store_true",
                        help="노션에 쓰지 않고 계산 결과만 출력한다")
    args = parser.parse_args()

    if not breakout_tracker.is_enabled():
        # 기능을 아직 켜지 않은 상태가 정상 종료다. 워크플로를 빨갛게
        # 만들지 않는다 - 이 기능이 꺼져 있다고 야간 스캔이 실패한 건
        # 아니기 때문이다.
        logger.info("돌파 추적 DB(%s) 미설정 - 갱신을 건너뜁니다. "
                    "설정 방법은 BREAKOUT_TRACKING.md 참고.",
                    breakout_tracker.DB_ENV)
        return 0

    scan_frame = scan_all.load_scan()
    if args.dry_run:
        return _preview(scan_frame)

    summary = breakout_tracker.refresh_from_scan(scan_frame)
    if summary.get("error"):
        logger.warning("갱신 미완료: %s", summary["error"])
    logger.info("갱신 요약: %s", summary)
    return 1 if summary.get('error') or summary.get('failed') else 0


if __name__ == "__main__":
    sys.exit(main())
