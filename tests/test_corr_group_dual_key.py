"""상관군 유닛을 노션 상관군 + 업종 맵 두 키로 집계한다.

배경: 상관군 키가 두 어휘로 갈려 있었다.
  - 집계·추가매수: 노션 '상관군'(사람이 쓰는 자유 텍스트 - 반도체, 전력기기…)
  - 신규매수 후보의 sector: 스캔 CSV = 업종 맵(KRX 지수 업종명 - 전기전자…)

2026-10-06 점검표의 상관군 12개 값 중 6개(반도체·전력기기·로봇·바이오·
플랫폼·유아용품)가 업종 맵에 없었다. 보유를 '반도체'로 적어두면 집계는
{'반도체': 6}인데 반도체 후보의 sector는 '전기전자'라
groups.get('전기전자', 0) == 0이 되어, **같은 리스크 군인데 캡을 통과한다**.

두 키 모두에 더해 어느 어휘로 물어도 같은 유닛이 잡히게 한다.
전체 유닛(total_units)은 키와 무관한 포지션 수라 한 번만 더한다.
"""

from __future__ import annotations

import pytest

import core
import kis_client
import notion_repo
from helpers import block_external_http, scrub_credential_env


def _holding(ticker, name, corr_group, units):
    return ("page-" + ticker,
            core.HoldingInput(ticker, name, "KOSDAQ", 10_000.0, 100,
                              units=units, corr_group=corr_group))


@pytest.fixture
def env(monkeypatch):
    scrub_credential_env(monkeypatch)
    block_external_http(monkeypatch)
    monkeypatch.setenv("KIS_APP_KEY", "dummy-key")
    monkeypatch.setenv("KIS_APP_SECRET", "dummy-secret")
    monkeypatch.setenv("KIS_ACCOUNT", "12345678-01")
    monkeypatch.setattr(kis_client, "MAX_ORDERS_PER_DAY", 3)
    monkeypatch.setattr(notion_repo, "count_success_orders_today", lambda d, a: 0)
    monkeypatch.setattr(notion_repo, "fetch_unconfirmed_sell_orders", lambda *a: [])
    monkeypatch.setattr(notion_repo, "find_auto_holding_page", lambda t: None)
    monkeypatch.setattr(kis_client, "get_account_balance",
                        lambda token: {"account_size": 10_000_000.0,
                                       "available_cash": 1_000_000_000.0,
                                       "holdings": [{"ticker": "200470", "qty": 1}]})
    monkeypatch.setattr(kis_client, "get_price_quote",
                        lambda token, ticker: {"price": 10_000.0,
                                               "market_warned": False})
    monkeypatch.setattr(kis_client, "_notify_warning_throttled", lambda k, m: None)
    monkeypatch.setattr(kis_client, "_notify_failure", lambda m: None)
    monkeypatch.setattr(kis_client, "_track_no_entry", lambda *a, **k: None)
    monkeypatch.setattr(kis_client, "_track_no_entry_all", lambda *a, **k: None)
    monkeypatch.setattr(kis_client, "_notify_new_buy_rejections", lambda *a, **k: None)


def _candidate(ticker="000999", sector="전기전자"):
    return {"ticker": ticker, "name": "반도체후보", "sector": sector,
            "price": 10_000.0, "atr20": 500.0, "high20": 9_900.0,
            "gap_atr": 0.2, "market": "KOSDAQ"}


# ── 집계 ────────────────────────────────────────────────────

def test_units_counted_under_both_keys(env, monkeypatch):
    """노션 '반도체' + 맵 '전기전자' → 두 키 모두에 6유닛."""
    monkeypatch.setattr(notion_repo, "fetch_holdings", lambda **kw: [
        _holding("200470", "에이팩트", "반도체", 3),
        _holding("425420", "티에프이", "반도체", 3),
    ])
    monkeypatch.setattr(kis_client, "_get_sector_map",
                        lambda: {"200470": "전기전자", "425420": "전기전자"})

    corr = kis_client.get_mock_account_corr_units(10_000_000.0, [])

    assert corr["groups"] == {"반도체": 6.0, "전기전자": 6.0}
    assert corr["total_units"] == 6.0       # 전체는 한 번만


