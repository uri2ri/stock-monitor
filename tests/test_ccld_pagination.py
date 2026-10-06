"""체결 조회 연속조회(페이징) 회귀 테스트.

배경: 모의 VTTC0081R은 한 페이지에 15건까지만 준다. 예전 코드는 첫 페이지의
`tr_cont`가 F/M이면 **행을 보기도 전에** 보류해서, 하루 주문이 15건을 넘는
날은 찾는 주문이 1페이지에 있어도 그날 체결 확인이 통째로 막혔다.

여기서는 `get_order_execution`을 실제로 실행하고 `requests.get`만 합성
응답으로 바꾼다 - 페이지 전환·요청 파라미터·누적 대조가 전부 실제 코드다.
네트워크·자격증명은 helpers로 차단한다.

검증 범위: 1페이지 / 2페이지(대상이 2페이지) / 페이지 중간 실패 / 상한 초과.
"""

from __future__ import annotations

import pytest
import requests

import kis_client as k
from helpers import block_external_http, scrub_credential_env

ORDER_NO = "0000006229"


@pytest.fixture
def kis_env(monkeypatch):
    scrub_credential_env(monkeypatch)
    block_external_http(monkeypatch)
    monkeypatch.setenv("KIS_APP_KEY", "dummy-key")
    monkeypatch.setenv("KIS_APP_SECRET", "dummy-secret")
    monkeypatch.setenv("KIS_ACCOUNT", "12345678-01")
    # 페이지 간 대기를 실제로 재우지 않고 호출만 기록한다.
    slept: list[float] = []
    monkeypatch.setattr(k.time, "sleep", lambda s: slept.append(s))
    return slept


def _row(odno: str, *, qty: int = 13, avg: str = "112460") -> dict:
    return {
        "ord_dt": "20260917", "odno": odno, "pdno": "078930",
        "sll_buy_dvsn_cd": "01", "ord_qty": "13",
        "tot_ccld_qty": str(qty), "avg_prvs": avg, "rmn_qty": "0",
    }


class _Page:
    """합성 응답 한 페이지.

    keys_in: 연속조회 키를 본문에 넣을지('body'), 헤더에 넣을지('header'),
    아예 안 줄지(None). 실제 KIS는 본문으로 주지만 헤더 보고도 있어
    양쪽 다 받아들이게 돼 있다 - 그 분기를 여기서 고정한다.
    """

    def __init__(self, rows, *, tr_cont="", rt_cd="0", http_error=None,
                 keys_in="body", nk="NEXTKEY", output1="rows"):
        self.rows, self.tr_cont, self.rt_cd = rows, tr_cont, rt_cd
        self.http_error, self.keys_in, self.nk = http_error, keys_in, nk
        self.output1 = output1

    def build(self):
        page = self
        headers = {"tr_cont": page.tr_cont}
        if page.keys_in == "header" and page.tr_cont in ("F", "M"):
            headers["ctx_area_fk100"] = "FK"
            headers["ctx_area_nk100"] = page.nk

        class _Resp:
            status_code = 200

            def __init__(self):
                self.headers = headers

            def raise_for_status(self):
                if page.http_error:
                    raise page.http_error

            def json(self):
                body = {"rt_cd": page.rt_cd, "msg1": "정상처리 되었습니다."}
                body["output1"] = (page.rows if page.output1 == "rows"
                                   else page.output1)
                if page.keys_in == "body" and page.tr_cont in ("F", "M"):
                    body["ctx_area_fk100"] = "FK"
                    body["ctx_area_nk100"] = page.nk
                return body

        return _Resp()


def _serve(monkeypatch, pages):
    """페이지 목록을 순서대로 돌려주고, 각 요청의 params·headers를 기록한다."""
    calls: list[dict] = []

    def _get(url, **kwargs):
        index = len(calls)
        calls.append({"params": kwargs.get("params", {}),
                      "headers": kwargs.get("headers", {})})
        if index >= len(pages):
            raise AssertionError(f"예상보다 많은 요청: {index + 1}번째")
        return pages[index].build()

    monkeypatch.setattr(requests, "get", _get)
    return calls


# ── 1페이지 (기존 동작 유지) ─────────────────────────────────

def test_single_page_does_not_paginate(kis_env, monkeypatch):
    """마지막 페이지 표시(E)면 한 번만 부르고 끝낸다."""
    calls = _serve(monkeypatch, [_Page([_row(ORDER_NO)], tr_cont="E")])

    fill = k.get_order_execution("tok", ORDER_NO)

    assert fill == {"filled_qty": 13, "avg_price": 112460.0}
    assert len(calls) == 1
    # 첫 요청은 연속조회 키를 비워 보내고 tr_cont 헤더를 달지 않는다.
    assert calls[0]["params"]["CTX_AREA_FK100"] == ""
    assert calls[0]["params"]["CTX_AREA_NK100"] == ""
    assert "tr_cont" not in calls[0]["headers"]
    # 1페이지면 페이지 간 대기가 아예 없다 (체결 처리 대기 1초는
    # _record_holding_after_buy 쪽이라 이 함수에는 없다).
    assert kis_env == []


def test_blank_tr_cont_is_last_page(kis_env, monkeypatch):
    calls = _serve(monkeypatch, [_Page([_row(ORDER_NO)], tr_cont="")])
    assert k.get_order_execution("tok", ORDER_NO) is not None
    assert len(calls) == 1


# ── 2페이지: 찾는 주문이 2페이지에 있다 ──────────────────────

