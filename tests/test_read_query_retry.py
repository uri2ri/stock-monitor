"""현재가·잔고 조회의 제한된 재시도 검증.

배경(2026-09-15 운영 로그, 한국시간): 09:23·09:33·09:43 현재가 조회가
HTTP 500으로, 13:23·15:03 잔고조회가 10초 시간초과로 실패해 그 회차의
자동매도 판정(13:23은 추가매수까지)이 생략됐다. 워크플로 자체는 성공으로
끝나 겉으로는 드러나지 않았다.

여기서 검증하는 건 "kis_client가 그런 일시적 실패에 한해 같은 회차 안에서
다시 물어보고, 끝내 실패하면 기존 보류 경로로 넘긴다"는 코드 동작뿐이다.
실 네트워크·실 자격증명·실 대기는 전부 차단·mock하므로 이 통과가 실제 KIS
장애에서의 복구를 확인해 주지는 않는다.
"""

from __future__ import annotations

import os
from datetime import date
from unittest.mock import Mock

import pytest
import requests

import core
import kis_client as k
import notion_repo as n
from tests.helpers import (CREDENTIAL_ENV_VARS, block_external_http,
                           scrub_credential_env)


# ── 공용 픽스처 ─────────────────────────────────────────────

class _Clock:
    """테스트용 가짜 단조 시계.

    재시도 예산은 time.monotonic()으로 잰다. 실제로 자거나 기다리지
    않으면서 "재시도 도중 예산이 소진되는" 상황을 재현하려면 시간의
    흐름을 테스트가 정해야 한다 - sleep()은 잠든 만큼, 조회 호출은
    request_seconds만큼 시계를 민다.
    """

    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []
        self.request_seconds = 0.0      # 조회 한 번이 걸리는 (가짜) 시간

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture(autouse=True)
def read_retry_env(monkeypatch):
    """실 자격증명·실 네트워크·실 대기를 차단하고 가짜 시계를 끼운다.

    `.env` 로딩 자체는 픽스처로 막을 수 없다 - kis_client는 import
    시점에 load_dotenv()를 부르고, 그 import(이 파일 위쪽)는 픽스처보다
    먼저 일어난다. 그래서 "로딩을 막는" 대신 로딩된 결과를 지운다:
    scrub_credential_env()가 .env.example의 키를 전부 환경에서
    제거하고, 그 다음 이 조회 경로에 필요한 세 개만 테스트용 더미로
    다시 넣는다. 자격증명은 전부 호출 시점에 os.environ에서 읽히므로
    (모듈 상수로 굳지 않는다) 실제 값이 쓰일 여지가 없다.

    HTTP는 requests의 전 메서드 + Session.request를 막아두고, 각
    테스트가 필요한 조회 응답만 requests.get을 mock해서 열어준다.
    """
    scrub_credential_env(monkeypatch)
    for name, value in (("KIS_APP_KEY", "test-app-key"),
                        ("KIS_APP_SECRET", "test-app-secret"),
                        ("KIS_ACCOUNT", "12345678-01")):
        monkeypatch.setenv(name, value)
    block_external_http(monkeypatch)

    clock = _Clock()
    monkeypatch.setattr(k.time, "monotonic", clock.now)
    monkeypatch.setattr(k.time, "sleep", clock.sleep)

    # 예산은 프로세스 전역이라 테스트 간 누수를 막는다.
    k._reset_read_retry_budget()
    yield clock
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


def _sequence(monkeypatch, items: list, clock: _Clock | None = None):
    """requests.get이 items를 순서대로 돌려주거나(Mock 응답) 던지게(예외) 한다.

    clock을 주면 호출 한 번마다 clock.request_seconds만큼 가짜 시계를
    밀어, 조회가 시간을 잡아먹는 상황(=재시도 예산 소모)을 재현한다.
    """
    calls = {"n": 0}

    def fake_get(*a, **kw):
        i = calls["n"]
        calls["n"] += 1
        if clock is not None:
            clock.advance(clock.request_seconds)
        item = items[i] if i < len(items) else items[-1]
        if isinstance(item, Exception):
            raise item
        return item

    mock = Mock(side_effect=fake_get)
    monkeypatch.setattr(k.requests, "get", mock)
    return mock


# ── 0. 격리 자체 점검 - 이 파일의 다른 테스트가 서 있는 전제 ──

def test_every_http_method_is_blocked(read_retry_env):
    """mock하지 않은 HTTP는 메서드를 가리지 않고 즉시 드러난다."""
    for name in ("request", "get", "post", "put", "patch", "delete",
                 "head", "options"):
        with pytest.raises(AssertionError):
            getattr(requests, name)("https://blocked.invalid")
    # Session을 직접 만들어 쓰는 경로도 같이 막힌다.
    with pytest.raises(AssertionError):
        requests.Session().get("https://blocked.invalid")


