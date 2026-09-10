# PROJECT STATUS

## 현재 구현 상태
- 종목 스캔 완료 (`scan_all.py`, `high20`/`high20_next` 둘 다 저장)
- 장중 돌파 감시 완료 (`intraday_watch.py`)
- KIS 모의투자 주문 연결 완료 (`kis_client.py`)
- 장중 자동매수 파이프라인 fail-closed 강화 완료 (구버전 CSV 신규매수 차단,
  후보 선정·판정 기준 통일, KIS 가격 재검증 fail-closed, 거래일 달력 기반
  스캔 신선도 검사) — PR #2 (draft), 브랜치 `claude/holdings-search-error-kfynqa`

## 최근 중요 결정
- `high20_next`(당일 포함 20일 고가)를 장중 진입 판정 기준으로 사용,
  없거나 무효(NaN·inf·0 이하)면 `high20`으로 대체하지 않고 보류
- 워치리스트 후보 선정도 `dist_atr`(당일 제외 기준) 대신 `dist_atr_next`로
  재계산 — `judge()`의 판정 기준과 후보 우선순위를 일치시킴
- KIS 주문 직전 최신가 재검증을 fail-open → fail-closed로 전환 (조회
  실패·무효 가격이면 신규매수 보류, 판정 시점 가격으로 되돌아가지 않음)
- 야간 스캔 신선도 검사를 주말만 건너뛰는 로컬 계산에서
  `screener.trading_days()`(KRX 거래일 달력) 기반으로 교체, 달력 조회
  실패 시 "평일이니 어제"로 추정하지 않고 보류
- `alerted`는 카카오 알림 여부만 의미
- 주문 여부는 `has_order_today()`로 별도 확인 (성공/주문중/실패=차단,
  거부=별도 스트라이크 캡, 조회실패=일시적 fail-closed)

## 현재 문제
- 보유종목 검색 오류 확인 필요

## 최근 정상 기준
- commit: `56c222d` (직전 정상 기준 `d85d75a` 위에 추가, 리셋 없음)