@pytest.mark.parametrize("keys_in", ["body", "header"])
@pytest.mark.parametrize("first_tr_cont", ["F", "M"])
def test_target_on_second_page_is_found(kis_env, monkeypatch,
                                        keys_in, first_tr_cont):
    """예전 코드가 보류했던 바로 그 상황 - 이제는 끝까지 모아 찾는다."""
    page1 = _Page([_row("0000000001"), _row("0000000002")],
                  tr_cont=first_tr_cont, keys_in=keys_in, nk="PAGE2")
    page2 = _Page([_row(ORDER_NO, qty=13, avg="112460")], tr_cont="E")
    calls = _serve(monkeypatch, [page1, page2])

    fill = k.get_order_execution("tok", ORDER_NO)

    assert fill == {"filled_qty": 13, "avg_price": 112460.0}
    assert len(calls) == 2
    # 2페이지 요청에 받은 키와 tr_cont='N'이 실려야 한다.
    assert calls[1]["params"]["CTX_AREA_NK100"] == "PAGE2"
    assert calls[1]["params"]["CTX_AREA_FK100"] == "FK"
    assert calls[1]["headers"]["tr_cont"] == "N"
    # 페이지 사이에 정확히 한 번, 지정된 간격만큼 쉰다.
    assert kis_env == [k.CCLD_PAGE_INTERVAL_SECONDS]


def test_rows_from_all_pages_are_accumulated(kis_env, monkeypatch):
    """1페이지에 있는 주문도 2페이지를 받은 뒤 정상 대조된다."""
    page1 = _Page([_row(ORDER_NO), _row("0000000002")], tr_cont="F")
    page2 = _Page([_row("0000000003")], tr_cont="E")
    calls = _serve(monkeypatch, [page1, page2])

    assert k.get_order_execution("tok", ORDER_NO) is not None
    assert len(calls) == 2               # 끝까지 다 받고 판정한다


def test_duplicate_across_pages_still_holds(kis_env, monkeypatch):
    """같은 주문번호가 페이지를 걸쳐 두 번 나오면 넘겨짚지 않고 보류."""
    page1 = _Page([_row(ORDER_NO)], tr_cont="F")
    page2 = _Page([_row(ORDER_NO)], tr_cont="E")
    _serve(monkeypatch, [page1, page2])

    with pytest.raises(RuntimeError, match="여러 건"):
        k.get_order_execution("tok", ORDER_NO)


def test_not_found_after_all_pages_returns_none(kis_env, monkeypatch):
    page1 = _Page([_row("0000000001")], tr_cont="F")
    page2 = _Page([_row("0000000002")], tr_cont="E")
    _serve(monkeypatch, [page1, page2])

    assert k.get_order_execution("tok", ORDER_NO) is None


# ── 페이지 중간 실패 → 보류 (앞 페이지로 판정하지 않는다) ────

def test_mid_page_kis_error_holds(kis_env, monkeypatch):
    """2페이지가 rt_cd 오류면, 1페이지에 대상이 있어도 보류한다."""
    page1 = _Page([_row(ORDER_NO)], tr_cont="F")
    page2 = _Page([], rt_cd="1")
    _serve(monkeypatch, [page1, page2])

    with pytest.raises(RuntimeError, match="2페이지"):
        k.get_order_execution("tok", ORDER_NO)


def test_mid_page_http_error_holds(kis_env, monkeypatch):
    page1 = _Page([_row(ORDER_NO)], tr_cont="F")
    page2 = _Page([], http_error=requests.HTTPError("500 Server Error"))
    _serve(monkeypatch, [page1, page2])

    with pytest.raises(requests.HTTPError):
        k.get_order_execution("tok", ORDER_NO)


def test_mid_page_invalid_output1_holds(kis_env, monkeypatch):
    page1 = _Page([_row(ORDER_NO)], tr_cont="F")
    page2 = _Page([], output1=None)
    _serve(monkeypatch, [page1, page2])

    with pytest.raises(RuntimeError, match="유효하지 않습니다"):
        k.get_order_execution("tok", ORDER_NO)


def test_missing_continuation_key_holds(kis_env, monkeypatch):
    """F인데 키를 안 주면 다음 페이지를 요청할 수 없다 - 보류."""
    page1 = _Page([_row(ORDER_NO)], tr_cont="F", keys_in=None)
    calls = _serve(monkeypatch, [page1])

    with pytest.raises(RuntimeError, match="연속조회 키 없음"):
        k.get_order_execution("tok", ORDER_NO)
    assert len(calls) == 1


# ── 상한 초과 → 보류 ────────────────────────────────────────

def test_page_cap_exceeded_holds(kis_env, monkeypatch):
    """끝까지 F만 오면 상한에서 멈추고 보류한다 - 무한 호출 금지."""
    pages = [_Page([_row(f"{i:010d}")], tr_cont="F")
             for i in range(k.CCLD_MAX_PAGES)]
    calls = _serve(monkeypatch, pages)

    with pytest.raises(RuntimeError, match="상한.*초과"):
        k.get_order_execution("tok", ORDER_NO)

    # 상한 페이지까지만 부르고 그 이상은 부르지 않는다.
    assert len(calls) == k.CCLD_MAX_PAGES


def test_exactly_at_cap_last_page_succeeds(kis_env, monkeypatch):
    """상한 번째 페이지가 마지막(E)이면 정상 처리된다 - off-by-one 방지."""
    pages = [_Page([_row(f"{i:010d}")], tr_cont="F")
             for i in range(k.CCLD_MAX_PAGES - 1)]
    pages.append(_Page([_row(ORDER_NO)], tr_cont="E"))
    calls = _serve(monkeypatch, pages)

    assert k.get_order_execution("tok", ORDER_NO) is not None
    assert len(calls) == k.CCLD_MAX_PAGES
