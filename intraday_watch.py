"""
intraday_watch.py – 장중 돌파 감시 (10분 간격)

야간 스캔(scan_all.py)이 남긴 data/scan_latest.csv에서 돌파 임박 종목을
추려, 장중 현재가가 실제로 직전 20일 고가를 넘는 순간 카톡으로 알린다.

판정선은 core.CHASE_ATR_MULT를 그대로 쓴다 (app.py의 진입 점검 화면과
같은 값). 이 파일에 숫자를 다시 적지 않는다.

같은 날 같은 종목은 한 번만 알린다 (data/alerted_YYYYMMDD.json).
노션·메일에 쓰지 않는다.

로컬 실행:
    python intraday_watch.py
    python intraday_watch.py --dry-run     # 카톡 발송 없이 출력만
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import pandas as pd
import requests

import core
import scan_all

logger = logging.getLogger(__name__)

# ── 설정 ────────────────────────────────────────────────────

DATA_DIR = scan_all.DATA_DIR
KST = ZoneInfo("Asia/Seoul")

# 워치리스트에 올릴 거리 상한 (×ATR). 넓히면 조회 종목이 늘 뿐
# 돌파 판정 자체는 변하지 않는다.
WATCH_THRESHOLD = float(os.environ.get("WATCH_THRESHOLD") or 1.0)
MAX_WATCH = 100

NAVER_URL = "https://polling.finance.naver.com/api/realtime/domestic/stock/{codes}"
# 이 엔드포인트는 콤마로 여러 종목을 한 번에 준다. 100종목이 개별 100회가
# 아니라 5회로 끝나므로, 차단 위험도 실행 시간도 크게 준다.
BATCH_SIZE = 20
TIMEOUT = 5
RETRY_MAX = 2
BATCH_SLEEP = 0.2
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)

# 카톡 한 통 200자 제한(kakao.MAX_TEXT_LEN). 한 줄 약 20자라 4종목이면
# 헤더 포함 100자 안쪽이다. 넘치면 자르지 않고 여러 통으로 나눈다.
PER_MESSAGE = 4
MAX_MESSAGES = 5           # 그 이상은 "외 N종목"으로 접는다

STATUS_ENTER = "진입가능"
STATUS_CHASE = "추격금지"


def alerted_path(day: str) -> Path:
    return DATA_DIR / f"alerted_{day}.json"


# ── 워치리스트 ──────────────────────────────────────────────

def load_watchlist(threshold: float = WATCH_THRESHOLD,
                   limit: int = MAX_WATCH) -> pd.DataFrame:
    """scan_latest.csv에서 감시 대상을 고른다. 없으면 빈 DataFrame.

    아직 돌파하지 않은 종목(status="임박")만 본다. 어젯밤 이미 돌파로
    기록된 종목은 '넘는 순간'이 지났으므로 장 시작과 동시에 전부
    알림이 나가버린다 — 그건 새 신호가 아니라 어제 스캔의 반복이다.
    """
    frame = scan_all.load_scan()
    if frame is None:
        logger.warning("%s가 없습니다. 야간 스캔을 먼저 돌리세요.",
                       scan_all.LATEST_PATH.name)
        return pd.DataFrame()

    watch = frame[
        (frame["status"] == scan_all.STATUS_NEAR)
        & (frame["dist_atr"] <= threshold)
    ]
    watch = watch.sort_values("dist_atr").head(limit)
    logger.info("워치리스트 %d종목 (기준일 %s · dist_atr ≤ %.1f)",
                len(watch),
                frame["scan_date"].iloc[0] if not frame.empty else "?",
                threshold)
    return watch


# ── 시세 조회 (네이버 JSON) ─────────────────────────────────

def _parse(datas: list[dict]) -> dict[str, float]:
    """응답에서 종목코드 → 현재가. 거래정지·장마감은 제외한다.

    장이 열려 있지 않으면 closePrice가 직전 거래일 종가다. 그대로 쓰면
    휴장일에 어제 종가로 돌파 알림이 나간다 — marketStatus로 막는다.
    """
    prices: dict[str, float] = {}
    for item in datas:
        code = str(item.get("itemCode", "")).zfill(6)
        if item.get("marketStatus") != "OPEN":
            logger.debug("[%s] 장중 아님 (%s) – 건너뜀",
                         code, item.get("marketStatus"))
            continue
        if item.get("tradableStatus") != "tradable":
            logger.debug("[%s] 거래불가 (%s) – 건너뜀",
                         code, item.get("tradableStatus"))
            continue
        raw = str(item.get("closePrice", "")).replace(",", "").strip()
        try:
            price = float(raw)
        except ValueError:
            logger.debug("[%s] 현재가 해석 불가: %r", code, raw)
            continue
        if price > 0:
            prices[code] = price
    return prices


def _fetch_batch(codes: list[str]) -> dict[str, float]:
    """한 배치. 실패하면 빈 dict (전체를 멈추지 않는다)."""
    url = NAVER_URL.format(codes=",".join(codes))
    for attempt in range(RETRY_MAX + 1):
        try:
            resp = requests.get(
                url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT,
            )
            resp.raise_for_status()
            return _parse(resp.json().get("datas", []))
        except Exception as e:              # noqa: BLE001 – 무엇이든 재시도
            if attempt < RETRY_MAX:
                logger.debug("배치 조회 실패 (%d/%d): %s",
                             attempt + 1, RETRY_MAX + 1, e)
                time.sleep(BATCH_SLEEP * 2)
            else:
                logger.warning("배치 조회 포기 (%d종목): %s", len(codes), e)
    return {}


def fetch_prices(codes: list[str]) -> dict[str, float]:
    """현재가 일괄 조회. 실패한 배치는 조용히 빠진다."""
    prices: dict[str, float] = {}
    for i in range(0, len(codes), BATCH_SIZE):
        batch = codes[i:i + BATCH_SIZE]
        prices.update(_fetch_batch(batch))
        time.sleep(BATCH_SLEEP)

    missing = len(codes) - len(prices)
    if missing:
        logger.info("현재가 확보 %d/%d종목 (미확보 %d)",
                    len(prices), len(codes), missing)
    return prices


# ── 알림 이력 ───────────────────────────────────────────────

def load_alerted(day: str) -> dict[str, dict]:
    """오늘 이미 알린 종목. 파일이 깨졌으면 빈 dict로 시작한다."""
    path = alerted_path(day)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("alerted", {})
    except Exception as e:                  # noqa: BLE001
        logger.warning("알림 이력을 읽지 못했습니다 (%s) – 새로 시작합니다", e)
        return {}


def save_alerted(day: str, alerted: dict[str, dict]) -> Path:
    path = alerted_path(day)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"date": day, "alerted": alerted},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


# ── 판정 ────────────────────────────────────────────────────

def judge(row: pd.Series, price: float) -> Optional[dict]:
    """현재가가 직전 20일 고가를 넘었는가. 아니면 None.

    돌파 폭이 CHASE_ATR_MULT×ATR을 넘으면 추격 구간으로 본다 —
    app.py의 entry_state()와 같은 기준이다.
    """
    high20 = float(row["high20"])
    if price <= high20:
        return None

    atr = float(row["atr20"])
    gap_atr = (price - high20) / atr if atr > 0 else 0.0
    return {
        "ticker": str(row["ticker"]),
        "name": str(row["name"]),
        "price": price,
        "high20": high20,
        "atr20": atr,
        "gap_atr": gap_atr,
        "unit_shares": int(row["unit_shares"]),
        "status": (STATUS_ENTER if gap_atr <= core.CHASE_ATR_MULT
                   else STATUS_CHASE),
    }


# ── 카톡 메시지 ─────────────────────────────────────────────

def build_messages(hits: list[dict], now: datetime) -> list[str]:
    """진입가능 종목만 한 줄씩. 200자를 넘기지 않게 여러 통으로 나눈다.

    추격금지는 넣지 않는다 — 어차피 사지 않을 종목이라 알림으로서
    행동을 유발하지 않고, 진짜 신호를 밀어낸다. CSV와 스트림릿에는
    그대로 남아 있다.

    20일 고가는 뺐다. 돌파했다는 사실 자체가 알림이고, 정확한 수치는
    스트림릿에서 본다.
    """
    enterable = [h for h in hits if h["status"] == STATUS_ENTER]
    if not enterable:
        return []

    enterable.sort(key=lambda h: h["gap_atr"])
    header = f"[돌파]{now.strftime('%H:%M')}"

    shown, overflow = enterable[:PER_MESSAGE * MAX_MESSAGES], 0
    if len(enterable) > len(shown):
        overflow = len(enterable) - len(shown)

    messages: list[str] = []
    for i in range(0, len(shown), PER_MESSAGE):
        chunk = shown[i:i + PER_MESSAGE]
        lines = [
            f"{h['name']} {h['price']:,.0f} +{h['gap_atr']:.2f}N"
            for h in chunk
        ]
        if overflow and i + PER_MESSAGE >= len(shown):
            lines.append(f"외 {overflow}종목")
        messages.append("\n".join([header] + lines))
    return messages


# ── 실행 ────────────────────────────────────────────────────

def run(dry_run: bool = False) -> int:
    now = datetime.now(KST)
    day = now.strftime("%Y%m%d")

    watch = load_watchlist()
    if watch.empty:
        logger.info("감시할 종목이 없습니다.")
        return 0

    alerted = load_alerted(day)
    pending = watch[~watch["ticker"].isin(list(alerted))]
    if pending.empty:
        logger.info("워치리스트 %d종목이 모두 이미 알림 완료입니다.", len(watch))
        return 0

    codes = pending["ticker"].tolist()
    prices = fetch_prices(codes)

    hits: list[dict] = []
    for _, row in pending.iterrows():
        price = prices.get(str(row["ticker"]))
        if price is None:
            continue            # 조회 실패는 조용히 건너뛴다
        hit = judge(row, price)
        if hit:
            hits.append(hit)

    if not hits:
        logger.info("신규 돌파 없음 (감시 %d종목)", len(pending))
        return 0

    for h in hits:
        logger.info("돌파: %s(%s) %s원 → %s (+%.2fN)",
                    h["name"], h["ticker"], f"{h['price']:,.0f}",
                    h["status"], h["gap_atr"])

    messages = build_messages(hits, now)
    if not messages:
        # 전부 추격금지 – 카톡은 보내지 않지만 이력에는 남긴다.
        # 남기지 않으면 10분 뒤 같은 종목을 또 판정하게 된다.
        logger.info("돌파 %d종목이 모두 추격 구간 – 발송하지 않습니다", len(hits))

    if dry_run:
        for msg in messages:
            print("─" * 40)
            print(msg)
            print(f"({len(msg)}자)")
        return 0

    if messages:
        from kakao import send_kakao_message
        for msg in messages:
            send_kakao_message(msg)
        logger.info("카톡 %d통 발송", len(messages))

    for h in hits:
        alerted[h["ticker"]] = {
            "time": now.strftime("%H:%M"),
            "name": h["name"],
            "price": h["price"],
            "gap_atr": round(h["gap_atr"], 3),
            "status": h["status"],
        }
    path = save_alerted(day, alerted)
    logger.info("알림 이력 저장: %s (%d종목)", path.name, len(alerted))
    return 0


def main() -> int:
    import argparse

    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s [%(levelname)s] %(message)s",
        stream=sys.stdout,
    )
    logger.setLevel(logging.INFO)

    parser = argparse.ArgumentParser(description="장중 돌파 감시")
    parser.add_argument("--dry-run", action="store_true",
                        help="카톡을 보내지 않고 메시지만 출력한다")
    args = parser.parse_args()

    try:
        return run(dry_run=args.dry_run)
    except Exception as e:                  # noqa: BLE001
        # 10분마다 도는 작업이다. 한 번 실패해도 다음 실행이 이어받게
        # 비정상 종료로 남기되, 다른 워크플로에는 영향이 없다.
        logger.error("장중 감시 실패: %s", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
