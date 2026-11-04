"""진입일 지수 상태 태깅 - 미래 참조 없음 검증.

태그(index_state.state_asof)는 진입일(1유닛 체결일) **전날까지**의 지수
일봉만 써야 한다. 진입일 당일·이후의 지수 값을 아무리 바꿔도 태그가
그대로여야 하고, 기준일(idx_asof)은 항상 진입일보다 앞서야 한다.
백테스트 거래 로그(PortfolioTrade)에 붙는 태그도 같은 기준인지 합성
데이터로 run_portfolio_backtest를 끝까지 돌려 확인한다.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

import backtest
import index_state
import screener
from helpers import block_external_http


def _index_frame(closes: list[float], start: str = "2020-01-01",
                 spread: float = 10.0) -> pd.DataFrame:
    idx = pd.bdate_range(start, periods=len(closes))
    return pd.DataFrame({"시가": closes, "고가": [c + spread for c in closes],
                         "저가": [c - spread for c in closes], "종가": closes},
                        index=idx)


def _poison_from(frame: pd.DataFrame, day: date) -> pd.DataFrame:
    """day 당일·이후 행을 극단값으로 바꾼다 - 이걸 읽으면 태그가 바뀐다."""
    out = frame.copy()
    mask = out.index >= pd.Timestamp(day)
    out.loc[mask, ["시가", "고가", "저가", "종가"]] = [1e9, 1e9, 1.0, 1.0]
    return out


# ── 태그 계산 ───────────────────────────────────────────────

def test_tags_ignore_entry_day_and_later():
    frame = _index_frame([1000 + (i % 7) * 3 for i in range(120)])
    entry = frame.index[90].date()

    clean = index_state.state_asof(frame, entry, "KOSPI")
    poisoned = index_state.state_asof(_poison_from(frame, entry), entry, "KOSPI")

    assert clean == poisoned
    assert clean["idx_asof"] == frame.index[89].date() < entry
    assert all(clean[k] is not None for k in index_state.TAG_FIELDS)


def test_entry_day_value_alone_would_change_the_tag():
    """반대로 당일 값을 기준으로 삼았다면 태그가 달라졌어야 한다 - 위 테스트가
    우연히 통과하는 게 아님을 확인한다."""
    frame = _index_frame([1000.0] * 60 + [1000.0 + i for i in range(1, 31)])
    entry = frame.index[80].date()
    next_day = frame.index[81].date()

    today_view = index_state.state_asof(frame, next_day)      # entry 당일 포함
    asof_view = index_state.state_asof(_poison_from(frame, entry), entry)
    assert asof_view["idx_asof"] == frame.index[79].date()
    assert today_view["idx_asof"] == entry
    assert asof_view != {**today_view, "idx_market": None}


def test_turtle_states():
    flat = [1000.0] * 30
    up = index_state.state_asof(_index_frame(flat + [1100.0, 1000.0]),
                                pd.bdate_range("2020-01-01", periods=32)[31].date())
    assert up["idx_turtle"] == index_state.TURTLE_HIGH        # 전일 1100 > 직전 20일 고가 1010

    down = index_state.state_asof(_index_frame(flat + [900.0, 1000.0]),
                                  pd.bdate_range("2020-01-01", periods=32)[31].date())
    assert down["idx_turtle"] == index_state.TURTLE_LOW

    mid = index_state.state_asof(_index_frame(flat + [1005.0, 1000.0]),
                                 pd.bdate_range("2020-01-01", periods=32)[31].date())
    assert mid["idx_turtle"] == index_state.TURTLE_MID


def test_channel_sma_and_ret20_values():
    # 70일: 처음 50일 1000, 이후 20일 1010→1200 (10씩 상승)
    closes = [1000.0] * 50 + [1000.0 + 10 * i for i in range(1, 21)]
    frame = _index_frame(closes, spread=0.0)
    entry = (frame.index[-1] + pd.offsets.BDay(1)).date()
    tags = index_state.state_asof(frame, entry)

    assert tags["idx_asof"] == frame.index[-1].date()
    assert tags["idx_channel_pos"] == 1.0                     # 전일 종가 = 20일 최고가
    assert tags["idx_channel_bucket"] == "0.8~1.0"
    assert tags["idx_above_sma60"] == index_state.SMA_ABOVE
    assert tags["idx_ret20"] == pytest.approx(0.2, abs=1e-4)  # 1200 / 20거래일 전 1000 - 1
    assert tags["idx_ret20_bucket"] == "5% 이상"


@pytest.mark.parametrize("ret,bucket", [(-0.051, "-5% 미만"), (-0.05, "-5~0%"),
                                        (-0.0001, "-5~0%"), (0.0, "0~5%"),
                                        (0.0499, "0~5%"), (0.05, "5% 이상")])
def test_ret20_bucket_edges(ret, bucket):
    assert index_state.ret20_bucket(ret) == bucket


@pytest.mark.parametrize("pos,bucket", [(0.0, "0.0~0.2"), (0.1999, "0.0~0.2"),
                                        (0.2, "0.2~0.4"), (0.8, "0.8~1.0"),
                                        (1.0, "0.8~1.0")])
def test_channel_bucket_edges(pos, bucket):
    assert index_state.channel_bucket(pos) == bucket


def test_warmup_shortage_leaves_tags_empty():
    frame = _index_frame([1000.0] * 10)
    tags = index_state.state_asof(frame, (frame.index[-1] + pd.offsets.BDay(1)).date())
    assert tags["idx_turtle"] is None and tags["idx_above_sma60"] is None
    assert tags["idx_ret20"] is None and tags["idx_channel_pos"] is None
    assert index_state.state_asof(frame, frame.index[0].date())["idx_asof"] is None


def test_market_mapping():
    dfs = {"KOSPI": _index_frame([1000.0] * 70), "KOSDAQ": _index_frame([500.0] * 70)}
    entry = date(2020, 3, 20)
    tm = {"A": "KOSPI", "B": "KOSDAQ GLOBAL", "C": "KONEX"}
    assert index_state.tag_for(tm, dfs, "A", entry)["idx_market"] == "KOSPI"
    assert index_state.tag_for(tm, dfs, "B", entry)["idx_market"] == "KOSDAQ"
    assert index_state.tag_for(tm, dfs, "C", entry) == index_state.empty_tags(None)
    assert index_state.tag_for(tm, dfs, "Z", entry) == index_state.empty_tags(None)


def test_parse_naver_chart():
    text = ('<chartdata><item data="20190603|2031.03|2070.58|2029.17|2067.85|1" />'
            '<item data="20190604|2066.78|2072.19|2060.98|2066.97|2" /></chartdata>')
    frame = index_state.parse_naver_chart(text)
    assert list(frame["날짜"]) == [date(2019, 6, 3), date(2019, 6, 4)]
    assert frame.loc[0, "고가"] == 2070.58 and frame.loc[1, "종가"] == 2066.97


# ── 백테스트 거래 로그 ───────────────────────────────────────

TICKER = "005930"


def _stock_raw() -> pd.DataFrame:
    """돌파 → 진입 → 급락 손절이 한 번 나오는 합성 종목."""
    days = pd.bdate_range("2020-01-01", periods=110)
    rows = []
    for i, d in enumerate(days):
        if i < 60:
            c = 10_000.0 + (100.0 if i % 2 else -100.0)
        elif i < 66:
            c = 10_350.0                                       # 60일째 돌파
        else:
            c = 8_500.0                                        # 급락 → 손절
        rows.append({"날짜": d.strftime("%Y%m%d"), "티커": TICKER, "시가": c,
                     "고가": c + 150.0, "저가": c - 150.0, "종가": c,
                     "거래량": 300_000})
    return pd.DataFrame(rows)


@pytest.fixture
def synthetic_backtest(monkeypatch, tmp_path):
    block_external_http(monkeypatch)
    monkeypatch.setattr(backtest, "_FULL_RAW_DF", _stock_raw())
    monkeypatch.setattr(backtest, "_PERIOD_MAX", None)
    monkeypatch.setattr(screener, "load_sector_map", lambda day, refresh=False: {})
    tm_path = tmp_path / "ticker_market.csv"
    pd.DataFrame({"티커": [TICKER], "시장": ["KOSPI"]}).to_csv(tm_path, index=False)
    monkeypatch.setattr(backtest, "TICKER_MARKET_PATH", tm_path)

    def run(index_frame: pd.DataFrame):
        monkeypatch.setattr(index_state, "load_all_index_ohlcv",
                            lambda: {"KOSPI": index_frame, "KOSDAQ": index_frame})
        return backtest.run_portfolio_backtest(
            [TICKER], date(2020, 2, 3), date(2020, 5, 29),
            unlimited_cash=True, fixed_account_size=10_000_000)
    return run


def test_backtest_trade_tags_use_only_pre_entry_index(synthetic_backtest):
    index_frame = _index_frame([2000 + (i % 11) * 5 + i * 0.5 for i in range(200)],
                               start="2019-09-02")
    trades = synthetic_backtest(index_frame)["trades"]
    assert len(trades) == 1
    t = trades[0]
    assert t.idx_market == "KOSPI"
    assert t.idx_asof is not None and t.idx_asof < t.entry_date
    assert t.idx_turtle is not None and t.idx_ret20 is not None

    # 진입일·이후 지수를 망가뜨려도 태그는 그대로다.
    poisoned = synthetic_backtest(_poison_from(index_frame, t.entry_date))["trades"][0]
    for field in index_state.TAG_FIELDS:
        assert getattr(poisoned, field) == getattr(t, field), field
    # 직접 계산한 값과도 같다.
    expected = index_state.state_asof(index_frame, t.entry_date, "KOSPI")
    assert {f: getattr(t, f) for f in index_state.TAG_FIELDS} == expected


def test_backtest_unit1_r_matches_position_r_for_single_unit(synthetic_backtest):
    index_frame = _index_frame([2000.0] * 200, start="2019-09-02")
    t = synthetic_backtest(index_frame)["trades"][0]
    assert t.final_units == 1
    assert t.unit1_r == pytest.approx(t.r_multiple)
    assert t.r_multiple < 0                                    # 손절


def test_breakdown_table_shapes():
    def trade(r_pos, r_unit1, turtle, d):
        t = backtest.PortfolioTrade(
            ticker="X", entry_date=d, exit_date=d, final_units=1, total_shares=1,
            total_invested=100.0, proceeds=100.0 + r_pos * 2, entry_atr=1,
            unit_shares=1, exit_reason="", unit1_invested=100.0,
            unit1_proceeds=100.0 + r_unit1 * 2, idx_turtle=turtle)
        return t
    trades = [trade(6, 1, index_state.TURTLE_HIGH, date(2020, 1, 1)),
              trade(-1, -1, index_state.TURTLE_HIGH, date(2020, 1, 2)),
              trade(-1, -1, index_state.TURTLE_MID, date(2020, 1, 3)),
              trade(-1, -1, index_state.TURTLE_MID, date(2020, 1, 4))]
    table = backtest.index_state_breakdown(trades)
    pos_high = table[(table["단위"] == "포지션(피라미딩 포함)")
                     & (table["구간"] == index_state.TURTLE_HIGH)].iloc[0]
    assert pos_high["거래수"] == 2 and pos_high["5R이상"] == 1
    assert pos_high["PF"] == pytest.approx(6.0)
    unit_high = table[(table["단위"] == "1유닛 진입")
                      & (table["구간"] == index_state.TURTLE_HIGH)].iloc[0]
    assert unit_high["최대R"] == pytest.approx(1.0)
    mid = table[(table["단위"] == "포지션(피라미딩 포함)")
                & (table["구간"] == index_state.TURTLE_MID)].iloc[0]
    assert mid["최대연속손실"] == 2 and mid["승률%"] == 0
    total = table[(table["단위"] == "포지션(피라미딩 포함)") & (table["구간"] == "전체")].iloc[0]
    assert total["상위3제외평균R"] == pytest.approx(-1.0)
