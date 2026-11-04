"""
kakao.py – 카카오톡 나에게 보내기

[리프레시 토큰 자동 교체]
카카오는 리프레시 토큰 잔여 유효기간이 1개월 미만이면 갱신 응답에 새
refresh_token을 주고, 기존 토큰은 그 즉시 만료시킨다(이후 KOE322). 예전엔
새 토큰을 받아도 버려서 매달 카톡이 끊겼다(2026-10-01, 2026-11-02).

  - 저장소: 노션 KIS 토큰 캐시 DB(NOTION_TOKEN_CACHE_DB_ID)의 별도 행
    레코드=KAKAO_REFRESH_TOKEN (notion_repo.get/save_token_record).
  - 읽기 순서: 노션 저장값 → 없거나 조회 실패면 KAKAO_REFRESH_TOKEN Secret.
  - 갱신 응답에 refresh_token이 있으면 즉시 노션에 저장한다. 저장이 두 번
    실패하면 메일로 알린다 - 카톡은 이미 위험한 상태라 메일로.
  - 동시 실행 경합: 다른 실행이 먼저 교체하면 이쪽은 KOE322를 받는다.
    그때 노션 값을 다시 읽어 1회 재시도한다. 노션 값이 그대로면(경합이
    아니라 저장값 자체가 죽은 것 - 예: 사람이 OAuth를 다시 받아 Secret만
    바꾼 경우) Secret으로 1회 재시도하고, 성공한 토큰을 노션에 다시 쓴다.

토큰 값은 어디에도 출력하지 않는다(public 저장소의 Actions 로그는 공개다).
로그에는 출처(노션/Secret)와 교체 여부만 남긴다. 카카오 오류 응답의
error_description도 남기지 않는다 - 요청 값을 되돌려 주는 경우가 있다.

[발송 실패 감지]
send_kakao_message가 실패하면(토큰 갱신·발송 어느 단계든) 그날 첫 실패에만
메일을 1통 보낸다("카톡 발송 실패 - KOE코드"). 하루 건수·메일 여부는 같은
토큰 캐시 DB의 레코드=KAKAO_SEND_FAILURES 행(토큰값 칸에 JSON)에 남기고,
저녁 감사 메일이 today_failure_summary()로 "오늘 카톡 발송 실패 N건"을
표시한다. 이 기록·메일이 실패해도 원래 예외는 그대로 호출부로 올린다.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import requests

import notion_repo

logger = logging.getLogger(__name__)

TOKEN_URL = "https://kauth.kakao.com/oauth/token"
SEND_URL = "https://kapi.kakao.com/v2/api/talk/memo/default/send"
MAX_TEXT_LEN = 200

KST = ZoneInfo("Asia/Seoul")

REFRESH_TOKEN_RECORD = "KAKAO_REFRESH_TOKEN"
FAILURE_RECORD = "KAKAO_SEND_FAILURES"
KOE_REFRESH_EXPIRED = "KOE322"     # 만료(또는 교체돼 무효가 된) refresh_token

SOURCE_NOTION = "노션"
SOURCE_SECRET = "Secret"


class KakaoTokenError(RuntimeError):
    """토큰 갱신 실패. code는 카카오 error_code(KOE…)이거나 None.

    메시지에 토큰 값이나 카카오 error_description을 넣지 않는다.
    """

    def __init__(self, code: Optional[str], status: Optional[int], error: str = ""):
        self.code = code
        self.status = status
        self.error = error
        label = code or (f"HTTP {status}" if status else "응답 이상")
        super().__init__(f"카카오 토큰 갱신 실패 ({label}{', ' + error if error else ''})")


def _today() -> date:
    return datetime.now(KST).date()


# ── 리프레시 토큰 저장소 ────────────────────────────────────

def _read_notion_refresh_token() -> Optional[str]:
    """노션 저장값. 행이 없으면 None, 조회 실패는 raise."""
    found = notion_repo.get_token_record(REFRESH_TOKEN_RECORD)
    return found[0] if found else None


def _load_refresh_token() -> tuple[str, str]:
    """(토큰, 출처). 노션 저장값 → 없거나 조회 실패면 Secret."""
    try:
        token = _read_notion_refresh_token()
        if token:
            return token, SOURCE_NOTION
        logger.info("노션에 카카오 refresh_token이 없습니다 - Secret을 씁니다")
    except Exception as e:                  # noqa: BLE001 - 노션 장애·미설정
        logger.warning("노션 카카오 refresh_token 조회 실패 - Secret으로 폴백합니다 (%s)",
                       type(e).__name__)
    return os.environ["KAKAO_REFRESH_TOKEN"], SOURCE_SECRET


def _notify_store_failure(reason: str) -> None:
    """새 refresh_token을 노션에 못 남겼을 때 메일 경고. 실패해도 raise하지 않는다."""
    subject = "[stock-monitor] 카카오 refresh_token 저장 실패"
    body = (
        f"{datetime.now(KST):%Y-%m-%d %H:%M} KST\n\n"
        "카카오가 새 refresh_token을 발급했지만 노션(KIS 토큰 캐시 DB, "
        f"레코드={REFRESH_TOKEN_RECORD})에 저장하지 못했습니다 ({reason}).\n"
        "이전 토큰은 이미 만료됐으므로, 이 상태로는 다음 실행부터 카톡 발송이 "
        "KOE322로 실패할 수 있습니다.\n\n"
        "조치: 카카오 OAuth 인증을 다시 받아 새 refresh_token을 노션 해당 행의 "
        "토큰값 칸(또는 GitHub Secret KAKAO_REFRESH_TOKEN)에 넣어 주세요.\n"
        "보안상 토큰 값은 이 메일에 넣지 않았습니다."
    )
    try:
        import mailer                          # 무거운 import는 필요할 때만
        mailer.send_alert_mail(subject, body)
    except Exception as e:                  # noqa: BLE001
        logger.error("refresh_token 저장 실패 경고 메일도 보내지 못했습니다 (%s)",
                     type(e).__name__)


def _store_refresh_token(token: str, expires_in: Optional[int]) -> bool:
    """노션에 저장. 1회 재시도 후에도 실패하면 메일 경고하고 False."""
    expires_at = (datetime.now(KST) + timedelta(seconds=int(expires_in))
                  if expires_in else None)
    last_error = ""
    for attempt in (1, 2):
        try:
            notion_repo.save_token_record(REFRESH_TOKEN_RECORD, token, expires_at)
            return True
        except Exception as e:              # noqa: BLE001
            last_error = type(e).__name__
            logger.warning("카카오 refresh_token 노션 저장 실패 (%d/2): %s",
                           attempt, last_error)
    logger.error("카카오 refresh_token을 노션에 저장하지 못했습니다 - 메일로 알립니다")
    _notify_store_failure(last_error)
    return False


# ── 토큰 갱신 ───────────────────────────────────────────────

def _request_tokens(refresh_token: str) -> dict:
    """refresh_token으로 갱신 요청. 실패는 KakaoTokenError (error_code 포함)."""
    data = {
        "grant_type": "refresh_token",
        "client_id": os.environ["KAKAO_REST_API_KEY"],
        "refresh_token": refresh_token,
    }
    client_secret = os.environ.get("KAKAO_CLIENT_SECRET")
    if client_secret:
        data["client_secret"] = client_secret

    resp = requests.post(TOKEN_URL, data=data, timeout=15)
    if resp.status_code >= 400:
        code = error = None
        try:
            payload = resp.json()
            code = payload.get("error_code")
            error = payload.get("error")
        except ValueError:
            pass
        raise KakaoTokenError(code, resp.status_code, error or "")
    body = resp.json()
    if not body.get("access_token"):
        raise KakaoTokenError(None, resp.status_code, "access_token 없음")
    return body


def _refresh_access_token() -> str:
    """access_token을 받는다. 새 refresh_token이 오면 노션에 저장한다.

    KOE322면 1회만 다른 토큰으로 재시도한다:
      - 노션을 다시 읽어 값이 바뀌었으면(다른 실행이 먼저 교체) 그 값으로
      - 아니면 Secret이 지금 쓴 값과 다를 때 Secret으로
    """
    token, source = _load_refresh_token()
    try:
        body = _request_tokens(token)
    except KakaoTokenError as e:
        if e.code != KOE_REFRESH_EXPIRED:
            raise
        retry = _retry_candidate(token, source)
        if retry is None:
            raise
        token, source = retry
        logger.warning("KOE322 - %s 값으로 1회 재시도합니다", source)
        body = _request_tokens(token)

    new_token = body.get("refresh_token")
    if new_token:
        logger.warning("카카오 refresh_token이 교체됐습니다 - 노션에 저장합니다")
        if _store_refresh_token(new_token, body.get("refresh_token_expires_in")):
            logger.info("새 카카오 refresh_token 노션 저장 완료")
    elif source == SOURCE_SECRET:
        # Secret 값이 통했다 = 노션 값이 없거나 죽어 있다. 다음 실행이 노션을
        # 먼저 읽으므로 살아 있는 값을 노션에 맞춰 둔다(실패해도 Secret이 남음).
        try:
            notion_repo.save_token_record(REFRESH_TOKEN_RECORD, token)
            logger.info("Secret의 카카오 refresh_token을 노션에 반영했습니다")
        except Exception as e:              # noqa: BLE001
            logger.warning("Secret 토큰 노션 반영 실패 (%s) - 다음에도 Secret을 씁니다",
                           type(e).__name__)

    logger.info("카카오 access_token 갱신 완료 (refresh_token 출처: %s)", source)
    return body["access_token"]


def _retry_candidate(used: str, source: str) -> Optional[tuple[str, str]]:
    try:
        fresh = _read_notion_refresh_token()
    except Exception as e:                  # noqa: BLE001
        logger.warning("KOE322 재시도용 노션 재조회 실패 (%s)", type(e).__name__)
        fresh = None
    if fresh and fresh != used:
        return fresh, SOURCE_NOTION
    secret = os.environ.get("KAKAO_REFRESH_TOKEN")
    if source == SOURCE_NOTION and secret and secret != used:
        return secret, SOURCE_SECRET
    return None


# ── 발송 ────────────────────────────────────────────────────

def _send(text: str) -> None:
    access_token = _refresh_access_token()

    # 200자 제한 처리
    if len(text) > MAX_TEXT_LEN:
        text = text[: MAX_TEXT_LEN - 1] + "…"

    template = {
        "object_type": "text",
        "text": text,
        "link": {
            "web_url": "https://www.notion.so",
            "mobile_web_url": "https://www.notion.so",
        },
    }

    resp = requests.post(
        SEND_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        data={"template_object": json.dumps(template)},
        timeout=15,
    )
    resp.raise_for_status()
    logger.info("카카오톡 전송 완료 (%d자)", len(text))


def send_kakao_message(text: str) -> None:
    """카카오톡 나에게 보내기 (기본 텍스트 템플릿).

    - 텍스트는 200자 제한 → 초과 시 잘라서 보냄
    - access_token은 refresh_token으로 매번 갱신
    - 실패하면 그날 첫 실패만 메일로 알리고(_note_send_failure) 예외는 그대로 올린다
    """
    try:
        _send(text)
    except Exception as e:
        _note_send_failure(e)
        raise


# ── 발송 실패 기록 (하루 1회 메일 + 저녁 감사 건수) ──────────────

# 같은 프로세스 안에서 두 번 메일을 보내지 않는다 - 노션 기록이 안 될 때
# (노션도 장애) 한 실행이 여러 통 보내는 것만은 막는다.
_mailed_on: Optional[date] = None


def failure_code(e: Exception) -> str:
    """메일 제목·기록용 짧은 사유. 토큰 값은 들어가지 않는다."""
    if isinstance(e, KakaoTokenError):
        return e.code or (f"HTTP {e.status}" if e.status else "토큰 응답 이상")
    if isinstance(e, requests.HTTPError) and e.response is not None:
        code = None
        try:
            code = e.response.json().get("code")
        except ValueError:
            pass
        status = e.response.status_code
        return f"HTTP {status}" + (f" code {code}" if code is not None else "")
    if isinstance(e, KeyError):
        return f"설정 누락 {e.args[0] if e.args else ''}".strip()
    return type(e).__name__


def _read_failure_state() -> dict:
    """{"date","count","mailed","last_code"}. 행이 없으면 {}. 조회 실패는 raise."""
    found = notion_repo.get_token_record(FAILURE_RECORD)
    if not found:
        return {}
    try:
        state = json.loads(found[0])
    except ValueError:
        return {}
    return state if isinstance(state, dict) else {}


def _send_failure_mail(code: str, now: datetime) -> bool:
    subject = f"[stock-monitor] 카톡 발송 실패 - {code}"
    hint = ""
    if code == KOE_REFRESH_EXPIRED:
        hint = ("\nKOE322 = refresh_token 만료/교체. 노션 KIS 토큰 캐시 DB의 "
                f"레코드={REFRESH_TOKEN_RECORD} 행과 GitHub Secret KAKAO_REFRESH_TOKEN을 "
                "확인하고, 둘 다 죽었으면 카카오 OAuth를 다시 받아 넣어 주세요.\n")
    body = (
        f"{now:%Y-%m-%d %H:%M} KST 오늘 첫 카톡 발송 실패입니다 ({code}).\n"
        f"{hint}\n"
        "오늘 이후 실패는 메일을 더 보내지 않고 저녁 감사 메일에 건수로 표시합니다.\n"
        "카톡으로 가야 할 알림(주문·체결·경고)이 지금 전달되지 않고 있을 수 있습니다."
    )
    import mailer
    return bool(mailer.send_alert_mail(subject, body))


def _note_send_failure(e: Exception) -> None:
    """실패 건수를 남기고 그날 첫 실패면 메일. 무슨 일이 있어도 raise하지 않는다."""
    global _mailed_on
    try:
        now = datetime.now(KST)
        today = now.date()
        code = failure_code(e)
        logger.error("카톡 발송 실패 (%s)", code)

        try:
            state = _read_failure_state()
            readable = True
        except Exception as err:            # noqa: BLE001
            logger.warning("카톡 실패 기록 조회 실패 (%s) - 메일 억제 여부를 알 수 없어 보냅니다",
                           type(err).__name__)
            state, readable = {}, False

        if state.get("date") != today.isoformat():
            state = {"date": today.isoformat(), "count": 0, "mailed": False}
        state["count"] = int(state.get("count", 0)) + 1
        state["last_code"] = code

        if not state.get("mailed") and _mailed_on != today:
            try:
                if _send_failure_mail(code, now):
                    state["mailed"] = True
                    _mailed_on = today
            except Exception as err:        # noqa: BLE001
                logger.error("카톡 실패 알림 메일 발송 실패 (%s)", type(err).__name__)

        if readable:
            try:
                notion_repo.save_token_record(FAILURE_RECORD,
                                              json.dumps(state, ensure_ascii=False))
            except Exception as err:        # noqa: BLE001
                logger.warning("카톡 실패 기록 저장 실패 (%s)", type(err).__name__)
    except Exception:                       # noqa: BLE001 - 기록 때문에 호출부가 죽으면 안 된다
        logger.exception("카톡 실패 기록 처리 중 오류")


def today_failure_summary(day: Optional[date] = None) -> Optional[dict]:
    """{"count": N, "last_code": str|None}. 기록을 읽지 못하면 None."""
    day = day or _today()
    try:
        state = _read_failure_state()
    except Exception as e:                  # noqa: BLE001
        logger.warning("카톡 실패 기록 조회 실패 (%s)", type(e).__name__)
        return None
    if state.get("date") != day.isoformat():
        return {"count": 0, "last_code": None}
    return {"count": int(state.get("count", 0)), "last_code": state.get("last_code")}