def test_same_key_counted_once(env, monkeypatch):
    """노션과 맵이 같으면 두 배로 세지 않는다."""
    monkeypatch.setattr(notion_repo, "fetch_holdings", lambda **kw: [
        _holding("200470", "에이팩트", "전기전자", 3)])
    monkeypatch.setattr(kis_client, "_get_sector_map",
                        lambda: {"200470": "전기전자"})

    corr = kis_client.get_mock_account_corr_units(10_000_000.0, [])

    assert corr["groups"] == {"전기전자": 3.0}
    assert corr["total_units"] == 3.0


def test_empty_keys_are_skipped(env, monkeypatch):
    """상관군이 비었고 맵에도 없으면 어느 그룹에도 안 들어간다."""
    monkeypatch.setattr(notion_repo, "fetch_holdings", lambda **kw: [
        _holding("999999", "미분류종목", "", 2)])
    monkeypatch.setattr(kis_client, "_get_sector_map", lambda: {})

    corr = kis_client.get_mock_account_corr_units(10_000_000.0, [])

    assert corr["groups"] == {}
    assert corr["total_units"] == 2.0       # 전체에는 들어간다


def test_map_failure_falls_back_to_notion_key_only(env, monkeypatch):
    """맵을 못 읽으면 노션 키만으로 센다 - 기존 동작 유지."""
    monkeypatch.setattr(notion_repo, "fetch_holdings", lambda **kw: [
        _holding("200470", "에이팩트", "반도체", 3)])
    monkeypatch.setattr(kis_client, "_get_sector_map", lambda: {})

    corr = kis_client.get_mock_account_corr_units(10_000_000.0, [])

    assert corr["groups"] == {"반도체": 3.0}


def test_dual_key_holdings_are_logged(env, monkeypatch, caplog):
    monkeypatch.setattr(notion_repo, "fetch_holdings", lambda **kw: [
        _holding("200470", "에이팩트", "반도체", 3),
        _holding("122640", "예스티", "기계·장비", 3),   # 노션=맵, 이중 아님
    ])
    monkeypatch.setattr(kis_client, "_get_sector_map",
                        lambda: {"200470": "전기전자", "122640": "기계·장비"})

    with caplog.at_level("INFO"):
        kis_client.get_mock_account_corr_units(10_000_000.0, [])

    assert "상관군 이중 집계" in caplog.text
    assert "에이팩트(200470)" in caplog.text
    assert "예스티" not in caplog.text       # 갈리지 않은 종목은 안 찍는다


# ── 캡 적용: 신규매수 ───────────────────────────────────────

def test_new_buy_blocked_by_map_key(env, monkeypatch):
    """노션 '반도체' 6유닛 보유 + 맵 '전기전자' → 전기전자 후보가 캡에 걸린다.

    수정 전에는 groups == {'반도체': 6}뿐이라 이 후보가 통과했다.
    """
    monkeypatch.setattr(notion_repo, "fetch_holdings", lambda **kw: [
        _holding("200470", "에이팩트", "반도체", 3),
        _holding("425420", "티에프이", "반도체", 3),
    ])
    monkeypatch.setattr(kis_client, "_get_sector_map",
                        lambda: {"200470": "전기전자", "425420": "전기전자"})

    selected = kis_client.select_buy_candidates("tok", [_candidate(sector="전기전자")])

    assert selected == []                   # 상관군 캡(6/6)에 막힘


def test_new_buy_still_allowed_for_unrelated_group(env, monkeypatch):
    """상관없는 업종 후보는 그대로 통과한다 - 과잉 차단이 아님을 고정."""
    monkeypatch.setattr(notion_repo, "fetch_holdings", lambda **kw: [
        _holding("200470", "에이팩트", "반도체", 3),
        _holding("425420", "티에프이", "반도체", 3),
    ])
    monkeypatch.setattr(kis_client, "_get_sector_map",
                        lambda: {"200470": "전기전자", "425420": "전기전자"})

    selected = kis_client.select_buy_candidates("tok", [_candidate(sector="화학")])

    assert len(selected) == 1


# ── 캡 적용: 추가매수도 같은 집계를 쓴다 ────────────────────

def test_pyramid_uses_the_same_aggregation():
    """추가매수가 get_mock_account_corr_units 결과를 그대로 쓰는지 소스로 고정."""
    import inspect
    src = inspect.getsource(kis_client.run_auto_pyramid)
    assert "get_mock_account_corr_units" in src
    assert 'group_units = dict(corr["groups"])' in src
    # 추가매수 조회 키는 노션 우선 + 맵 폴백 - 집계가 두 키를 다 담으므로
    # 어느 쪽으로 물어도 같은 유닛이 잡힌다.
    assert "inp.corr_group or sector_map.get(inp.ticker" in src
