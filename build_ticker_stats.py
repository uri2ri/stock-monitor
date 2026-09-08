"""
종목별 백테스트 통계 집계 스크립트

입력: data/backtest_portfolio_trades.csv (7년 fixed 모드 결과)
출력: data/ticker_stats.json

종목별 통계:
- 승률, 평균 R배수, 총손익
- 유닛별 도달 횟수 (1/2/3/4유닛)
- 평균 보유일수, 평균 진입시 ATR%
"""

from pathlib import Path
import pandas as pd
import json
from datetime import datetime

DATA_DIR = Path(__file__).resolve().parent / "data"

def build_ticker_stats():
    """종목별 통계 집계"""

    # CSV 로드
    csv_path = DATA_DIR / "backtest_portfolio_trades.csv"
    if not csv_path.exists():
        print(f"❌ 파일 없음: {csv_path}")
        return

    df = pd.read_csv(csv_path)
    df['진입일'] = pd.to_datetime(df['진입일'])
    df['청산일'] = pd.to_datetime(df['청산일'])

    print(f"✓ CSV 로드: {len(df)}건 거래")

    # 종목별 집계
    stats = {}

    for ticker in df['종목코드'].unique():
        ticker_data = df[df['종목코드'] == ticker]

        n = len(ticker_data)

        # 승률
        wins = len(ticker_data[ticker_data['순손익'] > 0])
        win_rate = wins / n * 100 if n > 0 else 0

        # 평균 R배수
        avg_r = ticker_data['R배수'].mean()

        # 총손익
        total_pnl = ticker_data['순손익'].sum()

        # 유닛별 도달 횟수
        units_dist = {}
        for u in [1, 2, 3, 4]:
            units_dist[str(u)] = int(len(ticker_data[ticker_data['최종유닛수'] == u]))

        # 평균 보유일수
        ticker_data['보유일'] = (ticker_data['청산일'] - ticker_data['진입일']).dt.days + 1
        avg_holding_days = ticker_data['보유일'].mean()

        # 평균 진입시 ATR%
        # ATR%는 진입시ATR / 진입가 * 100로 해석 (자산 대비 손절 규모)
        avg_atr_pct = ticker_data['진입시ATR'].mean() if '진입시ATR' in ticker_data.columns else 0

        # 진입가 추정 (매수원가 / 1유닛주수)
        ticker_data['진입가'] = ticker_data['매수원가(수수료포함)'] / ticker_data['1유닛주수']
        entry_prices = ticker_data['진입가'].values
        atrs = ticker_data['진입시ATR'].values

        # ATR을 진입가 대비 %로 계산
        atr_pcts = []
        for atr, entry_price in zip(atrs, entry_prices):
            if entry_price > 0:
                atr_pcts.append(atr / entry_price * 100)

        avg_atr_pct = sum(atr_pcts) / len(atr_pcts) if atr_pcts else 0

        stats[str(ticker)] = {
            "거래수": int(n),
            "승률": round(win_rate, 2),
            "평균R배수": round(avg_r, 2),
            "총손익": int(total_pnl),
            "유닛별_도달": units_dist,
            "평균_보유일": round(avg_holding_days, 1),
            "평균_진입시_ATR_pct": round(avg_atr_pct, 2),
        }

    # JSON 저장
    output_path = DATA_DIR / "ticker_stats.json"
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(stats, f, indent=2, ensure_ascii=False)

    print(f"✓ JSON 저장: {output_path}")
    print(f"✓ 종목 수: {len(stats)}개")

    return stats

def verify_stats(stats):
    """통계 검증"""
    if not stats:
        return

    print(f"\n【검증】")

    # 임의 종목 선택
    sample_ticker = list(stats.keys())[0]
    print(f"\n샘플 종목: {sample_ticker}")
    print(f"  {json.dumps(stats[sample_ticker], indent=2, ensure_ascii=False)}")

    # CSV에서 직접 확인
    csv_path = DATA_DIR / "backtest_portfolio_trades.csv"
    df = pd.read_csv(csv_path)
    df['진입일'] = pd.to_datetime(df['진입일'])
    df['청산일'] = pd.to_datetime(df['청산일'])

    sample_data = df[df['종목코드'] == int(sample_ticker)]

    print(f"\n원본 CSV 데이터 ({len(sample_data)}건):")
    print(f"  승률: {len(sample_data[sample_data['순손익'] > 0]) / len(sample_data) * 100:.2f}%")
    print(f"  평균R: {sample_data['R배수'].mean():.2f}")
    print(f"  총손익: {sample_data['순손익'].sum():,.0f}")
    print(f"  유닛 분포:")
    for u in [1, 2, 3, 4]:
        count = len(sample_data[sample_data['최종유닛수'] == u])
        print(f"    {u}유닛: {count}건")

    sample_data['보유일'] = (sample_data['청산일'] - sample_data['진입일']).dt.days + 1
    print(f"  평균 보유일: {sample_data['보유일'].mean():.1f}")

    print(f"\n✓ 검증 완료 (값이 일치하면 정상)")

def main():
    print("\n" + "="*80)
    print("【종목별 백테스트 통계 집계】")
    print("="*80 + "\n")

    stats = build_ticker_stats()
    verify_stats(stats)

    print("\n" + "="*80)

if __name__ == "__main__":
    main()
