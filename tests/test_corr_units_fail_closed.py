"""노션 상관군 조회가 실패하면 그 회차 매수를 통째로 건너뛴다(fail-closed).

예전 동작: `get_mock_account_corr_units`가 조회 실패 시
`{"total_units": 0.0, "groups": {}}`를 돌려줬다. 그러면 상관군 캡뿐 아니라
**전체 캡까지 같이 풀린다** - 실제로 10유닛을 들고 있어도 total_units가
0으로 와서 `total_units + 1 > MAX_UNITS_TOTAL(12)`이 성립하지 않기 때문이다.
당시 docstring은 "전체 캡만 적용"이라고 했지만 두 캡 모두 무효였다.

같은 함수 안의 `count_success_orders_today` 실패는 "안전을 위해 주문 중단"
(fail-closed)인데 여기만 반대 방향이었던 것도 일관성이 없었다.

자동매도(run_auto_sell)는 이 집계를 쓰지 않으므로 영향이 없어야 한다 -
"못 파는 쪽이 더 위험하다"는 기존 원칙 그대로다.
"""

from __future__ import annotations

from unittest import mock

import pytest

import core
import kis_client
import notion_repo
from helpers import block_external_http, scrub_credential_env


@pytest.fixture
def env(monkeypatch):
    scrub_credential_env(monkeypatch)
    block_external_http(monkeypatch)
    monkeypatch.setenv("KIS_APP_KEY", "dummy-key")
    monkeypatch.setenv("KIS_APP_SECRET", "dummy-secret")
    monkeypatch.setenv("KIS_ACCOUNT", "12345678-01")
    monkeypatch.setattr(kis_client, "MAX_ORDERS_PER_DAY", 3)
    monkeypatch.setattr(kis_client, "get_account_balance",
                        lambda token: {"account_size": 10_000_000.0,
                                       "available_cash": 1_000_000_000.0,
                                       "holdings": []})
    monkeypatch.setattr(notion_repo, "count_success_orders_today",
                        lambda day, acct: 0)
    monkeypatch.setattr(notion_repo, "fetch_unconfirmed_sell_orders", lambda *a: [])
    monkeypatch.setattr(notion_repo, "find_auto_holding_page", lambda ticker: None)
    monkeypatch.setattr(kis_client, "get_price_quote",
                        lambda token, ticker: {"price": 10_000.0,
                                               "market_warned": False})
    warned: list[tuple[str, str]] = []
    monkeypatch.setattr(kis_client, "_notify_warning_throttled",
                        lambda k, m: warned.append((k, m)))
    monkeypatch.setattr(kis_client, "_notify_failure", lambda m: None)
    monkeypatch.setattr(kis_client, "_track_no_entry", lambda *a, **k: None)
    monkeypatch.setattr(kis_client, "_track_no_entry_all", lambda *a, **k: None)
    return warned


def _break_notion(monkeypatch):
    """점검표 조회만 끊는다 - 다른 노션 조회는 살아 있는 부분 장애 상황."""
    monkeypatch.setattr(
        notion_repo, "fetch_holdings",
        lambda **kw: (_ for _ in ()).throw(RuntimeError("노션 점검표 접근 불가")))


def _candidate(ticker="000001", sector="전기전자"):
    return {"ticker": ticker, "name": f"종목{ticker}", "sector": sector,
            "price": 10_000.0, "atr20": 500.0, "high20": 9_900.0,
            "gap_atr": 0.2, "market": "KOSPI"}


# ── 집계 함수 자체 ──────────────────────────────────────────

def test_lookup_failure_raises_instead_of_returning_zero(env, monkeypatch):
    _break_notion(monkeypatch)

    with pytest.raises(kis_client.CorrUnitsUnavailable):
        kis_client.get_mock_account_corr_units(10_000_000.0, [])

    # 경고는 예전과 같은 키로 그대로 나간다.
    assert [k for k, _ in env] == [kis_client.WARN_NOTION_CORR_FAILED]