def test_real_credentials_are_not_in_environment(read_retry_env):
    """실제 자격증명은 환경에서 제거되고, 더미만 남는다.

    값을 출력하지 않고 "테스트용 더미인지"와 "아예 없는지"만 본다.
    """
    assert os.environ["KIS_APP_KEY"] == "test-app-key"
    assert os.environ["KIS_APP_SECRET"] == "test-app-secret"
    assert os.environ["KIS_ACCOUNT"] == "12345678-01"
    stubbed = {"KIS_APP_KEY", "KIS_APP_SECRET", "KIS_ACCOUNT"}
    leaked = [name for name in CREDENTIAL_ENV_VARS
              if name not in stubbed and name in os.environ]
    assert leaked == []


# ── 1. 최초 성공이면 재시도가 붙지 않는다 ────────────────────

def test_price_first_try_success_calls_once(monkeypatch, read_retry_env):
    get = _sequence(monkeypatch, [_resp(PRICE_OK)])
    assert k.get_current_price("tok", "000001") == "10500"
    assert get.call_count == 1
    assert read_retry_env.sleeps == []          # 대기 없음


def test_balance_first_try_success_calls_once(monkeypatch, read_retry_env):
    get = _sequence(monkeypatch, [_resp(BALANCE_OK)])
    assert k.get_account_balance("tok")["account_size"] == 10_000_000
    assert get.call_count == 1
    assert read_retry_env.sleeps == []


# ── 2. 일시적 실패 뒤 성공하면 정상 응답을 쓴다 ───────────────

@pytest.mark.parametrize("failure", [
    _http_error(500),                       # 2026-09-15 09:23 현재가 조회
    _http_error(502),
    _http_error(503),
    _http_error(504),
    requests.ConnectionError("connection reset"),
    requests.Timeout("read timeout"),       # 2026-09-15 13:23 잔고조회
])
def test_price_recovers_within_limit(monkeypatch, read_retry_env, failure):
    get = _sequence(monkeypatch, [_raising_resp(failure), _resp(PRICE_OK)])
    assert k.get_price_quote("tok", "000001")["price"] == 10500.0
    assert get.call_count == 2
    assert read_retry_env.sleeps == [1.0]       # 첫 재시도 대기만


def test_balance_recovers_on_third_attempt(monkeypatch, read_retry_env):
    get = _sequence(monkeypatch, [
        _raising_resp(requests.Timeout("read timeout")),
        _raising_resp(_http_error(500)),
        _resp(BALANCE_OK),
    ])
    assert k.get_account_balance("tok")["available_cash"] == 3_000_000
    assert get.call_count == 3
    assert read_retry_env.sleeps == [1.0, 2.0]  # 점증 대기


# ── 3. 계속 실패하면 최대 횟수에서 멈추고 예외를 올린다 ───────

def test_price_stops_at_max_attempts(monkeypatch, read_retry_env):
    get = _sequence(monkeypatch, [_raising_resp(_http_error(500))])
    with pytest.raises(requests.HTTPError):
        k.get_current_price("tok", "000001")
    assert get.call_count == 3              # 최초 호출 포함 최대 3회
    assert read_retry_env.sleeps == [1.0, 2.0]


def test_balance_stops_at_max_attempts(monkeypatch, read_retry_env):
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
def test_non_retryable_http_fails_immediately(monkeypatch, read_retry_env,
                                              failure):
    get = _sequence(monkeypatch, [_raising_resp(failure)])
    with pytest.raises(requests.HTTPError):
        k.get_current_price("tok", "000001")
    assert get.call_count == 1
    assert read_retry_env.sleeps == []


