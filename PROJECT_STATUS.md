# PROJECT STATUS

## 현재 구현 상태
- 종목 스캔 완료 (`scan_all.py`, `high20`/`high20_next` 둘 다 저장)
- 장중 돌파 감시 완료 (`intraday_watch.py`)
- KIS 모의투자 주문 연결 완료 (`kis_client.py`)
- 장중 자동매수 파이프라인 fail-closed 강화 완료 — PR #2 (draft),
  브랜치 `claude/holdings-search-error-kfynqa`
  1. 구버전 스캔 CSV의 신규매수 차단 (화면 표시는 계속, 자동매수만 보류)
  2. 후보 선정(워치리스트 순위)과 판정(`judge()`) 기준을 `high20_next`로 통일
  3. KIS 최신 가격 검증 실패 시 신규매수 보류 (fail-open → fail-closed)
  4. 휴장일을 반영한 스캔 신선도 검사 (거래일 달력 기반)
  5. 주문 상태별 재시도 구분·중복 방지 — 기존 설계가 이미 요건 충족 확인(코드 변경 없음)

## 최근 중요 결정
- `high20_next`(당일 포함 20일 고가)를 장중 진입 판정 기준으로 사용,
  없거나 무효(NaN·inf·0 이하)면 `high20`으로 대체하지 않고 보류
  (`_is_valid_threshold()`, `intraday_watch.py`)
- 워치리스트 후보 선정도 `dist_atr`(당일 제외 기준) 대신 `dist_atr_next`로
  재계산 — `judge()`의 판정 기준과 후보 우선순위를 일치시킴. 화면 표시용
  `high20`/`status`/`dist_atr`와 구버전 CSV 자체는 그대로 읽힘
- KIS 주문 직전 최신가 재검증을 fail-open → fail-closed로 전환 (조회
  실패·무효 가격이면 신규매수 보류, 판정 시점 가격으로 되돌아가지 않음).
  시장경보 필드 조회 실패의 기존 fail-open 정책은 별개로 유지
- 최종 현금 여력 확인에 `ORDER_COST_RATE`(0.25%, `backtest.py`의
  `COST_RATE`와 동일값) 포함 — 이 재검증에만 적용, 기존 게이트5는 미변경
- 야간 스캔 신선도 검사를 주말만 건너뛰는 로컬 계산에서
  `screener.trading_days()`(scan_all.py가 이미 쓰는 KRX 거래일 달력)
  기반으로 교체. 달력 조회 실패 시 "평일이니 어제"로 추정하지 않고
  "확인 불가"로 보고 신규 진입 보류(청산·추가매수는 계속 작동)
- `alerted`는 카카오 알림 여부만 의미
- 주문 여부는 `has_order_today()`로 별도 확인

### 주문 상태별 재시도 표
| 상황 | 노션 기록 상태 | `has_order_today` 차단 | 재시도 정책 |
|---|---|---|---|
| 전송 "전" 조회 실패 | (기록 없음) | 해당 없음 | fail-closed로 이번 회차만 보류, 다음 회차 재평가 |
| 명확한 거부(`rt_cd != "0"`) | 거부 | 아니오 | 재시도 허용, `MAX_REJECTIONS_PER_STOCK`(3회) 넘으면 중단 |
| 응답 시간초과 등 접수 여부 불명 | 실패 | 예 | 재전송 금지 |
| 접수·미체결/부분체결/완전체결 | 성공 | 예 | 중복 진입 금지 (세 경우를 "성공" 하나로 취급) |
| 사전기록 후 유령 행("주문중") | 주문중 | 예 | 자동 정리 안 함, 시작 시 경고만, 사람이 정리 |
| 취소 주문 | 해당 없음 | — | 취소 API 자체가 없어 해당 없음(범위 밖) |

### 달력 제공 방식 / 구버전 CSV 전환
- 제공자: `screener.trading_days(end, count)` (pykrx 기준 종목 시세 인덱스,
  scan_all.py 자체 스캔이 이미 사용 중). 매 호출 실시간 조회라 별도 캐시
  갱신 불필요, 실패 시 `KrxUnavailable`
- 구버전 CSV 전환: 별도 마이그레이션 불필요 — 야간 스캔(`scan_all.py`)을
  한 번 더 돌리면 `high20_next`가 채워진 새 `scan_latest.csv`로 자동 전환.
  재스캔 전까지 화면 표시는 계속되지만 신규매수만 보류

## 현재 문제
- 보유종목 검색 오류 확인 필요

## 테스트
- `python -m pytest tests/ -q` → **63 passed** (기존 20건 + 신규/갱신 43건)
- 핵심 회귀는 수정 전 실패 → 수정 후 통과를 직접 확인
  (`git stash push -- intraday_watch.py kis_client.py`로 되돌려 25건 실패 확인 후 복원)

## 남은 한계 / 후속 확인
- 최종 가격 재검증은 시장가 주문의 실제 체결가격을 보장하지 않음
  ("주문을 낼지 말지"만 확인)
- `ORDER_COST_RATE`는 `backtest.py` 근사치 재사용 — 실제 매수 수수료와
  정확히는 다를 수 있음(매수는 세금 없어 다소 과대추정, 안전한 방향)
- "취소된 주문" 상태는 취소 API가 없어 다루지 않음(범위 밖)
- 원격 환경에서 synthetic 데이터로만 검증 — 실 배포 전 실제
  `scan_latest.csv`(구버전·최신 각각)로 `load_watchlist()`/`judge()` 재확인,
  실 계좌 규모에서 비용 포함 현금 게이트가 기존 게이트5와 크게 어긋나지
  않는지 점검 권장
- `main` 병합·배포는 하지 않음

## 최근 정상 기준
- commit: `916becc` (이전 정상 기준 `d85d75a` → `56c222d` 위에 문서만 추가, 리셋 없음)
- PR: https://github.com/uri2ri/stock-monitor/pull/2 (draft, base `main`)
