"""build_ticker_stats.py – 종목별 돌파 이력을 집계해 저장한다.

원본 거래 상세(data/ticker_breakout_history.csv)는 backtest.py를
--portfolio --period dev --capital-mode fixed로, 자금 관련 게이트를
전부 무력화해서(--unit-caps 4/none/none --max-unit-ratio 999
--max-daily-entries 999999) 만든 산출물이다. 계좌 자금 개념을 완전히
빼고 "이 종목이 돌파했을 때 신호 자체가 어떻게 됐나"만 본다 - 즉 여러
종목이 계좌·상관군 캡을 놓고 경쟁하다 밀리는 일 없이, 조건을 만족하는
돌파는 전부 진입한다. 진입/청산 판정 로직(20일 고가 돌파 + 추격금지,
손절 2.0 ATR + 래칫, 추세청산 10일 저가, 피라미딩 0.5×ATR 최대 4유닛)은
실운용 규칙 그대로다. 이 CSV는 .gitignore 대상이라 재생성 가능한 로컬
파일로만 존재하고 스트림릿 클라우드엔 없다. 이 스크립트가 만드는 작은
집계 결과(data/ticker_stats.json)만 커밋해서, app.py는 그 JSON만 읽는다.

종목분석 화면(app.py)의 "백테스트 이력" 섹션 전용 참고 데이터다.
자동매매 판정(core.py·kis_client.py)은 이 파일의 존재 자체를 모른다.

※ 예전엔 계좌 하나를 전종목이 공유하는 포트폴리오 시뮬레이션 결과
(backtest_portfolio_dev_v2.csv 등)를 썼다. 그건 상관군 캡·자금 게이트
때문에 돌파했어도 다른 종목에 밀려 진입 자체가 안 된 사례가 많아서,
"이 종목이 돌파하면 어떻게 되는가"라는 질문에는 안 맞았다(예: 상관군캡
한 항목만 dev 구간에서 18,820건 제외). 이 스크립트는 이제 자금 게이트를
없앤 ticker_breakout_history.csv만 읽는다. 포트폴리오 시뮬레이션
결과(backtest_portfolio_dev_v2.csv)는 삭제하지 않았고 다른 분석에는
계속 쓸 수 있지만, 이 스크립트는 더 이상 그걸 읽지 않는다.

CLI:
    python build_ticker_stats.py
    python build_ticker_stats.py --trades data/ticker_breakout_history.csv \
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
DEFAULT_TRADES_PATH = DATA_DIR / "ticker_breakout_history.csv"
TICKER_STATS_PATH = DATA_DIR / "ticker_stats.json"

REQUIRED_COLUMNS = [
    "종목코드", "진입일", "청산일", "최종유닛수",
    "매수원가(수수료포함)", "총수량", "진입시ATR", "순손익", "R배수",
]


def aggregate(df: pd.DataFrame) -> dict[str, dict]:
    """거래 상세를 종목코드별로 집계한다.

    Returns:
        {종목코드: {돌파횟수, 승률, 평균R, 총손익, 유닛별, 평균보유일수}}
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

    stats: dict[str, dict] = {}
    for ticker, g in df.groupby("종목코드"):
        n = len(g)
        wins = int((g["순손익"] > 0).sum())
        unit_counts = {
            str(u): int((g["최종유닛수"] == u).sum()) for u in (1, 2, 3, 4)
        }
        stats[str(ticker)] = {
            "돌파횟수": n,
            "승률": round(wins / n * 100, 1) if n else 0.0,
            "평균R": round(float(g["R배수"].mean()), 2) if n else 0.0,
            "총손익": round(float(g["순손익"].sum()), 0),
            "유닛별_도달횟수": unit_counts,
            "평균보유일수": round(float(g["보유일수"].mean()), 1) if n else 0.0,
        }
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trades", type=Path, default=DEFAULT_TRADES_PATH,
                         help=f"돌파 이력 CSV (기본 {DEFAULT_TRADES_PATH})")
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
    logger.info("종목별 돌파 이력 집계 저장: %s (%d종목)", args.out, len(stats))
    return 0


if __name__ == "__main__":
    sys.exit(main())