@pytest.mark.parametrize("payload", [
    {"output": {}, "msg1": "조회할 자료가 없습니다"},   # HTTP 200 안의 KIS 오류
    {"output": {"stck_prpr": ""}},
    {},
])
def test_invalid_price_payload_is_not_retried(monkeypatch, read_retry_env,
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
def test_invalid_balance_payload_is_not_retried(monkeypatch, read_retry_env,
                                                payload, tr_cont):
    get = _sequence(monkeypatch, [_resp(payload, tr_cont=tr_cont)])
    with pytest.raises(RuntimeError):
        k.get_account_balance("tok")
    assert get.call_count == 1
    assert read_retry_env.sleeps == []


# ── 5. 전역 재시도 예산 - 여러 종목이 동시에 흔들려도 무한정 늘지 않는다 ──

def test_retry_budget_exhaustion_falls_back_to_single_attempt(
        monkeypatch, read_retry_env):
    monkeypatch.setattr(k, "_read_retry_spent",
                        k.READ_RETRY_TOTAL_BUDGET_SECONDS)
    get = _sequence(monkeypatch, [_raising_resp(_http_error(500))])
    with pytest.raises(requests.HTTPError):
        k.get_current_price("tok", "000001")
    assert get.call_count == 1              # 예산 소진 뒤에는 재시도하지 않는다
    assert read_retry_env.sleeps == []


def test_budget_exhausted_during_wait_stops_before_next_call(
        monkeypatch, read_retry_env):
    """대기 도중 예산이 넘어가면 다음 호출을 걸지 않는다.

    묶음 시작 때만 검사하면 남은 0.5초로 1초를 자고 또 호출하게 된다 -
    대기 뒤 호출 직전에 다시 확인해야 거기서 멈춘다.
    """
    monkeypatch.setattr(k, "_read_retry_spent",
                        k.READ_RETRY_TOTAL_BUDGET_SECONDS - 0.5)
    read_retry_env.request_seconds = 0.0
    get = _sequence(monkeypatch, [_raising_resp(_http_error(500))],
                    read_retry_env)

    with pytest.raises(requests.HTTPError) as exc:
        k.get_current_price("tok", "000001")

    # 마지막 조회 오류가 그대로 올라와 기존 보류 경로로 간다.
    assert exc.value.response.status_code == 500
    assert get.call_count == 1               # 대기는 했지만 재호출은 없다
    assert read_retry_env.sleeps == [1.0]


def test_budget_exhausted_during_retry_call_stops_further_retries(
        monkeypatch, read_retry_env):
    """재시도 호출 자체가 예산을 다 먹으면 그 다음 시도로 넘어가지 않는다."""
    monkeypatch.setattr(k, "_read_retry_spent",
                        k.READ_RETRY_TOTAL_BUDGET_SECONDS - 4.0)
    read_retry_env.request_seconds = 5.0     # 조회 한 번이 5초 걸린다
    get = _sequence(monkeypatch, [_raising_resp(requests.Timeout("t"))],
                    read_retry_env)

    with pytest.raises(requests.Timeout):
        k.get_account_balance("tok")

    # 남은 4초로 1초 자고 한 번 더 걸었지만(5초 소모), 3회째는 없다.
    assert get.call_count == 2
    assert read_retry_env.sleeps == [1.0]


def test_retry_budget_accumulates_across_tickers(monkeypatch, read_retry_env):
    """여러 종목이 연달아 흔들리면 재시도에 쓴 시간이 누적돼 곧 멈춘다.

    가짜 시계로 조회 한 번을 10초(= requests timeout 상한과 같은 값)로
    잡으면 한 종목의 재시도 묶음이 대기 1s + 10s + 대기 2s + 10s = 23초를
    쓴다. 예산 60초는 세 종목이면 바닥나고, 그 뒤 종목은 재시도 없이
    1회 호출로 떨어진다.
    """
    read_retry_env.request_seconds = 10.0
    get = _sequence(monkeypatch, [_raising_resp(_http_error(500))],
                    read_retry_env)

    counts = []
    for _ in range(4):
        before = get.call_count
        with pytest.raises(requests.HTTPError):
            k.get_current_price("tok", "000001")
        counts.append(get.call_count - before)

    assert counts == [3, 3, 3, 1]
    # 예산을 넘긴 뒤로는 재시도 대기도 붙지 않는다.
    assert read_retry_env.sleeps == [1.0, 2.0] * 3


# ── 6. 읽기 재시도 중에는 주문·메시지·노션 쓰기가 일어나지 않는다 ──

def test_read_retry_does_not_order_notify_or_write(monkeypatch,
                                                   read_retry_env):
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
                                                    read_retry_env):
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
                                                     read_retry_env):
    get = _sequence(monkeypatch, [_raising_resp(requests.Timeout("t"))])
    k.run_auto_sell([('holding', sell_env)])
    assert get.call_count == 3              # 잔고조회 3회에서 멈춘다
    k.place_market_sell_order.assert_not_called()
    k._notify_warning_throttled.assert_called_once()


def test_auto_sell_holds_when_price_never_recovers(monkeypatch, sell_env,
                                                   read_retry_env):
    _sequence(monkeypatch, [
        _resp(BALANCE_OK),
        _raising_resp(_http_error(500)),
        _raising_resp(_http_error(500)),
        _raising_resp(_http_error(500)),
    ])
    k.run_auto_sell([('holding', sell_env)])
    k.place_market_sell_order.assert_not_called()
