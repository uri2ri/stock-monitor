"""
breakout_tracker.py – 돌파 후 미진입 추적

목적
    돌파 판정은 났는데 자금 부족·상한 등으로 매수하지 못한 종목이,
    그 뒤 새 20일 고가를 다시 넘지 않더라도 흐름을 계속 볼 수 있게
    "최초 관측 돌파"를 한 건씩 남기고 매일 종가로 갱신한다.

이 모듈이 하지 않는 것 (의도적으로 범위 밖)
    - 매수 추천을 만들지 않는다. 점수·등급·"지금 사라" 같은 판단 문구를
      쓰지 않는다.
    - 과거 신호로 주문을 내지 않는다. 주문 경로(kis_client)는 이 모듈을
      읽지 않는다 - 단방향으로 기록만 받는다.
    - 추적 종목을 보유수량·상관군 유닛·리스크 계산에 넣지 않는다.
      (그쪽은 노션 보유종목 점검표 / KIS 잔고만 본다.)

용어
    "최초"는 **이 프로그램이 처음 관측한 돌파**를 뜻한다. 시장에서의
    최초 돌파가 아니다 - 장중 감시는 10분 간격이고, 워치리스트에 들어온
    종목만 보며, 스캔·조회가 실패한 회차는 아예 보지 못한다. 화면·리포트
    문구도 전부 "최초 감지"로 쓴다.

격리 원칙
    이 모듈의 공개 함수는 **예외를 밖으로 내보내지 않는다**. 추적 DB가
    설정되지 않았거나(기본값) 노션 조회·쓰기가 실패해도 호출부(장중 감시·
    자동매수)는 아무 영향 없이 계속 진행해야 한다. 실패는 로그로만 남고
    반환값(False/None/빈 목록)으로 알린다.
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime
from typing import Any, Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

KST = ZoneInfo("Asia/Seoul")

# ── 어휘 ────────────────────────────────────────────────────

STATUS_ACTIVE = "추적중"
STATUS_CLOSED = "종료"

# 추적 종료는 "근거 있는 사건"으로만 한다. 며칠이 지나면 자동으로
# 닫는 만료 기간은 두지 않았다 - 적정 기간을 정할 근거가 아직 없어
# 임의의 숫자를 넣지 않는다(PROJECT_STATUS.md의 미결정 사항).
CLOSE_FILLED = "체결확인"
CLOSE_USER = "사용자종료"

# 자동매수 결과. "기록 없음"과 "미주문"은 다르다 - 기록이 없다는 건
# 아직 아무 실행 결과도 받지 못했다는 뜻이지, 자금이 부족했다거나
# 체결되지 않았다는 뜻이 아니다.
OUTCOME_NONE = ""
OUTCOME_NOT_ORDERED = "미주문"      # 게이트에서 막혀 주문 자체를 내지 않음
OUTCOME_UNKNOWN = "상태미확인"       # 주문이 나갔는지 알 수 없음
OUTCOME_ORDER_SENT = "주문접수"      # 주문은 접수됨 (체결 여부는 별개)
OUTCOME_FILLED = "체결확인"          # 체결 수량을 확인함

# 결과를 덮어쓸 때의 우선순위. 약한 결과가 강한 결과를 지우지 못하게
# 한다 - 특히 "주문접수/상태미확인"을 나중의 "미주문"으로 덮으면 화면에
# 미진입이 확정된 것처럼 보이게 된다.
_OUTCOME_RANK = {
    OUTCOME_NONE: 0,
    OUTCOME_NOT_ORDERED: 1,
    OUTCOME_UNKNOWN: 2,
    OUTCOME_ORDER_SENT: 3,
    OUTCOME_FILLED: 4,
}

# 미진입이 "확정"이라고 말할 수 있는 결과는 이것뿐이다. 나머지는
# 화면에서 미확인으로 표시한다.
CONFIRMED_NO_ENTRY = (OUTCOME_NOT_ORDERED,)

LEVEL_ABOVE = "최초선위"
LEVEL_BELOW = "최초선아래"
LEVEL_UNKNOWN = "확인불가"

NEW_BREAKOUT_YES = "해당"
NEW_BREAKOUT_NO = "미해당"
NEW_BREAKOUT_UNKNOWN = "확인불가"

REFRESH_OK = "갱신됨"
REFRESH_FAIL = "갱신실패"

DB_ENV = "NOTION_BREAKOUT_TRACK_DB_ID"


# ── 저장소 ──────────────────────────────────────────────────
#
# 기본 저장소는 노션의 별도 "돌파 추적" DB다. 보유종목 점검표·자동주문
# 기록·매매일지와 칸을 섞지 않는다 - 저 DB들은 실제 보유·주문의 근거라
# "사지 않은 종목"이 섞이면 그 자체로 오염이다.
#
# 테스트는 set_store()로 메모리 저장소를 끼운다. 이 모듈의 로직(중복
# 방지·최초값 보존·결과 우선순위·일별 갱신)은 저장소 종류와 무관하다.

class MemoryStore:
    """테스트·드라이런용 메모리 저장소. 노션에 접근하지 않는다."""

    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}
        self._seq = 0

    def configured(self) -> bool:
        return True

    def find_active(self, ticker: str) -> Optional[dict]:
        for key, row in self.rows.items():
            if row.get("ticker") == ticker and row.get("status") == STATUS_ACTIVE:
                return {**row, "key": key}
        return None

    def list_tracks(self, include_closed: bool = False) -> list[dict]:
        out = []
        for key, row in self.rows.items():
            if not include_closed and row.get("status") != STATUS_ACTIVE:
                continue
            out.append({**row, "key": key})
        return out

    def create(self, record: dict) -> str:
        self._seq += 1
        key = f"mem-{self._seq}"
        self.rows[key] = dict(record)
        return key

    def update(self, key: str, fields: dict) -> None:
        if key not in self.rows:
            raise KeyError(key)
        self.rows[key].update(fields)


class NotionStore:
    """운영 저장소. notion_repo의 '돌파 추적' DB 함수만 호출한다."""

    def configured(self) -> bool:
        return bool(os.environ.get("NOTION_TOKEN", "").strip()
                    and os.environ.get(DB_ENV, "").strip())

    def find_active(self, ticker: str) -> Optional[dict]:
        import notion_repo
        return notion_repo.find_active_breakout_track(ticker)

    def list_tracks(self, include_closed: bool = False) -> list[dict]:
        import notion_repo
        return notion_repo.fetch_breakout_tracks(include_closed=include_closed)

    def create(self, record: dict) -> str:
        import notion_repo
        return notion_repo.create_breakout_track(record)

    def update(self, key: str, fields: dict) -> None:
        import notion_repo
        notion_repo.update_breakout_track(key, fields)


_store: Any = NotionStore()


def set_store(store: Any) -> Any:
    """저장소를 갈아끼운다. 이전 저장소를 돌려준다 (테스트에서 복원용)."""
    global _store
    previous = _store
    _store = store
    return previous


def get_store() -> Any:
    return _store


def is_enabled() -> bool:
    """추적 기능이 켜져 있는가. 설정 확인 자체가 실패해도 False."""
    try:
        return bool(_store.configured())
    except Exception as e:                  # noqa: BLE001
        logger.warning("돌파 추적 설정 확인 실패 - 비활성으로 다룹니다: %s", e)
        return False


# ── 안전 래퍼 ───────────────────────────────────────────────

def _guard(label: str, fn, default):
    """추적 기능의 어떤 실패도 호출부로 새지 않게 막는다."""
    if not is_enabled():
        logger.debug("돌파 추적 비활성 - %s 건너뜀", label)
        return default
    try:
        return fn()
    except Exception as e:                  # noqa: BLE001
        logger.warning("돌파 추적 %s 실패 - 기존 동작에는 영향 없습니다: %s",
                       label, e)
        return default


# ── 최초 기록 ───────────────────────────────────────────────

def _first_values_ok(hit: dict) -> bool:
    """최초 기준선·가격·ATR이 전부 실수로 확인되는가.

    하나라도 비어 있으면 추적 기록을 만들지 않는다 - 빈 칸을 0이나
    추정치로 채우면 나중에 '최초 기준선 대비 %'가 통째로 거짓말이 된다.
    """
    for name in ("high20", "price", "atr20"):
        value = hit.get(name)
        try:
            number = float(value)
        except (TypeError, ValueError):
            return False
        if number <= 0:
            return False
    return True


def record_breakout(hit: dict, now: Optional[datetime] = None) -> Optional[str]:
    """돌파 판정 1건을 추적 기록으로 남긴다. 이미 활성 기록이 있으면 만들지 않는다.

    hit은 intraday_watch.judge()의 반환 dict와 같은 모양이다
    ({"ticker","name","price","high20","atr20","gap_atr","status",...}).

    Returns:
        새로 만든 기록 key. 이미 있거나 만들지 않았으면 None.

    중복 생성 방지는 저장소 조회(find_active)로 한다 - 프로세스가 재시작
    되거나 같은 회차가 두 번 돌아도 활성 기록이 이미 있으면 새로 만들지
    않는다. **조회 자체가 실패하면 만들지 않는다**(fail-closed) - 모르는
    상태에서 만들면 같은 돌파가 두 건으로 갈라진다.
    """
    ticker = str(hit.get("ticker") or "").strip()
    if not ticker:
        return None
    if not _first_values_ok(hit):
        logger.warning(
            "[%s] 돌파 추적 기록 생략 - 최초 기준선/가격/ATR이 확인되지 "
            "않습니다(임의 값으로 채우지 않습니다)", ticker,
        )
        return None

    now = now or datetime.now(KST)

    def _do() -> Optional[str]:
        existing = _store.find_active(ticker)
        if existing:
            # 최초 값은 절대 덮어쓰지 않는다. 같은 종목이 며칠 뒤 다시
            # 돌파해도 활성 추적 건의 "최초"는 처음 감지한 그 값이다.
            logger.info("[%s] 이미 추적 중 - 최초 기록을 유지합니다 (key=%s)",
                        ticker, existing.get("key"))
            return None
        record = {
            "ticker": ticker,
            "name": str(hit.get("name") or ticker),
            "first_detected_at": now.astimezone(KST),
            "first_threshold": float(hit["high20"]),
            "first_price": float(hit["price"]),
            "first_atr": float(hit["atr20"]),
            "status": STATUS_ACTIVE,
            "outcome": OUTCOME_NONE,
        }
        key = _store.create(record)
        logger.info("[%s] 돌파 추적 시작 - 최초 감지 %s · 기준선 %s · 가격 %s",
                    ticker, now.strftime("%Y-%m-%d %H:%M"),
                    f"{record['first_threshold']:,.0f}",
                    f"{record['first_price']:,.0f}")
        return key

    return _guard(f"최초 기록({ticker})", _do, None)


def record_breakouts(hits: list[dict], now: Optional[datetime] = None) -> int:
    """돌파 판정 여러 건. 만들어진 기록 수를 돌려준다."""
    if not hits:
        return 0
    created = 0
    for hit in hits:
        if record_breakout(hit, now):
            created += 1
    return created


# ── 실행 결과(미진입 사유) ──────────────────────────────────

def _set_outcome(ticker: str, outcome: str, reason: str,
                 now: Optional[datetime] = None) -> bool:
    now = now or datetime.now(KST)

    def _do() -> bool:
        track = _store.find_active(ticker)
        if not track:
            # 추적 대상이 아니면 아무것도 만들지 않는다. 결과 기록이
            # 추적 기록을 새로 만드는 일은 없다 - 최초 기준선·ATR을
            # 모르는 채로 기록이 생기면 갱신 자체가 성립하지 않는다.
            logger.debug("[%s] 활성 추적 기록 없음 - 결과 기록 생략", ticker)
            return False
        current = track.get("outcome") or OUTCOME_NONE
        if _OUTCOME_RANK.get(outcome, 0) < _OUTCOME_RANK.get(current, 0):
            logger.debug("[%s] 기존 결과(%s)가 더 확정적이라 %s로 덮지 않습니다",
                         ticker, current, outcome)
            return False
        _store.update(track["key"], {
            "outcome": outcome,
            "outcome_reason": reason,
            "outcome_at": now.astimezone(KST),
        })
        logger.info("[%s] 돌파 추적 결과 기록: %s (%s)", ticker, outcome, reason)
        return True

    return _guard(f"결과 기록({ticker})", _do, False)


def record_no_entry(ticker: str, reason: str,
                    now: Optional[datetime] = None) -> bool:
    """주문을 내지 않았음이 확실한 경우. reason은 실제 실행 결과여야 한다."""
    return _set_outcome(ticker, OUTCOME_NOT_ORDERED, reason, now)


def record_unknown(ticker: str, reason: str,
                   now: Optional[datetime] = None) -> bool:
    """주문이 나갔는지 알 수 없는 경우(전송 중 예외 등)."""
    return _set_outcome(ticker, OUTCOME_UNKNOWN, reason, now)


def record_order_sent(ticker: str, order_no: str = "",
                      now: Optional[datetime] = None) -> bool:
    """주문 접수. 체결 여부는 아직 모른다 - 추적을 닫지 않는다."""
    detail = f"주문 접수(주문번호 {order_no})" if order_no else "주문 접수"
    return _set_outcome(ticker, OUTCOME_ORDER_SENT, detail, now)


def record_fill(ticker: str, qty: Optional[int] = None,
                price: Optional[float] = None,
                now: Optional[datetime] = None) -> bool:
    """체결 확인. 근거 있는 종료 사건이므로 추적을 닫는다."""
    now = now or datetime.now(KST)
    parts = ["체결 확인"]
    if qty:
        parts.append(f"{int(qty):,}주")
    if price:
        parts.append(f"@{float(price):,.0f}")
    detail = " ".join(parts)

    def _do() -> bool:
        track = _store.find_active(ticker)
        if not track:
            logger.debug("[%s] 활성 추적 기록 없음 - 체결 기록 생략", ticker)
            return False
        _store.update(track["key"], {
            "outcome": OUTCOME_FILLED,
            "outcome_reason": detail,
            "outcome_at": now.astimezone(KST),
            "status": STATUS_CLOSED,
            "closed_reason": CLOSE_FILLED,
            "closed_at": now.astimezone(KST).date(),
        })
        logger.info("[%s] 돌파 추적 종료 - %s", ticker, detail)
        return True

    return _guard(f"체결 기록({ticker})", _do, False)


def close_track(ticker: str, reason: str = CLOSE_USER,
                now: Optional[datetime] = None) -> bool:
    """사용자 종료 등 근거 있는 사건으로 추적을 닫는다."""
    now = now or datetime.now(KST)

    def _do() -> bool:
        track = _store.find_active(ticker)
        if not track:
            return False
        _store.update(track["key"], {
            "status": STATUS_CLOSED,
            "closed_reason": reason,
            "closed_at": now.astimezone(KST).date(),
        })
        logger.info("[%s] 돌파 추적 종료 - %s", ticker, reason)
        return True

    return _guard(f"추적 종료({ticker})", _do, False)


# ── 일별 갱신 ───────────────────────────────────────────────

def compute_daily(track: dict, row: Optional[dict]) -> dict:
    """확정 종가 한 줄로 갱신 필드를 만든다. 저장하지 않는다(순수 계산).

    row는 scan_latest.csv의 한 행(dict)이다. None이면 그 종목이 스캔
    결과에 없다는 뜻이라 '갱신실패'로 남긴다 - 이전 값은 지우지 않는다.

    ATR 배수의 분모는 **최초 ATR**이다. 최초 기준선을 기준으로 지금
    얼마나 움직였는지를 처음 신호와 같은 자로 재기 위해서다. 최신 ATR로
    재면 변동성이 커진 종목일수록 같은 상승폭이 작게 보인다.
    """
    first_threshold = float(track["first_threshold"])
    first_atr = float(track["first_atr"])

    if row is None:
        return {
            "refresh_state": REFRESH_FAIL,
            "refresh_note": "야간 스캔 결과에 이 종목이 없습니다"
                            "(유동성 컷·거래정지·상장폐지 등 - 직전 값 유지)",
            "level_state": LEVEL_UNKNOWN,
            "new_breakout": NEW_BREAKOUT_UNKNOWN,
        }

    close = float(row["close"])
    scan_date = row["scan_date"]
    if isinstance(scan_date, str):
        scan_date = date.fromisoformat(
            f"{scan_date[:4]}-{scan_date[4:6]}-{scan_date[6:8]}"
            if len(scan_date) == 8 else scan_date
        )

    latest_atr = float(row.get("atr20") or 0.0)
    latest_line = row.get("high20_next")
    latest_line = float(latest_line) if latest_line is not None else None

    fields: dict[str, Any] = {
        "latest_close": close,
        "latest_date": scan_date,
        "latest_atr": latest_atr or None,
        "latest_line": latest_line,
        "change_pct": round((close - first_threshold) / first_threshold * 100, 2),
        "atr_mult": round((close - first_threshold) / first_atr, 2)
                    if first_atr > 0 else None,
        "level_state": LEVEL_ABOVE if close > first_threshold else LEVEL_BELOW,
        "refresh_state": REFRESH_OK,
        "refresh_note": "",
    }

    # "새 돌파"는 그날 종가가 그날까지의(당일 제외) 20일 고가를 넘었는가다 -
    # scan_all.py가 이미 status 칸에 같은 기준으로 적어둔 값을 그대로 읽는다.
    status = str(row.get("status") or "")
    if status:
        fields["new_breakout"] = (NEW_BREAKOUT_YES if status == "돌파"
                                  else NEW_BREAKOUT_NO)
    else:
        fields["new_breakout"] = NEW_BREAKOUT_UNKNOWN

    # 최신 돌파선까지 남은 거리. 분모는 **최신 ATR**이다(최초 ATR이 아니다) -
    # "지금 기준으로 얼마나 더 가야 새 돌파인가"라서 지금의 변동성으로 재야
    # 한다. 위 atr_mult와 분모가 다르므로 화면·리포트에서 둘을 함께 쓸 때
    # 항상 분모를 밝힌다.
    if latest_line is not None:
        fields["dist_to_line"] = round(latest_line - close)
        fields["dist_to_line_atr"] = (round((latest_line - close) / latest_atr, 2)
                                      if latest_atr > 0 else None)
    else:
        fields["dist_to_line"] = None
        fields["dist_to_line_atr"] = None

    return fields


def scan_rows_by_ticker(scan_frame) -> dict[str, dict]:
    if scan_frame is None or getattr(scan_frame, "empty", True):
        return {}
    rows: dict[str, dict] = {}
    for record in scan_frame.to_dict("records"):
        ticker = str(record.get("ticker") or "").strip().zfill(6)
        if ticker:
            rows[ticker] = record
    return rows


def refresh_from_scan(scan_frame, now: Optional[datetime] = None) -> dict:
    """야간 스캔 확정 종가로 활성 추적 건을 하루 한 번 갱신한다.

    같은 기준일로 이미 갱신된 건은 다시 쓰지 않는다 - 워크플로가 두 번
    돌거나 수동으로 다시 실행해도 노션에 같은 값을 반복해 쓰지 않는다.

    Returns:
        {"updated": n, "skipped": n, "failed": n, "tracks": n, "error": str|None}
    """
    summary = {"updated": 0, "skipped": 0, "failed": 0, "tracks": 0,
               "error": None}

    if not is_enabled():
        summary["error"] = "추적 DB 미설정 - 갱신을 건너뜁니다"
        logger.info("%s", summary["error"])
        return summary

    rows = scan_rows_by_ticker(scan_frame)
    if not rows:
        # 스캔 결과가 통째로 없다. 개별 종목의 '갱신실패'와는 다른
        # 사건이라(인프라 문제) 기록을 일괄로 고치지 않고 그냥 멈춘다.
        summary["error"] = "야간 스캔 결과가 비어 있어 갱신하지 않았습니다"
        logger.warning("%s", summary["error"])
        return summary

    try:
        tracks = _store.list_tracks(include_closed=False)
    except Exception as e:                  # noqa: BLE001
        summary["error"] = f"추적 목록 조회 실패: {e}"
        logger.warning("돌파 추적 갱신 중단 - %s", summary["error"])
        return summary

    summary["tracks"] = len(tracks)
    for track in tracks:
        ticker = str(track.get("ticker") or "").strip()
        if not ticker:
            continue
        row = rows.get(ticker)
        try:
            fields = compute_daily(track, row)
        except Exception as e:              # noqa: BLE001
            logger.warning("[%s] 갱신 계산 실패: %s", ticker, e)
            summary["failed"] += 1
            continue

        if (row is not None
                and track.get("latest_date") == fields.get("latest_date")
                and track.get("refresh_state") == REFRESH_OK):
            summary["skipped"] += 1
            continue

        try:
            _store.update(track["key"], fields)
        except Exception as e:              # noqa: BLE001
            logger.warning("[%s] 갱신 저장 실패: %s", ticker, e)
            summary["failed"] += 1
            continue

        if fields["refresh_state"] == REFRESH_FAIL:
            summary["failed"] += 1
        else:
            summary["updated"] += 1

    logger.info("돌파 추적 갱신: 활성 %d건 · 갱신 %d · 생략 %d · 실패 %d",
                summary["tracks"], summary["updated"], summary["skipped"],
                summary["failed"])
    return summary


# ── 읽기 (화면·리포트 공용) ─────────────────────────────────

def list_tracks(include_closed: bool = False) -> list[dict]:
    """추적 목록. 실패하면 빈 목록 (화면·리포트를 막지 않는다)."""
    def _do() -> list[dict]:
        tracks = _store.list_tracks(include_closed=include_closed)
        return sort_tracks(tracks)
    return _guard("목록 조회", _do, [])


def sort_tracks(tracks: list[dict]) -> list[dict]:
    """최초 감지가 이른 순. 감지 시각이 없는 건은 뒤로 보낸다."""
    def _key(t: dict):
        at = t.get("first_detected_at")
        if isinstance(at, datetime):
            return (0, at.astimezone(KST).replace(tzinfo=None))
        if isinstance(at, date):
            return (0, datetime.combine(at, datetime.min.time()))
        return (1, datetime.max)
    return sorted(tracks, key=_key)


def outcome_label(track: dict) -> str:
    """자동매수 결과 표시 문구.

    기록이 없으면 '미진입 확정'처럼 보이지 않게 '결과 기록 없음'으로
    쓴다 - 기록이 없다는 건 사유를 모른다는 뜻이지 자금 부족이었다는
    뜻이 아니다.
    """
    outcome = track.get("outcome") or OUTCOME_NONE
    if outcome == OUTCOME_NONE:
        return "결과 기록 없음"
    if outcome == OUTCOME_NOT_ORDERED:
        return "미주문(확정)"
    return outcome


def is_confirmed_no_entry(track: dict) -> bool:
    """'미진입 확정'이라고 말해도 되는가. 주문 상태가 불명확하면 False."""
    return (track.get("outcome") or OUTCOME_NONE) in CONFIRMED_NO_ENTRY


def price_basis_label(track: dict) -> str:
    """가격의 기준 시각. 장중 현재가와 혼동하지 않게 항상 같이 표시한다."""
    latest_date = track.get("latest_date")
    if not latest_date:
        return "가격 미갱신"
    if track.get("refresh_state") == REFRESH_FAIL:
        return f"{latest_date} 종가(갱신 실패 - 직전 값)"
    return f"{latest_date} 종가"


def summarize(tracks: list[dict]) -> dict:
    """아침 리포트용 집계. 판단 문구는 만들지 않는다 - 건수만 센다."""
    active = [t for t in tracks if t.get("status", STATUS_ACTIVE) == STATUS_ACTIVE]
    return {
        "total": len(active),
        "above": sum(1 for t in active if t.get("level_state") == LEVEL_ABOVE),
        "below": sum(1 for t in active if t.get("level_state") == LEVEL_BELOW),
        "new_breakout": sum(1 for t in active
                            if t.get("new_breakout") == NEW_BREAKOUT_YES),
        "refresh_failed": sum(1 for t in active
                              if t.get("refresh_state") == REFRESH_FAIL),
        "unclear": sum(1 for t in active if not is_confirmed_no_entry(t)),
    }
