"""카카오 refresh_token 자동 교체 대응.

카카오는 refresh_token 잔여 유효기간이 1개월 미만이면 갱신 응답에 새
refresh_token을 주고 기존 토큰을 즉시 만료시킨다(KOE322). 예전 kakao.py는
새 토큰을 버려서 매달 카톡이 끊겼다(2026-10-01, 2026-11-02). 확인하는 것:

  - 새 토큰 수신 시 노션 저장 (토큰 값은 로그에 안 남음)
  - 노션 저장값 우선, 노션 장애·미설정이면 Secret 폴백
  - KOE322면 노션 재조회 후 1회 재시도 (경합), 노션 값이 그대로면 Secret으로
  - 저장 실패 시 메일 경고
  - 발송 실패 메일은 하루 1회, 건수는 저녁 감사 메일에 표시
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs

import pytest
import requests

import kakao
import mailer
import notion_repo
from helpers import block_external_http, scrub_credential_env


class _Resp:
    def __init__(self, status: int, payload: dict):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Error", response=self)


class FakeKakao:
    """토큰 엔드포인트: valid에 있는 refresh_token만 통과, 나머지는 KOE322."""

    def __init__(self):
        self.valid: dict[str, dict] = {}       # refresh_token -> 추가 응답 필드
        self.token_calls: list[str] = []
        self.sent: list[str] = []
        self.send_status = 200
        self.on_token_call = None                # 경합 재현용 훅

    def post(self, url, data=None, headers=None, timeout=None, **kw):
        if url == kakao.TOKEN_URL:
            token = data["refresh_token"]
            self.token_calls.append(token)
            if self.on_token_call:
                self.on_token_call(token)
            if token not in self.valid:
                return _Resp(401, {"error": "invalid_grant",
                                   "error_description": f"bad token {token}",
                                   "error_code": "KOE322"})
            return _Resp(200, {"access_token": "AT-" + token, **self.valid[token]})
        if url == kakao.SEND_URL:
            if self.send_status >= 400:
                return _Resp(self.send_status, {"msg": "fail", "code": -401})
            self.sent.append(json.loads(data["template_object"])["text"])
            return _Resp(200, {"result_code": 0})
        raise AssertionError(f"예상 밖 요청: {url}")


class FakeStore:
    def __init__(self):
        self.rows: dict[str, tuple[str, object]] = {}
        self.fail_get = False
        self.fail_save = False
        self.saves: list[tuple[str, str]] = []

    def get(self, record):
        if self.fail_get:
            raise requests.ConnectionError("notion down")
        return self.rows.get(record)

    def save(self, record, token, expires_at=None):
        if self.fail_save:
            raise requests.ConnectionError("notion down")
        self.rows[record] = (token, expires_at)
        self.saves.append((record, token))


@pytest.fixture
def env(monkeypatch):
    scrub_credential_env(monkeypatch)
    block_external_http(monkeypatch)
    monkeypatch.setenv("KAKAO_REST_API_KEY", "rest-key")
    monkeypatch.setenv("KAKAO_REFRESH_TOKEN", "SECRET-RT")
    fake = FakeKakao()
    monkeypatch.setattr(kakao.requests, "post", fake.post)
    store = FakeStore()
    monkeypatch.setattr(notion_repo, "get_token_record", store.get)
    monkeypatch.setattr(notion_repo, "save_token_record", store.save)
    mails: list[tuple[str, str]] = []
    monkeypatch.setattr(mailer, "send_alert_mail",
                        lambda subject, body: mails.append((subject, body)) or True)
    monkeypatch.setattr(kakao, "_mailed_on", None)
    return SimpleNamespace(fake=fake, store=store, mails=mails)


def _token_in_logs(caplog, *tokens) -> bool:
    text = "\n".join(r.getMessage() for r in caplog.records)
    return any(t in text for t in tokens)


# ── 새 토큰 수신 → 저장 ────────────────────────────────────

def test_rotated_refresh_token_is_saved_to_notion(env, caplog):
    caplog.set_level(logging.DEBUG)
    env.store.rows[kakao.REFRESH_TOKEN_RECORD] = ("OLD-RT", None)
    env.fake.valid["OLD-RT"] = {"refresh_token": "NEW-RT",
                                "refresh_token_expires_in": 5_184_000}

    kakao.send_kakao_message("hi")

    assert env.fake.sent == ["hi"]
    token, expires_at = env.store.rows[kakao.REFRESH_TOKEN_RECORD]
    assert token == "NEW-RT"
    assert expires_at is not None
    assert expires_at - datetime.now(kakao.KST) > timedelta(days=59)
    assert env.mails == []
    assert not _token_in_logs(caplog, "OLD-RT", "NEW-RT", "SECRET-RT", "AT-OLD-RT")


def test_no_rotation_leaves_store_untouched(env):
    env.store.rows[kakao.REFRESH_TOKEN_RECORD] = ("RT", None)
    env.fake.valid["RT"] = {}

    kakao.send_kakao_message("hi")

    assert env.store.saves == []


# ── 읽기 순서 ───────────────────────────────────────────────

def test_notion_value_is_preferred_over_secret(env):
    env.store.rows[kakao.REFRESH_TOKEN_RECORD] = ("NOTION-RT", None)
    env.fake.valid["NOTION-RT"] = {}
    env.fake.valid["SECRET-RT"] = {}

    kakao.send_kakao_message("hi")

    assert env.fake.token_calls == ["NOTION-RT"]


def test_notion_outage_falls_back_to_secret(env):
    env.store.fail_get = True
    env.fake.valid["SECRET-RT"] = {}

    kakao.send_kakao_message("hi")

    assert env.fake.token_calls == ["SECRET-RT"]
    assert env.fake.sent == ["hi"]


def test_missing_cache_db_env_falls_back_to_secret(env, monkeypatch):
    # 실제 notion_repo로 되돌려 NOTION_TOKEN_CACHE_DB_ID 미설정(KeyError)을 재현
    monkeypatch.undo()
    scrub_credential_env(monkeypatch)
    block_external_http(monkeypatch)
    monkeypatch.delenv("NOTION_TOKEN_CACHE_DB_ID", raising=False)
    monkeypatch.setenv("KAKAO_REST_API_KEY", "rest-key")
    monkeypatch.setenv("KAKAO_REFRESH_TOKEN", "SECRET-RT")
    fake = FakeKakao()
    fake.valid["SECRET-RT"] = {}
    monkeypatch.setattr(kakao.requests, "post", fake.post)

    kakao.send_kakao_message("hi")

    assert fake.token_calls == ["SECRET-RT"]


def test_empty_notion_uses_secret_and_backfills_notion(env):
    env.fake.valid["SECRET-RT"] = {}

    kakao.send_kakao_message("hi")

    assert env.fake.token_calls == ["SECRET-RT"]
    assert env.store.rows[kakao.REFRESH_TOKEN_RECORD][0] == "SECRET-RT"


# ── KOE322 재시도 ───────────────────────────────────────────

def test_koe322_rereads_notion_once_when_another_run_rotated(env):
    """경합: 이쪽이 옛 토큰을 읽은 뒤 다른 실행이 교체해 노션에 저장."""
    env.store.rows[kakao.REFRESH_TOKEN_RECORD] = ("OLD-RT", None)
    env.fake.valid["NEW-RT"] = {}

    def other_run_rotates(token):
        if token == "OLD-RT":
            env.store.rows[kakao.REFRESH_TOKEN_RECORD] = ("NEW-RT", None)
    env.fake.on_token_call = other_run_rotates

    kakao.send_kakao_message("hi")

    assert env.fake.token_calls == ["OLD-RT", "NEW-RT"]
    assert env.fake.sent == ["hi"]
    assert env.mails == []


def test_koe322_with_dead_notion_value_retries_secret_and_repairs_notion(env):
    """사람이 OAuth를 다시 받아 Secret만 바꾼 경우 - 노션 값이 계속 죽어 있다."""
    env.store.rows[kakao.REFRESH_TOKEN_RECORD] = ("DEAD-RT", None)
    env.fake.valid["SECRET-RT"] = {}

    kakao.send_kakao_message("hi")

    assert env.fake.token_calls == ["DEAD-RT", "SECRET-RT"]
    assert env.store.rows[kakao.REFRESH_TOKEN_RECORD][0] == "SECRET-RT"


def test_koe322_retries_only_once(env):
    env.store.rows[kakao.REFRESH_TOKEN_RECORD] = ("DEAD-RT", None)    # Secret도 죽음

    with pytest.raises(kakao.KakaoTokenError) as exc:
        kakao.send_kakao_message("hi")

    assert exc.value.code == "KOE322"
    assert env.fake.token_calls == ["DEAD-RT", "SECRET-RT"]
    assert env.fake.sent == []


def test_koe322_from_secret_with_nothing_else_does_not_retry(env):
    env.store.fail_get = True

    with pytest.raises(kakao.KakaoTokenError):
        kakao.send_kakao_message("hi")

    assert env.fake.token_calls == ["SECRET-RT"]


def test_error_message_has_no_token_or_description(env):
    env.store.rows[kakao.REFRESH_TOKEN_RECORD] = ("DEAD-RT", None)
    with pytest.raises(kakao.KakaoTokenError) as exc:
        kakao.send_kakao_message("hi")
    msg = str(exc.value)
    assert "KOE322" in msg
    assert "DEAD-RT" not in msg and "SECRET-RT" not in msg and "bad token" not in msg


# ── 저장 실패 → 메일 ────────────────────────────────────────

def test_save_failure_sends_warning_mail_but_message_still_goes(env, caplog):
    caplog.set_level(logging.DEBUG)
    env.store.rows[kakao.REFRESH_TOKEN_RECORD] = ("OLD-RT", None)
    env.fake.valid["OLD-RT"] = {"refresh_token": "NEW-RT"}
    env.store.fail_save = True

    kakao.send_kakao_message("hi")

    assert env.fake.sent == ["hi"]                  # 이번 발송은 그대로 성공
    assert len(env.mails) == 1
    subject, body = env.mails[0]
    assert "refresh_token 저장 실패" in subject
    assert "NEW-RT" not in body and "OLD-RT" not in body
    assert not _token_in_logs(caplog, "NEW-RT", "OLD-RT")


# ── 발송 실패 메일: 하루 1회 ─────────────────────────────────

def _fail_state(env):
    return json.loads(env.store.rows[kakao.FAILURE_RECORD][0])


def test_failure_mail_once_per_day_and_counted(env, monkeypatch):
    env.store.rows[kakao.REFRESH_TOKEN_RECORD] = ("DEAD-RT", None)
    monkeypatch.delenv("KAKAO_REFRESH_TOKEN")      # 재시도 후보 없음

    for _ in range(3):
        with pytest.raises(Exception):
            kakao.send_kakao_message("hi")

    failure_mails = [s for s, _ in env.mails if "카톡 발송 실패" in s]
    assert failure_mails == ["[stock-monitor] 카톡 발송 실패 - KOE322"]
    state = _fail_state(env)
    assert state["count"] == 3 and state["mailed"] is True
    assert state["last_code"] == "KOE322"
    assert state["date"] == kakao._today().isoformat()


def test_failure_mail_suppressed_across_runs_via_notion(env, monkeypatch):
    """다른 실행(새 프로세스)이어도 노션 기록으로 하루 1회가 유지된다."""
    env.store.rows[kakao.FAILURE_RECORD] = (json.dumps({
        "date": kakao._today().isoformat(), "count": 2, "mailed": True,
        "last_code": "KOE322"}), None)
    env.fake.send_status = 401
    env.store.rows[kakao.REFRESH_TOKEN_RECORD] = ("RT", None)
    env.fake.valid["RT"] = {}

    with pytest.raises(requests.HTTPError):
        kakao.send_kakao_message("hi")

    assert env.mails == []
    state = _fail_state(env)
    assert state["count"] == 3 and state["last_code"] == "HTTP 401 code -401"


def test_new_day_resets_count_and_mails_again(env):
    env.store.rows[kakao.FAILURE_RECORD] = (json.dumps({
        "date": (kakao._today() - timedelta(days=1)).isoformat(), "count": 9,
        "mailed": True}), None)
    env.store.rows[kakao.REFRESH_TOKEN_RECORD] = ("DEAD-RT", None)

    with pytest.raises(kakao.KakaoTokenError):
        kakao.send_kakao_message("hi")

    assert len([s for s, _ in env.mails if "카톡 발송 실패" in s]) == 1
    assert _fail_state(env)["count"] == 1


def test_unreadable_failure_record_mails_but_once_per_process(env, monkeypatch):
    """노션도 장애면 억제 여부를 모르니 보내되(fail-open), 한 실행에서 한 통만."""
    env.store.fail_get = True
    env.store.fail_save = True                       # Secret도 KOE322

    for _ in range(3):
        with pytest.raises(kakao.KakaoTokenError):
            kakao.send_kakao_message("hi")

    assert len([s for s, _ in env.mails if "카톡 발송 실패" in s]) == 1


def test_failure_bookkeeping_errors_never_mask_original(env, monkeypatch):
    env.store.fail_get = True
    monkeypatch.setattr(mailer, "send_alert_mail",
                        lambda *a: (_ for _ in ()).throw(RuntimeError("smtp down")))

    with pytest.raises(kakao.KakaoTokenError):
        kakao.send_kakao_message("hi")


# ── 저녁 감사 표시 ──────────────────────────────────────────

def test_today_failure_summary(env):
    assert kakao.today_failure_summary() == {"count": 0, "last_code": None}
    env.store.rows[kakao.FAILURE_RECORD] = (json.dumps({
        "date": kakao._today().isoformat(), "count": 4, "mailed": True,
        "last_code": "KOE322"}), None)
    assert kakao.today_failure_summary() == {"count": 4, "last_code": "KOE322"}
    env.store.fail_get = True
    assert kakao.today_failure_summary() is None


def _evening_report(kakao_failures):
    return SimpleNamespace(
        today=date(2026, 11, 4), dry_run=False, buys=[], exits=[], executed=[],
        unexecuted=[], random_trades=[], ledger_signal_writes=[],
        holding_signal_writes=[], level_changes=[], kakao_failures=kakao_failures)


@pytest.mark.parametrize("summary,expected", [
    ({"count": 0, "last_code": None}, "오늘 카톡 발송 실패 0건"),
    ({"count": 3, "last_code": "KOE322"}, "오늘 카톡 발송 실패 3건 (마지막 사유 KOE322)"),
    (None, "오늘 카톡 발송 실패: 확인 불가"),
])
def test_evening_mail_shows_kakao_failure_count(summary, expected):
    report = _evening_report(summary)
    assert expected in mailer.build_evening_text(report)
    assert expected in mailer.build_evening_html(report)
