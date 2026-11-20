"""잔고조회 응답 검증 실패 시 남기는 진단 정보 검증.

배경(2026-11-20 운영 로그): `Auto Trade (KIS 모의투자)`가 2026-11-18부터
매 회차 `잔고 조회 응답이 유효하지 않습니다` 한 줄로 자동매수·자동매도
판정을 통째로 보류했는데, 로그에 KIS가 무엇을 돌려줬는지(rt_cd·msg_cd·
msg1·output 구조)가 없어 원인을 판별할 수 없었다.

여기서 검증하는 건 두 가지뿐이다.
  1) 실패 응답은 예전처럼 예외로 올려 보류된다(빈 잔고로 해석하지 않는다).
  2) 그 예외 메시지와 로그에 원인 판별용 코드·구조가 남되, 계좌·보유
     정보(종목코드·수량·평가금액)는 남지 않는다.
실 네트워크·실 자격증명은 helpers로 차단하고 requests.get만 합성 응답으로
바꾼다.
"""

from __future__ import annotations

import logging
from unittest.mock import Mock

import pytest

import kis_client as k
from tests.helpers import block_external_http, scrub_credential_env


@pytest.fixture
def kis_env(monkeypatch):
    scrub_credential_env(monkeypatch)
    block_external_http(monkeypatch)
    monkeypatch.setenv("KIS_APP_KEY", "dummy-key")
    monkeypatch.setenv("KIS_APP_SECRET", "dummy-secret")
    monkeypatch.setenv("KIS_ACCOUNT", "12345678-01")


def _respond(monkeypatch, payload, *, tr_cont: str = "") -> None:
    response = Mock()
    response.status_code = 200
    response.headers = {"tr_cont": tr_cont}
    response.json.return_value = payload
    response.raise_for_status.return_value = None
    monkeypatch.setattr(k.requests, "get", Mock(return_value=response))


def _raise(kis_env, monkeypatch, payload) -> str:
    _respond(monkeypatch, payload)
    with pytest.raises(RuntimeError, match="잔고 조회 응답이 유효하지 않습니다") as exc:
        k.get_account_balance("dummy")
    return str(exc.value)


def test_error_codes_are_reported(kis_env, monkeypatch, caplog):
    """rt_cd·msg_cd·msg1이 메시지와 로그에 함께 남는다."""
    payload = {
        "rt_cd": "1",
        "msg_cd": "EGW00201",
        "msg1": "초당 거래건수를 초과하였습니다.",
        "output1": [],
    }
    with caplog.at_level(logging.ERROR, logger=k.logger.name):
        message = _raise(kis_env, monkeypatch, payload)

    for expected in ("rt_cd='1'", "msg_cd='EGW00201'",
                     "초당 거래건수를 초과하였습니다.", "output1=list(0건)",
                     "output2=없음"):
        assert expected in message, expected
    assert "msg_cd='EGW00201'" in caplog.text


@pytest.mark.parametrize("payload,expected", [
    # output2 키가 빠진 응답과 빈 리스트는 다르다 - 뭉뚱그리면 또 헤맨다.
    ({"rt_cd": "0", "output1": []}, "output2=없음"),
    ({"rt_cd": "0", "output1": None, "output2": [{}]}, "output1=NoneType"),
    ({"rt_cd": "0", "output1": {}, "output2": [{}]}, "output1=dict"),
    ({"rt_cd": "0", "output1": [], "output2": []}, "output2=list(0건)"),
])
def test_output_shape_is_reported(kis_env, monkeypatch, payload, expected):
    assert expected in _raise(kis_env, monkeypatch, payload)


def test_non_dict_response_still_fails_closed(kis_env, monkeypatch):
    """dict가 아닌 JSON이 와도 AttributeError가 아니라 보류 사유가 남는다."""
    message = _raise(kis_env, monkeypatch, ["예상치 못한 형식"])
    assert "응답 형식=list" in message


def test_account_and_holding_values_are_not_logged(kis_env, monkeypatch):
    """진단 정보에 종목코드·수량·평가금액 같은 계좌 내용은 싣지 않는다."""
    payload = {
        "rt_cd": "1",
        "msg_cd": "EGW00123",
        "msg1": "조회 오류",
        "output1": [{"pdno": "005930", "hldg_qty": "1234",
                     "ord_psbl_qty": "1234"}],
        "output2": [{"tot_evlu_amt": "98765432"}],
    }
    message = _raise(kis_env, monkeypatch, payload)
    for leaked in ("005930", "1234", "98765432"):
        assert leaked not in message