def test_success_path_unchanged(env, monkeypatch):
    """정상일 때는 예전과 같은 모양을 돌려준다."""
    monkeypatch.setattr(notion_repo, "fetch_holdings", lambda **kw: [])
    monkeypatch.setattr(kis_client, "_get_sector_map", lambda: {})

    assert kis_client.get_mock_account_corr_units(10_000_000.0, []) == {
        "total_units": 0.0, "groups": {}}
    assert env == []


# ── 신규매수: 그 회차 전체 보류 ─────────────────────────────

def test_new_buy_skipped_entirely(env, monkeypatch):
    _break_notion(monkeypatch)
    quotes = mock.Mock(side_effect=AssertionError("후보 검사까지 진행했다"))
    monkeypatch.setattr(kis_client, "get_price_quote", quotes)

    selected = kis_client.select_buy_candidates("tok", [_candidate(), _candidate("000002")])

    assert selected == []                   # 0건
    quotes.assert_not_called()
    assert [k for k, _ in env] == [kis_client.WARN_NOTION_CORR_FAILED]


def test_new_buy_would_have_passed_caps_without_the_failure(env, monkeypatch):
    """대조군 - 조회가 되면 같은 후보가 통과한다.

    이게 없으면 "원래 못 사는 후보였다"와 구분이 안 된다.
    """
    monkeypatch.setattr(notion_repo, "fetch_holdings", lambda **kw: [])
    monkeypatch.setattr(kis_client, "_get_sector_map", lambda: {})

    selected = kis_client.select_buy_candidates("tok", [_candidate()])

    assert len(selected) == 1


# ── 추가매수: 그 회차 전체 보류 ─────────────────────────────

def test_pyramid_skipped_entirely(env, monkeypatch):
    """추가매수도 종목별 판정에 들어가기 전에 멈춘다.

    probe: run_auto_pyramid은 상관군 집계 **직후** _get_sector_map()을 부른다.
    그게 안 불렸다면 집계 단계에서 멈춘 것이다.
    """
    _break_notion(monkeypatch)
    inp = core.HoldingInput("000001", "종목", "KOSPI", 10_000.0, 10,
                            units=1, corr_group="전기전자")
    monkeypatch.setattr(kis_client, "_within_trading_hours", lambda *a, **k: True)
    monkeypatch.setattr(kis_client, "_is_trading_day", lambda *a, **k: True)
    monkeypatch.setattr(kis_client, "_auto_trade_configured", lambda *a, **k: True)
    # 자동매매 제어(노션) 게이트는 이 테스트 대상이 아니다 - 통과시킨다.
    monkeypatch.setattr(notion_repo, "get_auto_trade_status", lambda *a, **k: "실행중")
    monkeypatch.setattr(kis_client, "get_access_token", lambda *a, **k: "tok")
    monkeypatch.setattr(kis_client, "get_account_balance",
                        lambda token: {"account_size": 10_000_000.0,
                                       "available_cash": 1_000_000_000.0,
                                       "holdings": [{"ticker": "000001", "qty": 10}]})
    sector_map = mock.Mock(return_value={})
    monkeypatch.setattr(kis_client, "_get_sector_map", sector_map)

    kis_client.run_auto_pyramid(holdings=[inp])

    sector_map.assert_not_called()          # 집계 단계에서 멈췄다
    assert kis_client.WARN_NOTION_CORR_FAILED in [k for k, _ in env]


# ── 자동매도는 무관 ─────────────────────────────────────────

def test_auto_sell_does_not_use_corr_units():
    """자동매도는 이 집계를 아예 참조하지 않는다 - 소스로 고정한다."""
    import inspect
    src = inspect.getsource(kis_client.run_auto_sell)
    assert "get_mock_account_corr_units" not in src
    assert "CorrUnitsUnavailable" not in src
