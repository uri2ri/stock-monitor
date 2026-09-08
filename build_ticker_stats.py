"""build_ticker_stats.py – 백테스트 결과를 종목별로 집계해 저장한다.

원본 거래 상세(data/backtest_portfolio_trades.csv, backtest.py의 3단계
포트폴리오 시뮬레이션 산출물)는 .gitignore 대상이라 재생성 가능한
로컬 파일로만 존재하고 스트림릿 클라우드엔 없다. 이 스크립트가 만드는
작은 집계 결과(data/ticker_stats.json)만 커밋해서, app.py는 그 JSON만
읽는다 - CSV 자체를 배포 환경에 올릴 필요가 없다.

종목분석 화면(app.py)의 "백테스트 이력" 섹션 전용 참고 데이터다.
자동매매 판정(core.py·kis_client.py)은 이 파일의 존재 자체를 모른다.

[집계 범위 – 1차분]
아래 항목은 backtest_portfolio_trades.csv 컬럼만으로 바로 나온다:
    거래수, 승률, 평균R, 총손익, 유닛별(1~4유닛) 도달 횟수,
    평균 보유일수, 평균 ATR%(진입시ATR ÷ 평균매수단가)

아래 두 항목은 이번엔 뺐다 - 이 CSV엔 실제 진입까지 간 거래만 남고
거부된 신호는 포트폴리오 전체 집계로만 있어 종목별로 못 살린다.
가짜 돌파 비율은 자본 무제한 단일진입 비교모드(backtest_compare_trades.csv)
에만 있어 조건이 달라 그냥 섞으면 안 된다(backtest.py에 종목별 로깅을
추가하고 재실행해야 나온다):
    돌파 신호 발생 횟수(진입 안 된 것 포함), 가짜 돌파 비율

CLI:
    python build_ticker_stats.py
    python build_ticker_stats.py --trades data/backtest_portfolio_trades.csv \
        --out data/ticker_stats.json
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent / "data"
DEFAULT_TRADES_PATH = DATA_DIR / "backtest_portfolio_trades.csv"
TICKER_STATS_PATH = DATA_DIR / "ticker_stats.json"

REQUIRED_COLUMNS = [
    "종목코드", "진입일", "청산일", "최종유닛수",
    "매수원가(수수료포함)", "총수량", "진입시ATR", "순손익", "R배수",
]


def aggregate(df: pd.DataFrame) -> dict[str, dict]:
    """거래 상세를 종목코드별로 집계한다.

    Returns:
        {종목코드: {거래수, 승률, 평균R, 총손익, 유닛별, 평균보유일수, 평균ATR퍼센트}}
    """
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"거래 CSV에 필요한 컬럼이 없습니다: {missing}")

    df = df.copy()
    # 종목코드는 6자리 zero-padded 문자열이다 - dtype 지정 없이 읽으면
    # pandas가 순수 숫자로 보고 앞자리 0을 날린다("000660" -> 660).
    df["종목코드"] = df["종목코드"].astype(str).str.zfill(6)
    df["진입일"] = pd.to_datetime(df["진입일"])
    df["청산일"] = pd.to_datetime(df["청산일"])
    df["보유일수"] = (df["청산일"] - df["진입일"]).dt.days
    # 진입가는 CSV에 직접 없다 - 매수원가(수수료 포함)를 총수량으로 나눈
    # 평균 매수단가로 근사한다 (ATR%는 참고용 표시라 이 정도 근사면 충분).
    df["평균매수단가"] = df["매수원가(수수료포함)"] / df["총수량"].replace(0, pd.NA)
    df["ATR퍼센트"] = df["진입시ATR"] / df["평균매수단가"] * 100

    stats: dict[str, dict] = {}
    for ticker, g in df.groupby("종목코드"):
        n = len(g)
        wins = int((g["순손익"] > 0).sum())
        unit_counts = {
            str(u): int((g["최종유닛수"] == u).sum()) for u in (1, 2, 3, 4)
        }
        stats[str(ticker)] = {
            "거래수": n,
            "승률": round(wins / n * 100, 1) if n else 0.0,
            "평균R": round(float(g["R배수"].mean()), 2) if n else 0.0,
            "총손익": round(float(g["순손익"].sum()), 0),
            "유닛별_도달횟수": unit_counts,
            "평균보유일수": round(float(g["보유일수"].mean()), 1) if n else 0.0,
            "평균ATR퍼센트": (
                round(float(g["ATR퍼센트"].dropna().mean()), 2)
                if g["ATR퍼센트"].notna().any() else None
            ),
        }
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trades", type=Path, default=DEFAULT_TRADES_PATH,
                         help=f"백테스트 거래 상세 CSV (기본 {DEFAULT_TRADES_PATH})")
    parser.add_argument("--out", type=Path, default=TICKER_STATS_PATH,
                         help=f"집계 결과 저장 경로 (기본 {TICKER_STATS_PATH})")
    args = parser.parse_args()

    if not args.trades.exists():
        logger.error("거래 CSV가 없습니다: %s (backtest.py로 먼저 생성하세요)", args.trades)
        return 1

    df = pd.read_csv(args.trades, dtype={"종목코드": str})
    if df.empty:
        logger.warning("거래 CSV가 비어 있습니다 - 빈 집계 결과를 저장합니다: %s", args.trades)
        stats = {}
    else:
        stats = aggregate(df)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(stats, ensure_ascii=False, indent=1), encoding="utf-8",
    )
    logger.info("종목별 백테스트 집계 저장: %s (%d종목)", args.out, len(stats))
    return 0


if __name__ == "__main__":
    sys.exit(main())
