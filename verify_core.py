"""
삼성전기(009150) ATR·손절선 검증 스크립트
"""

from datetime import date
from core import (
    calc_atr,
    evaluate_holding,
    fetch_ohlcv,
    latest_close,
    HoldingInput,
)

TICKER = "009150"
TOTAL_CAPITAL = 100_000_000  # 1억 (예시)

print("=" * 60)
print(f"검증 대상: 삼성전기 ({TICKER})")
print(f"총자산(예시): {TOTAL_CAPITAL:,.0f}원")
print("=" * 60)

# 1) ATR 단독 계산
_df = fetch_ohlcv(TICKER)
atr = calc_atr(_df)
current_price = latest_close(_df)
print(f"\n[ATR 계산]")
print(f"  현재가(최근 종가): {current_price:,.0f}")
print(f"  ATR(20일, wilder): {atr:,.2f}")
print(f"  1유닛 주수: {int((TOTAL_CAPITAL * 0.01) / atr)} 주")

# 2) 신규 진입 시뮬레이션 (노션 기존값 없음)
inp_new = HoldingInput(
    ticker=TICKER,
    name="삼성전기",
    market="KOSPI",
    buy_price=130_000,     # 가상 매수단가
    shares=10,
    take_profit_1=180_000,
    take_profit_2=220_000,
    reeval_date=date(2026, 9, 30),
    prev_trailing_high=None,   # 신규: 기존값 없음
    prev_stop_loss=None,
)

result_new = evaluate_holding(inp_new, TOTAL_CAPITAL)
print(f"\n[신규 진입 시뮬레이션]")
print(f"  매수단가: 130,000")
print(f"  진입후 최고가: {result_new.trailing_high:,.0f}")
print(f"    = max(매수단가 130,000, 현재가 {current_price:,.0f})")
print(f"  손절선: {result_new.stop_loss:,.0f}")
print(f"    = (130,000 - 2×{atr:,.2f}) "
      f"+ ({result_new.trailing_high:,.0f} - 130,000)/2   [half 방식]")
print(f"  종목 리스크액: {result_new.risk_amount:,.0f}")
print(f"  현재 유닛수: {result_new.current_units}")
print(f"  판정: {result_new.verdict}")
print(f"  판정 메모: {result_new.verdict_memo}")

# 3) 기존 보유 시뮬레이션 (노션에 값이 있는 경우)
#    기존 손절선이 새 계산보다 높은 경우 → 기존값 유지 확인
high_prev_stop = result_new.stop_loss + 5000  # 기존 손절선을 일부러 높게

inp_existing = HoldingInput(
    ticker=TICKER,
    name="삼성전기",
    market="KOSPI",
    buy_price=130_000,
    shares=10,
    take_profit_1=180_000,
    take_profit_2=220_000,
    reeval_date=date(2026, 9, 30),
    prev_trailing_high=current_price - 1000,  # 약간 낮게
    prev_stop_loss=high_prev_stop,            # 높게 설정
)

result_exist = evaluate_holding(inp_existing, TOTAL_CAPITAL)
print(f"\n[트레일링 손절선 검증]")
print(f"  기존 손절선(노션): {high_prev_stop:,.0f}")
print(f"  새 계산 손절선: {result_new.stop_loss:,.0f}")
print(f"  최종 손절선: {result_exist.stop_loss:,.0f}")
print(f"  → 기존값 유지 여부: {'✅ 유지됨' if result_exist.stop_loss == high_prev_stop else '❌ 내려감!'}")

# 4) 손절 판정 시뮬레이션
inp_stop = HoldingInput(
    ticker=TICKER,
    name="삼성전기",
    market="KOSPI",
    buy_price=130_000,
    shares=10,
    take_profit_1=180_000,
    take_profit_2=220_000,
    reeval_date=date(2026, 9, 30),
    prev_trailing_high=current_price + 50_000,
    prev_stop_loss=current_price + 1_000,  # 손절선을 현재가 위로
)

result_stop = evaluate_holding(inp_stop, TOTAL_CAPITAL)
print(f"\n[손절 판정 검증]")
print(f"  현재가: {current_price:,.0f}")
print(f"  손절선: {result_stop.stop_loss:,.0f} (현재가 위)")
print(f"  판정: {result_stop.verdict}")
print(f"  → 손절 판정 여부: {'✅ 손절 판정됨' if result_stop.verdict == '손절' else '❌ 손절 안 됨!'}")

print("\n" + "=" * 60)
print("검증 완료")
print("=" * 60)
