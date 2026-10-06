"""체결 조회가 실패하면 진입가가 추정치임을 알리고 기록에도 남긴다.

배경: `_record_holding_after_buy`는 체결 조회 예외를 `logger.warning`으로만
삼키고 돌파 시점 현재가를 진입가로 기록했다. 손절선·R배수·손익이 전부 이
값에서 나오는데 카톡도, 기록상 표시도 없었다 - 하루 주문이 15건을 넘어
체결 조회가 보류될 때 조용히 추정가가 박히는 경로였다.

이제는 (1) `_notify_warning_throttled`로 알리고, (2) 메모·매수이유에
"추정 진입가(체결 미확인)"를 남기고, (3) **기록 자체는 계속 진행**한다.
(3)이 중요하다 - 여기서 멈추면 그 종목이 자동매도 대상에서 빠져
"사기만 하고 못 파는" 상태가 된다.
"""

from __future__ import annotations

import pytest

import kis_client as k
import notion_repo as n
from helpers import block_external_http, scrub_credential_env

CANDIDATE = {
    "ticker": "005930", "name": "삼성전자", "market": "KOSPI",
    "price": 70000, "unit_shares": 10, "atr20": 1000.0,
    "sector": "전기전자", "high20": 69500, "gap_atr": 0.5,
}


@pytest.fixture
def buy_env(monkeypatch):
    """노션 쓰기를 가로채고 카톡·HTTP를 막는다."""
    scrub_credential_env(monkeypatch)
    block_external_http(monkeypatch)
    monkeypatch.setenv("KIS_APP_KEY", "dummy-key")
    monkeypatch.setenv("KIS_APP_SECRET", "dummy-secret")
    monkeypatch.setenv("KIS_ACCOUNT", "12345678-01")
    monkeypatch.setattr(k.time, "sleep", lambda s: None)

    created: list[dict] = []
    warnings: list[tuple[str, str]] = []
    monkeypatch.setattr(n, "find_auto_holding_page", lambda ticker: None)
    monkeypatch.setattr(n, "create_auto_holding",
                        lambda **kw: created.append(kw) or "page-1")
    monkeypatch.setattr(k, "_notify_warning_throttled",
                        lambda key, msg: warnings.append((key, msg)))
    # 실패 알림 경로가 돌면 테스트 의도와 다르다 - 섞이지 않게 분리해 잡는다.
    failures: list[str] = []
    monkeypatch.setattr(k, "_notify_failure", lambda msg: failures.append(msg))
    return {"created": created, "warnings": warnings, "failures": failures}


def _fill_raises(monkeypatch, exc):
    def _boom(*a, **kw):
        raise exc
    monkeypatch.setattr(k, "get_order_execution", _boom)


def test_pagination_hold_marks_estimate_and_still_records(buy_env, monkeypatch):
    """상한 초과 보류(연속조회)가 바로 이 경로로 들어온다."""
    _fill_raises(monkeypatch, RuntimeError(
        "체결 조회 연속조회 상한(10페이지) 초과 - 전체 내역 확인 전 처리 보류"))

    k._record_holding_after_buy("tok", CANDIDATE, "0000006229")

    # (3) 기록은 계속 진행된다.
    assert len(buy_env["created"]) == 1
    row = buy_env["created"][0]
    assert row["buy_price"] == 70000          # 돌파 시점 현재가로 폴백
    assert row["shares"] == 10
    # (2) 메모·매수이유에 추정 표기가 남는다.
    assert "추정 진입가(체결 미확인)" in row["memo"]
    assert "추정 진입가" in row["buy_reason"]
    # (1) 카톡 경고가 나간다 - 종목별 키라 다른 종목을 억제하지 않는다.
    assert len(buy_env["warnings"]) == 1
    key, msg = buy_env["warnings"][0]
    assert key == "buy_fill_unconfirmed:005930"
    assert "삼성전자" in msg and "체결 미확인" in msg
    assert "연속조회 상한" in msg              # 원인을 그대로 전달한다
    assert buy_env["failures"] == []


def test_successful_fill_has_no_estimate_marking(buy_env, monkeypatch):
    """정상 경로는 그대로다 - 경고도, 추정 표기도 없다."""
    monkeypatch.setattr(k, "get_order_execution",
                        lambda *a, **kw: {"filled_qty": 10,
                                          "avg_price": 70350.0})

    k._record_holding_after_buy("tok", CANDIDATE, "0000006229")

    row = buy_env["created"][0]
    assert row["buy_price"] == 70350.0        # 실제 체결가를 쓴다
    assert "추정" not in row["memo"]
    assert "추정" not in row["buy_reason"]
    assert buy_env["warnings"] == []


def test_unfilled_without_exception_keeps_existing_behavior(buy_env, monkeypatch):
    """아직 미체결(None)은 예외가 아니다 - 이번 변경 범위 밖(기존 동작 유지).

    이 경로도 진입가가 추정가가 되지만 표기·경고를 붙이지 않았다.
    범위를 넘겨 바꾸지 않았다는 사실을 테스트로 고정해 둔다.
    """
    monkeypatch.setattr(k, "get_order_execution", lambda *a, **kw: None)

    k._record_holding_after_buy("tok", CANDIDATE, "0000006229")

    row = buy_env["created"][0]
    assert row["buy_price"] == 70000
    assert "추정" not in row["memo"]
    assert buy_env["warnings"] == []


def test_notion_failure_still_alerts_separately(buy_env, monkeypatch):
    """노션 편입 실패는 기존 실패 알림 경로를 그대로 탄다."""
    _fill_raises(monkeypatch, RuntimeError("보류"))
    monkeypatch.setattr(n, "create_auto_holding",
                        lambda **kw: (_ for _ in ()).throw(RuntimeError("노션 다운")))

    k._record_holding_after_buy("tok", CANDIDATE, "0000006229")

    assert len(buy_env["warnings"]) == 1       # 추정가 경고
    assert len(buy_env["failures"]) == 1       # 편입 실패 경고
    assert "자동매도 대상에서 빠집니다" in buy_env["failures"][0]
