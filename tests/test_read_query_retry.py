"""현재가·잔고 조회의 제한된 재시도 검증.

배경(2026-09-15 운영 로그, 한국시간): 09:23·09:33·09:43 현재가 조회가
HTTP 500으로, 13:23·15:03 잔고조회가 10초 시간초과로 실패해 그 회차의
자동매도 판정(13:23은 추가매수까지)이 생략됐다. 워크플로 자체는 성공으로
끝나 겉으로는 드러나지 않았다.

여기서 검증하는 건 "kis_client가 그런 일시적 실패에 한해 같은 회차 안에서
다시 물어보고, 끝내 실패하면 기존 보류 경로로 넘긴다"는 코드 동작뿐이다.
실 네트워크·.env·대기는 전부 차단·mock하므로 이 통과가 실제 KIS 장애에서의
복구를 확인해 주지는 않는다.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import Mock

import pytest
import requests

import core
import kis_client as k
import notion_repo as n


# ── 공용 픽스처 ─────────────────────────────────────────────

@pytest.fixture(autouse=True)
def no_network_no_env(monkeypatch):
    """실 네트워크·.env·재시도 대기를 차단한다.

    kis_client는 import 시 load_dotenv()를 한 번 부르지만, 자격증명은
    함수 호출 시점에 os.environ에서 읽는다 - 여기서 테스트용 값으로
    덮어써서 실제 .env 값이 쓰일 여지를 없앤다.
    """
    for name in ("KIS_APP_KEY", "KIS_APP_SECRET"):
        monkeypatch.setenv(name, "test-" + name.lower())
    monkeypatch.setenv("KIS_ACCOUNT", "12345678-01")
    # 대기까지 실제로 자면 테스트가 느려지기만 한다 - 호출만 기록한다.
    sleeps: list[float] = []
    monkeypatch.setattr(k.time, "sleep", sleeps.append)
    # 예산은 프로세스 전역이라 테스트 간 누수를 막는다.
    k._reset_read_retry_budget()
    monkeypatch.setattr(k.requests, "get", Mock(side_effect=AssertionError(
        "테스트가 requests.get을 직접 mock하지 않았습니다")))
    yield sleeps
    k._reset_read_retry_budget()


def _resp(payload: dict, *, status: int = 200, tr_cont: str = "") -> Mock:
    r = Mock()
    r.status_code = status
    r.headers = {"tr_cont": tr_cont}
    r.json.return_value = payload
    r.raise_for_status.return_value = None
    return r


def _http_error(status: int) -> requests.HTTPError:
    resp = Mock()
    resp.status_code = status
    return requests.HTTPError(f"{status} Server Error", response=resp)


def _raising_resp(exc: Exception) -> Mock:
    r = Mock()
    r.headers = {}
    r.raise_for_status.side_effect = exc
    return r


PRICE_OK = {"output": {"stck_prpr": "10500", "mrkt_warn_cls_code": "00"}}

BALANCE_OK = {
    "rt_cd": "0",
    "output1": [{"pdno": "000001", "hldg_qty": "100", "ord_psbl_qty": "100"}],
    "output2": [{"tot_evlu_amt": "10000000", "prvs_rcdl_excc_amt": "3000000",
                 "dnca_tot_amt": "3000000"}],
}


def _sequence(monkeypatch, items: list):
    """requests.get이 items를 순서대로 돌려주거나(Mock 응답) 던지게(예외) 한다."""
    calls = {"n": 0}

    def fake_get(*a, **kw):
        i = calls["n"]
        calls["n"] += 1
        item = items[i] if i < len(items) else items[-1]
        if isinstance(item, Exception):
            raise item
        return item

    mock = Mock(side_effect=fake_get)
    monkeypatch.setattr(k.requests, "get", mock)
    return mock


# ── 1. 최초 성공이면 재시도가 붙지 않는다 ────────────────────

def test_price_first_try_success_calls_once(monkeypatch, no_network_no_env):
    get = _sequence(monkeypatch, [_resp(PRICE_OK)])
    assert k.get_current_price("tok", "000001") == "10500"
    assert get.call_count == 1
    assert no_network_no_env == []          # 대기 없음


def test_balance_first_try_success_calls_once(monkeypatch, no_network_no_env):
    get = _sequence(monkeypatch, [_resp(BALANCE_OK)])
    assert k.get_account_balance("tok")["account_size"] == 10_000_000
    assert get.call_count == 1
    assert no_network_no_env == []


# ── 2. 일시적 실패 뒤 성공하면 정상 응답을 쓴다 ───────────────

@pytest.mark.parametrize("failure", [
    _http_error(500),                       # 2026-09-15 09:23 현재가 조회
    _http_error(502),
    _http_error(503),
    _http_error(504),
    requests.ConnectionError("connection reset"),
    requests.Timeout("read timeout"),       # 2026-09-15 13:23 잔고조회
])
def test_price_recovers_within_limit(monkeypatch, no_network_no_env, failure):
    get = _sequence(monkeypatch, [_raising_resp(failure), _resp(PRICE_OK)])
    assert k.get_price_quote("tok", "000001")["price"] == 10500.0
    assert get.call_count == 2
    assert no_network_no_env == [1.0]       # 첫 재시도 대기만


def test_balance_recovers_on_third_attempt(monkeypatch, no_network_no_env):
    get = _sequence(monkeypatch, [
        _raising_resp(requests.Timeout("read timeout")),
        _raising_resp(_http_error(500)),
        _resp(BALANCE_OK),
    ])
    assert k.get_account_balance("tok")["available_cash"] == 3_000_000
    assert get.call_count == 3
    assert no_network_no_env == [1.0, 2.0]  # 점증 대기


# ── 3. 계속 실패하면 최대 횟수에서 멈추고 예외를 올린다 ───────

def test_price_stops_at_max_attempts(monkeypatch, no_network_no_env):
    get = _sequence(monkeypatch, [_raising_resp(_http_error(500))])
    with pytest.raises(requests.HTTPError):
        k.get_current_price("tok", "000001")
    assert get.call_count == 3              # 최초 호출 포함 최대 3회
    assert no_network_no_env == [1.0, 2.0]


def test_balance_stops_at_max_attempts(monkeypatch, no_network_no_env):
    get = _sequence(monkeypatch, [_raising_resp(requests.Timeout("t"))])
    with pytest.raises(requests.Timeout):
        k.get_account_balance("tok")
    assert get.call_count == 3


# ── 4. 재시도 대상이 아닌 오류·무효 응답은 즉시 실패 ──────────

@pytest.mark.parametrize("failure", [
    _http_error(401),                       # 인증
    _http_error(403),                       # 권한
    _http_error(400),                       # 잘못된 요청
    _http_error(429),                       # 유량 초과 - 다시 걸면 제한만 키운다
    _http_error(501),                       # 지원하지 않는 요청
])
def test_non_retryable_http_fails_immediately(monkeypatch, no_network_no_env,
                                              failure):
    get = _sequence(monkeypatch, [_raising_resp(failure)])
    with pytest.raises(requests.HTTPError):
        k.get_current_price("tok", "000001")
    assert get.call_count == 1
    assert no_network_no_env == []


@pytest.mark.parametrize("payload", [
    {"output": {}, "msg1": "조회할 자료가 없습니다"},   # HTTP 200 안의 KIS 오류
    {"output": {"stck_prpr": ""}},
    {},
])
def test_invalid_price_payload_is_not_retried(monkeypatch, no_network_no_env,
                                              payload):
    get = _sequence(monkeypatch, [_resp(payload)])
    with pytest.raises(RuntimeError):
        k.get_current_price("tok", "000001")
    assert get.call_count == 1


@pytest.mark.parametrize("payload,tr_cont", [
    ({"rt_cd": "1", "msg1": "오류", "output1": [], "output2": [{}]}, ""),
    ({"rt_cd": "0", "output1": None, "output2": [{}]}, ""),
    ({"rt_cd": "0", "output1": [], "output2": []}, ""),
    (BALANCE_OK, "F"),                      # 연속조회 필요 - 전체 잔고 미확인
])
def test_invalid_balance_payload_is_not_retried(monkeypatch, no_network_no_env,
                                                payload, tr_cont):
    get = _sequence(monkeypatch, [_resp(payload, tr_cont=tr_cont)])
    with pytest.raises(RuntimeError):
        k.get_account_balance("tok")
    assert get.call_count == 1
    assert no_network_no_env == []


# ── 5. 전역 재시도 예산 - 여러 종목이 동시에 흔들려도 무한정 늘지 않는다 ──

def test_retry_budget_exhaustion_falls_back_to_single_attempt(
        monkeypatch, no_network_no_env):
    k._read_retry_spent = k.READ_RETRY_TOTAL_BUDGET_SECONDS
    get = _sequence(monkeypatch, [_raising_resp(_http_error(500))])
    with pytest.raises(requests.HTTPError):
        k.get_current_price("tok", "000001")
    assert get.call_count == 1              # 예산 소진 뒤에는 재시도하지 않는다
    assert no_network_no_env == []


def test_retry_budget_accumulates_across_tickers(monkeypatch,
                                                 no_network_no_env):
    """여러 종목이 연달아 흔들리면 재시도에 쓴 시간이 누적돼 곧 멈춘다.

    실제 대기·요청 시간은 mock으로 0이라, 한 주기를 예산의 60%로 세는
    가짜 시계를 끼워 "누적되어 예산을 넘으면 더는 재시도하지 않는다"만
    확인한다.
    """
    clock = {"t": 0.0}
    cycle = k.READ_RETRY_TOTAL_BUDGET_SECONDS * 0.6

    def fake_monotonic():
        t = clock["t"]
        clock["t"] += cycle             # 호출 쌍(시작/종료)마다 한 주기씩
        return t

    monkeypatch.setattr(k.time, "monotonic", fake_monotonic)
    get = _sequence(monkeypatch, [_raising_resp(_http_error(500))])

    counts = []
    for _ in range(3):
        before = get.call_count
        with pytest.raises(requests.HTTPError):
            k.get_current_price("tok", "000001")
        counts.append(get.call_count - before)

    # 1~2번째 종목은 재시도까지 가고, 예산이 넘어간 뒤로는 1회로 떨어진다.
    assert counts[0] == 3
    assert counts[-1] == 1


# ── 6. 읽기 재시도 중에는 주문·메시지·노션 쓰기가 일어나지 않는다 ──

def test_read_retry_does_not_order_notify_or_write(monkeypatch,
                                                   no_network_no_env):
    post = Mock(side_effect=AssertionError("읽기 재시도 중 POST 발생"))
    monkeypatch.setattr(k.requests, "post", post)
    for name in ("_notify_failure", "_notify_warning_throttled",
                 "_send_market_buy", "_send_market_sell",
                 "place_market_buy_order", "place_market_sell_order"):
        monkeypatch.setattr(k, name, Mock(side_effect=AssertionError(name)))
    for name in ("_create_pending_record", "update_order_record",
                 "close_auto_holding", "create_auto_holding"):
        if hasattr(n, name):
            monkeypatch.setattr(n, name, Mock(side_effect=AssertionError(name)))

    _sequence(monkeypatch, [
        _raising_resp(_http_error(500)),
        _raising_resp(_http_error(500)),
        _resp(PRICE_OK),
    ])
    assert k.get_current_price("tok", "000001") == "10500"

    k._reset_read_retry_budget()
    _sequence(monkeypatch, [_raising_resp(requests.Timeout("t"))])
    with pytest.raises(requests.Timeout):
        k.get_account_balance("tok")
    post.assert_not_called()


# ── 7. run_auto_sell(): 복구되면 판정을 잇고, 끝내 실패하면 주문하지 않는다 ──

@pytest.fixture
def sell_env(monkeypatch):
    """손절선(9,000)을 깬 가격이 오면 매도가 나가는 최소 환경."""
    inp = core.HoldingInput('000001', 'test', 'KOSPI', 10000, 100,
                            prev_stop_loss=9000)
    for name in ('_auto_trade_configured', '_within_trading_hours',
                 '_is_trading_day'):
        monkeypatch.setattr(k, name, Mock(return_value=True))
    monkeypatch.setattr(k, '_today', lambda: date(2026, 9, 15))
    monkeypatch.setattr(k, 'get_access_token', Mock(return_value='tok'))
    monkeypatch.setattr(k, '_notify_warning_throttled', Mock())
    monkeypatch.setattr(k, '_notify_failure', Mock())
    monkeypatch.setattr(k, 'place_market_sell_order',
                        Mock(return_value={'status': 'sent', 'order_no': '1',
                                           'page_id': 'order'}))
    monkeypatch.setattr(k, '_reconcile_sell', Mock())
    monkeypatch.setattr(n, 'fetch_unconfirmed_sell_orders',
                        Mock(return_value=[]))
    return inp


def test_auto_sell_continues_after_balance_recovers(monkeypatch, sell_env,
                                                    no_network_no_env):
    # 잔고조회가 한 번 시간초과 뒤 복구되고, 이어서 현재가 조회도
    # HTTP 500 한 번 뒤 복구된다 - 판정이 이어져 매도가 나가야 한다.
    _sequence(monkeypatch, [
        _raising_resp(requests.Timeout("read timeout")),
        _resp(BALANCE_OK),
        _raising_resp(_http_error(500)),
        _resp({"output": {"stck_prpr": "8500", "mrkt_warn_cls_code": "00"}}),
    ])
    k.run_auto_sell([('holding', sell_env)])
    k.place_market_sell_order.assert_called_once()
    assert k.place_market_sell_order.call_args.kwargs['ref_price'] == 8500.0
    # 매도 수량은 성공한 최신 잔고 응답(ord_psbl_qty)에서 온다.
    assert k.place_market_sell_order.call_args.args[2] == 100


def test_auto_sell_holds_when_balance_never_recovers(monkeypatch, sell_env,
                                                     no_network_no_env):
    get = _sequence(monkeypatch, [_raising_resp(requests.Timeout("t"))])
    k.run_auto_sell([('holding', sell_env)])
    assert get.call_count == 3              # 잔고조회 3회에서 멈춘다
    k.place_market_sell_order.assert_not_called()
    k._notify_warning_throttled.assert_called_once()


def test_auto_sell_holds_when_price_never_recovers(monkeypatch, sell_env,
                                                   no_network_no_env):
    _sequence(monkeypatch, [
        _resp(BALANCE_OK),
        _raising_resp(_http_error(500)),
        _raising_resp(_http_error(500)),
        _raising_resp(_http_error(500)),
    ])
    k.run_auto_sell([('holding', sell_env)])
    k.place_market_sell_order.assert_not_called()
