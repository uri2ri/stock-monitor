"""장중 경로는 업종 맵을 재생성하지 않고 캐시만 읽는다.

배경(2026-10-06 확인): 업종 맵 재생성은 KRX 지수 목록 조회가 필요하고 그
엔드포인트는 **로그인을 요구한다**. 자격증명 없이 부르면 KRX가
`HTTP 400 "LOGOUT"`(6바이트 text/html)을 돌려주고, JSON이 아니라서 pykrx의
`@dataframe_empty_handler`가 빈 DataFrame으로 떨어뜨린 뒤
`self.df["시장"]`에서 `KeyError: '시장'`이 난다. 우리 쪽 `_call`이 3회
재시도해서 "KOSPI 지수 목록 3회 실패: '시장'"으로 보였다.

장중(auto-trade) 워크플로에는 KRX_ID/KRX_PW가 없으므로 시도해봐야 매번
실패하고 회차 시간만 먹는다. 그래서 캐시만 읽고, 너무 낡으면 하루 한 번
경고한다. 생성은 자격증명이 있는 야간 스캔이 맡는다.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from unittest import mock

import pytest

import kis_client
import screener
from helpers import block_external_http, scrub_credential_env


@pytest.fixture
def cache(tmp_path, monkeypatch):
    scrub_credential_env(monkeypatch)
    block_external_http(monkeypatch)
    path = tmp_path / "sector_map.json"
    monkeypatch.setattr(screener, "SECTOR_PATH", path)
    warned: list[tuple] = []
    monkeypatch.setattr(kis_client, "_notify_warning_throttled",
                        lambda k, m, **kw: warned.append((k, m, kw)))
    return {"path": path, "warned": warned}


def _write(path, built_on: date, mapping=None):
    path.write_text(json.dumps({
        "built_on": built_on.isoformat(), "as_of": built_on.strftime("%Y%m%d"),
        "map": mapping if mapping is not None else {"005930": "전기전자"},
    }, ensure_ascii=False), encoding="utf-8")


# ── 재생성 금지 ─────────────────────────────────────────────

def test_intraday_never_rebuilds_even_when_stale(cache, monkeypatch):
    """100일 된 캐시여도 재생성을 부르지 않는다."""
    _write(cache["path"], date.today() - timedelta(days=100))
    build = mock.Mock(side_effect=AssertionError("재생성을 시도했다"))
    monkeypatch.setattr(screener, "build_sector_map", build)
    load = mock.Mock(side_effect=AssertionError("load_sector_map을 탔다"))
    monkeypatch.setattr(screener, "load_sector_map", load)

    assert kis_client._get_sector_map() == {"005930": "전기전자"}
    build.assert_not_called()
    load.assert_not_called()


def test_fresh_cache_is_returned_without_warning(cache, monkeypatch):
    _write(cache["path"], date.today() - timedelta(days=3))
    monkeypatch.setattr(screener, "build_sector_map",
                        mock.Mock(side_effect=AssertionError("재생성 시도")))

    assert kis_client._get_sector_map() == {"005930": "전기전자"}
    assert cache["warned"] == []


# ── 노후 경고: 하루 한 번 ───────────────────────────────────

def test_stale_cache_warns_once_a_day(cache):
    age = kis_client.SECTOR_MAP_WARN_AGE_DAYS + 1
    built = date.today() - timedelta(days=age)
    _write(cache["path"], built)

    mapping = kis_client._get_sector_map()

    assert mapping == {"005930": "전기전자"}      # 맵은 그대로 쓴다
    assert len(cache["warned"]) == 1
    key, msg, kwargs = cache["warned"][0]
    assert key == kis_client.WARN_STALE_SECTOR_MAP
    assert f"{age}일째" in msg and str(built) in msg
    assert kwargs["window_seconds"] == 86400       # 하루 한 번


def test_exactly_at_limit_does_not_warn(cache):
    """경계값에서 울리지 않는다 - off-by-one 방지."""
    _write(cache["path"],
           date.today() - timedelta(days=kis_client.SECTOR_MAP_WARN_AGE_DAYS))
    kis_client._get_sector_map()
    assert cache["warned"] == []


# ── 캐시 없음·깨짐 ──────────────────────────────────────────

def test_missing_cache_returns_empty(cache):
    assert kis_client._get_sector_map() == {}
    assert cache["warned"] == []        # 경고는 로그로만 (카톡 아님)


def test_broken_cache_returns_empty(cache):
    cache["path"].write_text("{not json", encoding="utf-8")
    assert kis_client._get_sector_map() == {}


# ── 억제 창 파라미터 ────────────────────────────────────────

def test_throttle_window_parameter(monkeypatch):
    """window_seconds를 주면 그 간격으로 억제한다 (기본은 1시간 그대로)."""
    from datetime import datetime
    import notion_repo

    sent: list[str] = []
    monkeypatch.setattr(kis_client, "_notify_failure", lambda m: sent.append(m))
    monkeypatch.setattr(notion_repo, "create_order_record", lambda **kw: None)
    # 2시간 전에 같은 사유로 보냈다.
    two_hours_ago = datetime.now(kis_client.KST) - timedelta(hours=2)
    monkeypatch.setattr(notion_repo, "latest_warning_at", lambda key: two_hours_ago)

    # 기본 창(1시간)이면 2시간 전 건은 지났으므로 보낸다.
    kis_client._notify_warning_throttled("k", "기본")
    # 하루 창이면 아직 억제된다.
    kis_client._notify_warning_throttled("k", "하루", window_seconds=86400)

    assert sent == ["기본"]


# ── 야간 스캔 쪽 문구 분리 ──────────────────────────────────

def test_rebuild_failure_message_reports_fallback_success(tmp_path, monkeypatch, caplog):
    """폴백이 성공하면 '미분류'라고 적지 않는다.

    예전 문구는 재생성 실패 시 무조건 "'미분류'로 표시됩니다"였는데, 바로
    아래 폴백이 성공하면 미분류가 되지 않는다. 2026-10-06 로그를 읽은
    사람이 실제로는 9/28 맵으로 정상 동작 중인데 미분류로 오해했다.
    """
    path = tmp_path / "sector_map.json"
    _write(path, date.today() - timedelta(days=30), {"005930": "전기전자"})
    monkeypatch.setattr(screener, "SECTOR_PATH", path)
    monkeypatch.setattr(screener, "build_sector_map", lambda day: {})
    monkeypatch.setattr(screener, "SECTOR_MAX_AGE_DAYS", 0)   # 항상 재생성 시도

    with caplog.at_level("WARNING"):
        result = screener.load_sector_map(date.today().strftime("%Y%m%d"))

    assert result == {"005930": "전기전자"}
    assert "기존 업종 맵으로 진행합니다" in caplog.text
    assert "미분류" not in caplog.text          # 폴백 성공 → 미분류 아님


def test_rebuild_failure_without_cache_says_unclassified(tmp_path, monkeypatch, caplog):
    """폴백할 캐시도 없으면 그때는 '미분류'가 맞다."""
    monkeypatch.setattr(screener, "SECTOR_PATH", tmp_path / "none.json")
    monkeypatch.setattr(screener, "build_sector_map", lambda day: {})
    monkeypatch.setattr(screener, "SECTOR_MAX_AGE_DAYS", 0)

    with caplog.at_level("WARNING"):
        result = screener.load_sector_map(date.today().strftime("%Y%m%d"))

    assert result == {}
    assert "'미분류'로 표시됩니다" in caplog.text
