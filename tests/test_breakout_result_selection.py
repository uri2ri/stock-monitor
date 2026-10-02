"""스캔 결과 선택·검증 - 추적 저널을 스캔 결과로 착각하면 안 된다.

2026-10-02 [오늘의 돌파] 화면이 KeyError로 죽었다. screener.latest_result()가
`data/breakout_*.json`을 파일명 역순으로 읽는데, 추적 저널
`breakout_outbox.json`이 같은 glob에 걸리고 'o'(0x6f) > '2'(0x32)라
날짜 파일들보다 앞에 와서 저널이 "가장 최근 스캔 결과"로 반환됐다.
저널에는 scan_date가 없어 app._breakout_freshness()에서 터졌다.

저널은 올바른 JSON이다 - 그래서 "파싱되면 유효"로는 걸러지지 않는다.
여기서는 (1) 날짜 형식 파일만 고르는지, (2) 구조 검증이 실제로 거르는지,
(3) 거른 뒤 그다음 유효 결과로 내려가는지, (4) 0종목 정상 결과를 유효로
보는지를 검증한다.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest

import screener

# 실제 저널(data/breakout_outbox.json)의 최상위 구조. scan_date가 없다.
JOURNAL = {
    "version": 1,
    "events": [],
    "plans": [],
    "prepare_cursor": None,
    "deliver_cursor": None,
    "excluded_events": [],
}


def _stock(ticker="005930", name="삼성전자") -> dict:
    return {
        "ticker": ticker, "name": name, "sector": "전기전자",
        "price": 70000, "atr_pct": 1.4, "vol_mult": 2.1,
        "gap_atr": 0.3, "atr": 1000.0, "high_20_prev": 69700,
    }


def _result(scan_date: str, stocks: list[dict] | None = None, **over) -> dict:
    stocks = [_stock()] if stocks is None else stocks
    data = {
        "scan_date": scan_date,
        "total_scanned": 2873,
        "passed": len(stocks),
        "stocks": stocks,
        "source": "cache",
        "notes": [],
    }
    data.update(over)
    return data


def _write(data_dir: Path, name: str, payload) -> Path:
    path = data_dir / name
    if isinstance(payload, str):            # 깨진 JSON을 그대로 쓰고 싶을 때
        path.write_text(payload, encoding="utf-8")
    else:
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


@pytest.fixture
def data_dir(tmp_path, monkeypatch) -> Path:
    """screener가 읽을 폴더를 임시 폴더로 바꾼다 - 저장소 data/ 불변."""
    d = tmp_path / "data"
    d.mkdir()
    monkeypatch.setattr(screener, "DATA_DIR", d)
    return d


def _iso(days_ago: int) -> str:
    return (date.today() - timedelta(days=days_ago)).isoformat()


def _compact(iso: str) -> str:
    return iso.replace("-", "")


# ── 1. 저널이 섞여 있어도 정상 스캔을 고른다 ──────────────────

def test_journal_is_not_mistaken_for_scan_result(data_dir):
    newest = _iso(1)
    _write(data_dir, "breakout_outbox.json", JOURNAL)
    _write(data_dir, f"breakout_{_compact(newest)}.json", _result(newest))

    result = screener.latest_result()

    assert result is not None
    assert result["_file"] == f"breakout_{_compact(newest)}.json"
    assert result["scan_date"] == newest


def test_journal_alone_yields_no_result(data_dir):
    """저널만 있으면 "결과 없음"이다 - 저널을 반환하면 안 된다."""
    _write(data_dir, "breakout_outbox.json", JOURNAL)
    assert screener.latest_result() is None


def test_other_helper_files_are_skipped(data_dir):
    """날짜 형식이 아닌 이름은 전부 후보에서 빠진다."""
    valid = _iso(1)
    for name in ("breakout_outbox.json", "breakout_latest.json",
                 "breakout_tmp.json", "breakout_2026093.json",
                 "breakout_202609301.json"):
        _write(data_dir, name, JOURNAL)
    _write(data_dir, f"breakout_{_compact(valid)}.json", _result(valid))

    result = screener.latest_result()
    assert result["_file"] == f"breakout_{_compact(valid)}.json"


def test_journal_file_is_left_untouched(data_dir):
    """읽는 쪽만 고친다 - 저널을 지우거나 이름을 바꾸지 않는다."""
    journal = _write(data_dir, "breakout_outbox.json", JOURNAL)
    before = journal.read_bytes()
    valid = _iso(1)
    _write(data_dir, f"breakout_{_compact(valid)}.json", _result(valid))

    screener.latest_result()

    assert journal.exists()
    assert journal.read_bytes() == before


# ── 2. 최신 파일이 못 쓰면 그다음 유효 결과로 ──────────────────

def test_falls_back_when_newest_is_corrupt_json(data_dir, caplog):
    newest, older = _iso(1), _iso(2)
    _write(data_dir, f"breakout_{_compact(newest)}.json", "{not json")
    _write(data_dir, f"breakout_{_compact(older)}.json", _result(older))

    with caplog.at_level("WARNING"):
        result = screener.latest_result()

    assert result["scan_date"] == older
    assert _compact(newest) in caplog.text          # 왜 건너뛰었는지 남는다


@pytest.mark.parametrize("missing", ["scan_date", "total_scanned",
                                     "passed", "stocks"])
def test_falls_back_when_newest_misses_required_key(data_dir, missing, caplog):
    newest, older = _iso(1), _iso(2)
    broken = _result(newest)
    del broken[missing]
    _write(data_dir, f"breakout_{_compact(newest)}.json", broken)
    _write(data_dir, f"breakout_{_compact(older)}.json", _result(older))

    with caplog.at_level("WARNING"):
        result = screener.latest_result()

    assert result["scan_date"] == older
    assert missing in caplog.text


def test_falls_back_when_stocks_is_not_a_list(data_dir):
    newest, older = _iso(1), _iso(2)
    _write(data_dir, f"breakout_{_compact(newest)}.json",
           _result(newest, stocks=[]) | {"stocks": {"005930": {}}})
    _write(data_dir, f"breakout_{_compact(older)}.json", _result(older))

    assert screener.latest_result()["scan_date"] == older


def test_falls_back_when_a_stock_misses_required_key(data_dir):
    newest, older = _iso(1), _iso(2)
    thin = _stock()
    del thin["vol_mult"]
    _write(data_dir, f"breakout_{_compact(newest)}.json",
           _result(newest, stocks=[thin]))
    _write(data_dir, f"breakout_{_compact(older)}.json", _result(older))

    assert screener.latest_result()["scan_date"] == older


def test_none_when_every_candidate_is_unusable(data_dir):
    newest, older = _iso(1), _iso(2)
    _write(data_dir, "breakout_outbox.json", JOURNAL)
    _write(data_dir, f"breakout_{_compact(newest)}.json", "{not json")
    _write(data_dir, f"breakout_{_compact(older)}.json", {"stocks": []})

    assert screener.latest_result() is None


# ── 3. 0종목 정상 결과는 유효하다 ─────────────────────────────

def test_zero_passed_result_is_valid(data_dir):
    """돌파가 없었던 날 - 이걸 무효로 보면 어제 결과를 오늘로 보여준다."""
    today = _iso(0)
    empty = _result(today, stocks=[])
    assert empty["passed"] == 0
    _write(data_dir, f"breakout_{_compact(today)}.json", empty)

    result = screener.latest_result()
    assert result is not None
    assert result["stocks"] == []
    assert screener.result_problem(empty) is None


def test_zero_passed_newest_is_not_skipped_for_older_nonempty(data_dir):
    today, older = _iso(0), _iso(1)
    _write(data_dir, f"breakout_{_compact(today)}.json", _result(today, stocks=[]))
    _write(data_dir, f"breakout_{_compact(older)}.json", _result(older))

    assert screener.latest_result()["scan_date"] == today


# ── result_problem 단위 검증 ──────────────────────────────────

def test_result_problem_rejects_journal_and_wrong_types():
    assert "scan_date" in screener.result_problem(JOURNAL)
    assert "객체가 아닙니다" in screener.result_problem([])
    assert "객체가 아닙니다" in screener.result_problem("문자열")
    assert "날짜로 읽을 수 없습니다" in screener.result_problem(_result("어제"))
    assert "숫자가 아닙니다" in screener.result_problem(
        _result(_iso(1), total_scanned="2873"))
    # bool은 int의 하위 타입이라 숫자로 통과하면 안 된다.
    assert "숫자가 아닙니다" in screener.result_problem(
        _result(_iso(1), passed=True))
    assert "객체가 아닙니다" in screener.result_problem(
        _result(_iso(1), stocks=["005930"]))


def test_result_problem_accepts_real_saved_results():
    """저장소에 실제로 있는 결과 파일들이 이 검증을 통과해야 한다.

    통과하지 못하면 검증이 너무 엄격해서 멀쩡한 과거 결과를 버린다는
    뜻이다(화면에 "확인 불가"가 뜬다).
    """
    files = sorted(Path("data").glob("breakout_[0-9]*.json"))
    assert files, "검증할 실제 결과 파일이 없다"
    for path in files:
        data = json.loads(path.read_text(encoding="utf-8"))
        assert screener.result_problem(data) is None, path.name
