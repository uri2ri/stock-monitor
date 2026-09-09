"""check_revalidation_data.py – PR #1 7년 재검증 로컬 실행 전 사전 점검.

`backtest.py --portfolio`를 실제로 돌리기 전에, 그 실행이 중간에 KRX
네트워크 조회로 멈추거나(업종 맵 캐시 미비) 아예 시작도 못 하는(OHLCV
캐시 없음) 상황을 미리 걸러내기 위한 읽기 전용 점검 스크립트다.

이 스크립트는:
  - 네트워크를 전혀 쓰지 않는다 (KRX·pykrx 호출 없음).
  - 아무 파일도 쓰지 않는다 (읽기 전용).
  - backtest.py를 실행하지 않는다 - run_portfolio_backtest()가 업종 맵을
    갱신하는 날짜만 그 함수와 동일한 규칙으로 미리 계산해서, 그 날짜에
    필요한 sector_map_YYYYMMDD.json 캐시가 로컬에 이미 있는지만 본다.

사용법:
    python check_revalidation_data.py --period full
    python check_revalidation_data.py --period dev --market KOSPI
    python check_revalidation_data.py --period holdout --kospi200 --regime-gate

종료 코드: 필수 파일이 하나라도 없으면 1, 전부 있으면(업종 캐시 포함) 0.
업종 캐시가 일부라도 없으면 "중단 권고"로 보고하되, 이는 backtest.py
실행 자체를 막지는 않는다(그 경우 backtest.py가 부족한 날짜마다 KRX
네트워크 호출로 자동 보충하려 시도한다 - 그게 바로 이 스크립트로
미리 피하려는 상황이다).
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

import pandas as pd

import backtest
import screener


def trading_days_in_range(start: date, end: date) -> list[date]:
    """backtest_ohlcv.parquet의 005930(삼성전자) 거래일을 달력으로 쓴다 -
    run_portfolio_backtest()가 calendar_ticker로 쓰는 것과 같은 종목이다.
    """
    if not backtest.BACKTEST_CACHE_PATH.exists():
        raise SystemExit(
            f"[필수 없음] {backtest.BACKTEST_CACHE_PATH} 가 없습니다 - "
            f"backtest_data.py로 먼저 만들어야 다른 점검도 의미가 있습니다."
        )
    full = pd.read_parquet(backtest.BACKTEST_CACHE_PATH, columns=["티커", "날짜"])
    sub = full.loc[full["티커"] == "005930", "날짜"]
    if sub.empty:
        raise SystemExit(
            "[필수 없음] backtest_ohlcv.parquet에 005930(삼성전자) 데이터가 없습니다 - "
            "거래일 달력을 만들 수 없습니다."
        )
    days = sorted(pd.to_datetime(sub, format="%Y%m%d").dt.date.unique())
    return [d for d in days if start <= d <= end]


def required_sector_dates(sim_days: list[date], refresh_days: int) -> list[date]:
    """run_portfolio_backtest() 2)번 블록과 동일한 규칙 - 첫 거래일 +
    이후 달력일 refresh_days(기본 backtest.SECTOR_REFRESH_DAYS=30)마다
    한 번씩 업종 맵을 갱신한다. 여기서는 그 갱신이 "언제" 일어나는지만
    계산한다(실제 갱신은 하지 않음)."""
    out: list[date] = []
    asof: date | None = None
    for d in sim_days:
        if asof is None or (d - asof).days >= refresh_days:
            out.append(d)
            asof = d
    return out


def sector_cache_path(day: date) -> Path:
    """screener.load_sector_map()이 이 날짜를 요청하면 실제로 읽을(혹은
    없으면 네트워크로 새로 만들) 경로. 판정 규칙은 load_sector_map()과
    동일 - 오늘 기준 최근(SECTOR_MAX_AGE_DAYS 이내)이면 공용 캐시,
    아니면 날짜 전용 캐시."""
    is_recent = (date.today() - day).days <= screener.SECTOR_MAX_AGE_DAYS
    if is_recent:
        return screener.SECTOR_PATH
    return screener.DATA_DIR / f"sector_map_{day.strftime('%Y%m%d')}.json"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--period", choices=("dev", "holdout", "full"), default="full",
                        help="점검할 구간 (backtest.py --period와 동일한 의미). 기본 full")
    parser.add_argument("--start", default=None, help="YYYYMMDD - 구간 안에서 더 좁힐 때만")
    parser.add_argument("--end", default=None, help="YYYYMMDD - 구간 안에서 더 좁힐 때만")
    parser.add_argument("--sector-refresh-days", type=int, default=backtest.SECTOR_REFRESH_DAYS,
                        help=f"업종 맵 갱신 주기 (기본 backtest.SECTOR_REFRESH_DAYS="
                             f"{backtest.SECTOR_REFRESH_DAYS})")
    parser.add_argument("--market", choices=("KOSPI", "KOSDAQ", "ALL"), default="ALL",
                        help="backtest.py --market과 동일 - ALL이 아니면 ticker_market.csv 필요")
    parser.add_argument("--kospi200", action="store_true",
                        help="backtest.py --kospi200과 동일 - kospi200_history.csv 필요")
    parser.add_argument("--regime-gate", action="store_true",
                        help="backtest.py --regime-gate와 동일 - index_regime_*.csv 필요")
    args = parser.parse_args()

    period_lo, period_hi = backtest.PERIOD_BOUNDS[args.period]
    from datetime import datetime as _dt
    start = _dt.strptime(args.start, "%Y%m%d").date() if args.start else period_lo
    end = _dt.strptime(args.end, "%Y%m%d").date() if args.end else period_hi
    if not (period_lo <= start <= end <= period_hi):
        raise SystemExit(f"--start/--end({start}~{end})가 --period {args.period} 범위 밖입니다.")

    print(f"=== 재검증 사전 점검 · period={args.period} ({start} ~ {end}) ===\n")

    missing_required: list[str] = []
    warnings: list[str] = []

    # 1) OHLCV 캐시 (필수) - 이 안에서 없으면 바로 SystemExit로 중단한다.
    sim_days = trading_days_in_range(start, end)
    print(f"[OK] {backtest.BACKTEST_CACHE_PATH.name} 존재, "
          f"구간 내 거래일 {len(sim_days)}일 확인")

    # 2) 업종 맵 캐시 (필수 - 없으면 실행 중 KRX 네트워크 호출로 멈출 수 있음)
    needed_dates = required_sector_dates(sim_days, args.sector_refresh_days)
    present, missing = [], []
    for d in needed_dates:
        (present if sector_cache_path(d).exists() else missing).append(d)
    print(f"\n[업종 맵] 이 구간 실행 중 총 {len(needed_dates)}회 갱신 필요 "
          f"(refresh_days={args.sector_refresh_days})")
    print(f"  캐시 있음: {len(present)}/{len(needed_dates)}")
    if missing:
        print(f"  캐시 없음: {len(missing)}개 - 이 날짜들에서 backtest.py가 KRX 네트워크 "
              f"호출을 시도합니다(과거 시점 1회당 약 60~70초, PR #1 본문 실측치):")
        for d in missing[:10]:
            print(f"    - {d.isoformat()} -> {sector_cache_path(d)}")
        if len(missing) > 10:
            print(f"    ... 외 {len(missing) - 10}개")
        missing_required.append(f"업종 맵 캐시 {len(missing)}개 없음")

    # 3) 시장 필터 (선택 - --market이 ALL이 아닐 때만 필수)
    if args.market != "ALL":
        if backtest.TICKER_MARKET_PATH.exists():
            print(f"\n[OK] {backtest.TICKER_MARKET_PATH.name} 존재 (--market {args.market} 사용)")
        else:
            print(f"\n[없음] {backtest.TICKER_MARKET_PATH.name} - --market {args.market}에 필요")
            missing_required.append(str(backtest.TICKER_MARKET_PATH))
    else:
        note = "선택" if not backtest.TICKER_MARKET_PATH.exists() else "존재(미사용)"
        print(f"\n[{note}] {backtest.TICKER_MARKET_PATH.name} - --market ALL(기본)이면 불필요")

    # 4) 코스피200 시점별 스냅샷 (선택 - --kospi200일 때만 필수)
    if args.kospi200:
        if backtest.KOSPI200_HISTORY_PATH.exists():
            print(f"[OK] {backtest.KOSPI200_HISTORY_PATH.name} 존재 (--kospi200 사용)")
        else:
            print(f"[없음] {backtest.KOSPI200_HISTORY_PATH.name} - --kospi200에 필요")
            missing_required.append(str(backtest.KOSPI200_HISTORY_PATH))
    elif backtest.KOSPI200_HISTORY_PATH.exists():
        print(f"[선택] {backtest.KOSPI200_HISTORY_PATH.name} 존재하지만 --kospi200 미사용 시 불필요")

    # 5) 국면 필터 (선택 - --regime-gate일 때만 필수)
    if args.regime_gate:
        for name, path in backtest.INDEX_REGIME_PATHS.items():
            if path.exists():
                print(f"[OK] {path.name} 존재 (--regime-gate 사용)")
            else:
                print(f"[없음] {path.name} - --regime-gate에 필요")
                missing_required.append(str(path))

    print()
    if missing_required:
        print("=== 결론: 중단 권고 ===")
        for m in missing_required:
            print(f"  - {m}")
        print("위 항목을 로컬에서 채운 뒤 다시 점검하세요. 업종 맵은 "
              "screener.load_sector_map(day, refresh=True)로 날짜별 채울 수 있으나 "
              "1회당 약 60~70초가 걸리므로 개수를 먼저 확인하고 진행하세요.")
        return 1

    print("=== 결론: 실행 가능 (필수 파일·업종 캐시 확인됨) ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
