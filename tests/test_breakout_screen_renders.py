"""[오늘의 돌파] 화면이 어떤 데이터 상태에서도 터지지 않는다.

2026-10-02 증상 그대로를 재현한다: data/에 추적 저널 breakout_outbox.json과
날짜별 스캔 결과가 함께 있는 상태. 수정 전에는 저널이 "최신 스캔 결과"로
반환돼 app._breakout_freshness()에서 `KeyError: 'scan_date'`가 났다.

화면을 실제로 그려서 본다(streamlit.testing.v1.AppTest) - latest_result()
단위 테스트만으로는 "화면이 안 죽는다"를 증명하지 못한다. 하네스는
tests/harness_breakout_screen.py이고, 읽을 폴더는 환경변수로 받는다.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

HARNESS = str(Path(__file__).resolve().parent / "harness_breakout_screen.py")

JOURNAL = {
    "version": 1, "events": [], "plans": [],
    "prepare_cursor": None, "deliver_cursor": None, "excluded_events": [],
}


def _stock(ticker="005930", name="삼성전자") -> dict:
    return {
        "ticker": ticker, "name": name, "sector": "전기전자",
        "price": 70000, "atr_pct": 1.4, "vol_mult": 2.1,
        "gap_atr": 0.3, "atr": 1000.0, "high_20_prev": 69700,
        "unit_shares": 10, "stop_loss": 68000, "exit_level": 67000,
    }


def _result(scan_date: str, stocks=None, **over) -> dict:
    stocks = [_stock()] if stocks is None else stocks
    data = {
        "scan_date": scan_date, "total_scanned": 2873, "passed": len(stocks),
        "stocks": stocks, "source": "cache", "notes": [],
    }
    data.update(over)
    return data


def _iso(days_ago: int) -> str:
    return (date.today() - timedelta(days=days_ago)).isoformat()


def _write(d: Path, name: str, payload) -> None:
    text = payload if isinstance(payload, str) else json.dumps(
        payload, ensure_ascii=False)
    (d / name).write_text(text, encoding="utf-8")


@pytest.fixture
def screen(tmp_path, monkeypatch):
    """하네스가 읽을 임시 data/ 폴더를 만들고 AppTest 실행기를 돌려준다."""
    d = tmp_path / "data"
    d.mkdir()
    monkeypatch.setenv("BREAKOUT_TEST_DATA_DIR", str(d))

    def run():
        return AppTest.from_file(HARNESS, default_timeout=60).run()

    return d, run


def _texts(at) -> str:
    """화면에 뜬 안내·경고·제목을 한 덩어리로 모은다."""
    parts = []
    for block in (at.info, at.warning, at.error, at.subheader,
                  at.caption, at.markdown):
        parts += [str(e.value) for e in block]
    return "\n".join(parts)


def test_journal_present_renders_real_scan(screen):
    """저널이 같이 있어도 KeyError 없이 정상 스캔을 보여준다."""
    d, run = screen
    today = _iso(0)
    _write(d, "breakout_outbox.json", JOURNAL)
    _write(d, f"breakout_{today.replace('-', '')}.json", _result(today))

    at = run()

    assert not at.exception
    assert today in _texts(at)
    assert "스캔 결과 확인 불가" not in _texts(at)


def test_falls_back_to_previous_valid_scan(screen):
    """최신 파일이 깨졌으면 이전 유효 결과를 보여준다."""
    d, run = screen
    newest, older = _iso(1), _iso(2)
    _write(d, "breakout_outbox.json", JOURNAL)
    _write(d, f"breakout_{newest.replace('-', '')}.json", "{not json")
    _write(d, f"breakout_{older.replace('-', '')}.json", _result(older))

    at = run()

    assert not at.exception
    assert older in _texts(at)
    assert newest not in _texts(at)


def test_journal_only_shows_guidance_without_error(screen):
    """저널만 있으면 안내만 뜬다 - 오류도, 꾸며낸 숫자도 없다."""
    d, run = screen
    _write(d, "breakout_outbox.json", JOURNAL)

    at = run()

    assert not at.exception
    text = _texts(at)
    assert "스캔 결과 확인 불가" in text
    # 누락 데이터를 오늘 날짜·0종목으로 꾸며 보여주지 않는다.
    assert date.today().isoformat() not in text
    assert "0종목" not in text
    assert not at.metric


def test_no_files_at_all_shows_guidance(screen):
    d, run = screen
    at = run()
    assert not at.exception
    assert "스캔 결과 확인 불가" in _texts(at)
    assert not at.metric


def test_zero_passed_scan_is_shown_as_valid(screen):
    """조건 통과 0종목은 정상 결과다 - "확인 불가"로 숨기지 않는다."""
    d, run = screen
    today = _iso(0)
    _write(d, "breakout_outbox.json", JOURNAL)
    _write(d, f"breakout_{today.replace('-', '')}.json",
           _result(today, stocks=[]))

    at = run()

    assert not at.exception
    text = _texts(at)
    assert "오늘 조건을 통과한 종목이 없습니다" in text
    assert "스캔 결과 확인 불가" not in text
    # 집계는 실제 저장값으로 보여준다(스캔 2,873종목 · 통과 0).
    assert any("2,873" in str(m.value) for m in at.metric)


def test_missing_required_key_does_not_crash_screen(screen):
    """필수 칸이 빠진 결과만 있으면 안내하고 멈춘다(KeyError 아님)."""
    d, run = screen
    today = _iso(0)
    broken = _result(today)
    del broken["total_scanned"]
    _write(d, f"breakout_{today.replace('-', '')}.json", broken)

    at = run()

    assert not at.exception
    assert "스캔 결과 확인 불가" in _texts(at)
