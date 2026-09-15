# PROJECT STATUS

## 현재 구현 상태
- 종목 스캔 완료 (`scan_all.py`, `high20`/`high20_next` 둘 다 저장)
- 장중 돌파 감시 완료 (`intraday_watch.py`)
- KIS 모의투자 주문 연결 완료 (`kis_client.py`)
- 장중 자동매수 파이프라인 fail-closed 강화 완료 — PR #2 **병합 완료**
  (`main`, 2026-09-10 08:28 UTC)
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

## 운영 상태 (중요)
- PR #2 병합 직전(2026-09-10 08:27 UTC) `MAX_ORDERS_PER_DAY`를 3 → **0**
  으로 내려 **신규 자동매수를 운영 보류 중**(커밋 `b3adc7f`). 병합 후
  변경 사항을 실거래에서 점검하기 위한 조치이며, `select_buy_candidates()`
  의 신규(ORDER_NEW) 전용 예산만 낮춘 것이라 추가매수(`MAX_PYRAMID_ORDERS_PER_DAY`)
  ·청산(`run_auto_sell`)에는 영향 없음. **재개 시 사람이 3으로 되돌려야 함**
  (자동 복구 아님) — 점검 결과를 보고 결정.
- 운영 보류 중 발견된 부작용 수정 완료(2026-09-11, PR #3 **병합 완료** —
  `main`, merge commit `ba27ff7c`): `MAX_ORDERS_PER_DAY=0`이어도
  `select_buy_candidates()`가 후보별 가격·유닛금액·상관군·현금 검사를
  그대로 거쳐 후보마다 거절 카톡을 반복 발송하던 문제. 이제 상한이 0이면
  계좌 조회·후보별 검사·거절 알림 없이 바로 빈 목록을 반환하고
  로그(`신규매수 운영 보류: 일일 상한 0`)만 남김. 상한이 정상값(예: 3)이고
  그날 소진돼 `remaining_slots == 0`이 된 경우는 대상이 아니며 기존
  후보별 검사·"우선순위 밀림" 알림 그대로 유지. 자동매도·추가매수·주문
  기록 기반 재시도 정책은 영향 없음(별도 캡·경로 사용). 상한값 자체는
  여전히 0 — 재개는 위 항목대로 사람이 결정.
- **병합 후 실거래 로그로 검증 완료(2026-09-11 02:1x UTC)**: 병합 커밋
  `ba27ff7c` 이후 첫 자동매매 실행(Actions run #984, #985 — head_sha
  `ba27ff7c`/`99c16ff`)의 로그를 읽기 전용으로 확인.
  1. head_sha가 병합 커밋 이후 — 병합된 코드로 실행됨 확인
  2. 로그에 `신규매수 운영 보류: 일일 상한 0` 및 바로 다음 줄
     `자동매수 후보 없음 (게이트 통과 0건, 후보 9건)` 정확히 출력됨
  3. 후보 9건에 대해 가격상한·현금부족 등 거절 카톡(`_notify_failure`)
     **0건** — 반복 거절 알림 중단 확인. 이번 회차 발송된 카톡 1통은
     신규 돌파(대양전기공업, "진입가능") 최초 알림이라 반복 거절과는 무관
  4. 보유종목 로드·계좌 조회·"재시도 대상 8종목(이전 회차 알림·아직
     미체결)" 로그로 청산·주문 기록 기반 재시도 정책이 평소대로 계속
     실행됨을 확인(이번 회차엔 매도 조건 충족 종목이 없어 청산 자체는
     발생하지 않음 — 정상적인 "해당 없음")
  - 워크플로 수동 실행·주문·알림 생성 없이 기존 정기 실행 로그만 조회
  - 확인용으로 걸어둔 1회성 예약(`trig_01H7KxKvW2MX1UZcEajsib2m`)은
    실행 완료 후 자동 종료

## 현재 문제
- 보유종목 검색 오류 확인 필요
- (위 운영 보류 참고) 신규 자동매수가 `MAX_ORDERS_PER_DAY=0`으로 막혀
  있는 동안은 정상 동작이 아니라 의도된 일시 정지 상태임
- 자동매도 체결 확인 문제(주문 접수="sent"만으로 청산·원장 확정) 수정
  완료 — 아래 "자동매도 체결 확인 수정" 섹션 참고
- 위 수정에 대한 추가 리뷰에서 나온 운영 차단 문제·미완료 주문 추적
  누락도 수정 완료 — 아래 "자동매도 체결 확인 추가 수정" 섹션 참고.
  **둘 다 `fix/auto-sell-recovery-review` 브랜치에만 있고 `main`은 물론
  `claude/holdings-search-error-kfynqa`에도 아직 없음** — 병합 전까지는
  실거래에 전혀 적용되지 않은 상태

## 테스트
- `python -m pytest tests/ -q` → **104 passed, 0 failed** (이 브랜치 HEAD
  기준. `main`은 아직 68건, `claude/holdings-search-error-kfynqa`는 아직
  84건 — 이 브랜치에서 추가된 20건은 전부 이번 추가 수정 검증용)
- `tests/test_auto_sell_fill_confirmation.py`는 이번에 36건으로 전면
  재작성됨(기존 16건 + 추가 수정 검증 20건) — "자동매도 체결 확인 수정"·
  "자동매도 체결 확인 추가 수정" 두 섹션 참고
- 신규 `tests/test_zero_cap_holds_new_buy.py` 5건은 모두 통과
  (상한 0 조기반환·로그, 상한 3 기존 흐름 유지, 추가매수·자동매도 무영향)
- `MAX_ORDERS_PER_DAY=3→0`(운영 보류) 전환 이후 이 값을 monkeypatch하지
  않아 실패하던 기존 6건(`test_multi_candidate_reservation.py` 2건,
  `test_order_state_retry_policy.py` 3건, `test_bug3_final_price_reverify.py`
  1건)은 모두 정상 신규매수 통과·재검증·현금 예약 동작을 검증하는
  테스트로 확인 — 검증 목적·assert는 그대로 두고 테스트 내부에서
  `MAX_ORDERS_PER_DAY`를 3으로 monkeypatch해 운영 보류 설정과 분리함
  (프로덕션 상한은 여전히 0, 변경 없음)

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
- `MAX_ORDERS_PER_DAY=0` 운영 보류 해제(3으로 복원) 여부는 실거래 점검
  결과를 보고 사람이 결정해야 함 (자동 복구 없음)

## 최근 정상 기준
- commit: `ba27ff7c` (`main`, PR #2 병합 `d2f29e1` + 운영 보류 `b3adc7f` +
  PR #3 병합 `ba27ff7c` 이후 최신)
- PR: https://github.com/uri2ri/stock-monitor/pull/2 (병합됨, `main`)
- PR: https://github.com/uri2ri/stock-monitor/pull/3 (병합됨, `main`,
  2026-09-11 02:01 UTC)

## PR #3 (병합 완료 — PR #2와는 별개 PR)
- 브랜치: `claude/holdings-search-error-kfynqa`, base `main`
- https://github.com/uri2ri/stock-monitor/pull/3 — **병합됨**
  (merge commit `ba27ff7c`, 2026-09-11 02:01 UTC, 병합 주체: `uri2ri`)
- `main`(당시 `cb14d02`) 대비 diff: 6 files, +200/-8
  - `kis_client.py`(최소 수정 8줄) — 신규매수 운영 보류(상한 0)에서 후보별
    거절 카톡 반복 발송 수정
  - `tests/test_zero_cap_holds_new_buy.py`(신규 5건)
  - `test_multi_candidate_reservation.py`·`test_order_state_retry_policy.py`·
    `test_bug3_final_price_reverify.py` 기존 6건에 `MAX_ORDERS_PER_DAY=3`
    monkeypatch 추가 — 운영 보류 설정(0) 의존 제거, 검증 목적 유지
  - `PROJECT_STATUS.md`(문서, 최종 테스트 결과 반영)
- 병합 시점 테스트: `python -m pytest tests/ -q` → 68 passed, 0 failed
  (커밋 `2780fd6` 기준)
- 병합 후 실거래 로그 검증 완료 — 위 "병합 후 실거래 로그로 검증 완료"
  항목 참고
- 프로덕션 상한은 `kis_client.py:108` `MAX_ORDERS_PER_DAY = 0` 그대로 유지
  (이 PR에서 변경하지 않음 — 재개 여부는 별도로 사람이 결정)
- **참고**: 위 기록과 달리 `main`의 `PROJECT_STATUS.md`엔 이 PR #3이 아직
  "Draft, 미병합"으로 남아 있었다(2026-09-14 확인, `main`은 이 파일을
  아직 이 내용으로 갱신받지 못한 상태 — PR #3 자체는 git 이력상
  `ba27ff7c`가 실제 병합 커밋으로 확인됨, 위 절 참고). 이 세션에서
  `PROJECT_STATUS.md`만 실제 상태에 맞게 고쳐 이 브랜치에 커밋했다
  (코드 변경 없음) — `main` 반영은 이 브랜치가 병합돼야 완료된다.

## 자동매도 체결 확인 수정 (이번 세션, `main` 미병합)

**원인** (ChatGPT가 `main` HEAD `06bebc9f`의 `kis_client.py`로 지적한
결함 — 이번 세션에서 최신 코드로 재확인, 실제 사고 발생 여부와는 별개):
`place_market_sell_order()`는 주문 API 응답(rt_cd=0)과 주문번호만 보고
`status="sent"`를 돌려주는데, 이건 "주문 접수"이지 "전량 체결"이
아니다(시장가도 미체결·부분체결로 남을 수 있다). 예전 `run_auto_sell()`은
"sent"만 보고 곧바로 `notion_repo.close_auto_holding()`으로 점검표를
청산 처리했고, 이어서 `_record_ledger_after_sell()`이 체결가를 조회는
하되 체결 수량을 확인하지 않아 조회 실패·미체결이어도 판정 시점 가격
(`ref_price`)으로 매매일지를 확정 기록할 수 있었다. 미체결·부분체결
상태인데도 노션에서 청산 처리돼 잔여 보유 감시가 빠지고 손익 기록이
틀릴 위험이 있었다. **실제 실행에서 이 결함이 발생한 증거는 없음** —
운영 확인 실행(https://github.com/uri2ri/stock-monitor/actions/runs/34805838340)
포함 지금까지의 실행 로그는 이 코드상 결함과는 무관하게 정상이었다.

**수정 파일**: `kis_client.py`, `notion_repo.py` (모두 최소 수정 —
전면 리팩터링·전략 변경 없음, 신규매수·추가매수 경로 무변경)
- `notion_repo.py`: 읽기 전용 함수 2개 추가
  - `fetch_latest_sell_order_today(ticker, day)` — 오늘 이 종목코드로
    상태="성공"(접수)인 매도 주문 중 최신 1건(주문번호·수량·사유)을
    "자동주문 기록" DB에서 다시 읽는다. GitHub Actions는 실행마다
    컨테이너가 새로 떠 로컬 변수가 안 남으므로, 이 기존 DB가 실행 간
    상태를 잇는 유일한 저장소다(새 저장 구조 추가 없음).
  - `ledger_exists_for_holding_today(page_id, day)` — 오늘 청산일로 이
    보유종목의 매매일지 청산 기록이 이미 있는지 확인한다(중복 생성 방지).
  - 둘 다 예외를 삼키지 않는다(fail-closed) — 호출자가 "확인 불가"로
    다뤄 확정을 보류한다.
- `kis_client.py`: `run_auto_sell()`의 확정 로직을 3개 함수로 재구성
  - `_check_sell_fill()` — "접수"와 "전량 체결"을 분리한다. 전량 체결
    (filled_qty ≥ 주문수량) + 유효 체결가(avg_price > 0)를 모두 확인해야만
    확정한다. 미체결·부분체결·조회 실패·체결가 미확인은 아무것도
    확정하지 않고 다음 실행에 넘긴다.
  - `_write_ledger_and_close()` — 매매일지 기록을 먼저 시도하고, 그게
    성공했을 때만 점검표를 청산 처리한다(순서 고정). 반대 순서였다면
    청산 처리 후 매매일지 기록이 실패할 때 다음 실행이 그 행을
    구분="보유" 필터로 더는 못 찾아 공백을 영영 못 채운다. 체결 수량·
    체결가는 실제 확인된 값을 쓴다(추정치 아님).
  - `_finalize_pending_sell()` — `run_auto_sell()` 루프에서 매도가능수량
    (`sellable`)이 0인 종목(이미 매도가 나가 있거나 체결까지 됐는데
    확정만 안 된 종목)을 처리한다. 예전엔 이 경우 그냥 `continue`해
    다음 실행에서도 영영 확정 기회가 없었다(중복 매도 방지 자체는 이
    잔고 값이 이미 하고 있어 변경 없음). 새 주문을 내지 않고
    `fetch_latest_sell_order_today`로 이전 주문을 이어서 확인만 한다.
  - `place_market_sell_order()`·`_sell_reason()`·중복 매도 방지(증권사
    잔고 기준)는 변경 없음.

**검증**: 외부 네트워크·KIS·카카오·이메일·노션 쓰기를 전부 monkeypatch로
차단한 `tests/test_auto_sell_fill_confirmation.py`(신규 16건)로 아래
7개 시나리오 확인. `python -m pytest tests/ -q` → **84 passed, 0 failed**
(기존 68건 전부 그대로 통과 — 신규매수 보류·추가매수 경로 무변경 확인).
  1. 접수 성공, 체결 0주 → 청산·원장 기록 없음
  2. 부분체결 후 다음 실행에서 전량 체결 → 새 주문 없이 추적, 매매일지는
     정확히 1번만 생성
  3. 체결 조회 실패·주문번호 확인 불가(접수 여부 불명) → 임의 청산·
     추정 손익 기록·중복 주문 없음
  4. 전량 체결 + 유효 체결가 → 올바른 수량·가격으로 청산 기록
  5. 매매일지 기록과 점검표 청산 처리 중 한쪽만 실패 → 다음 실행에서
     누락만 복구, 매매일지 중복 생성 없음(양방향 모두 확인)
  6. 미체결 종목은 매도가능수량이 계속 0으로 남아 다음 실행도 새 주문을
     내지 않음(프로세스 재시작 시 로컬 상태가 없다는 전제 — 모든 추적은
     노션 조회로만 이뤄짐을 코드 구조로 보장, 별도 프로세스 재시작 없이
     검증)
  7. 기존 신규매수(`place_market_buy_order`)·추가매수(`run_auto_pyramid`)
     경로가 이번에 추가한 매도 체결 확인 헬퍼를 전혀 안 부름을 확인

**운영 반영**: 안 됨. 이 브랜치에만 있고 `main`엔 병합되지 않았다.
`main` 직접 수정·병합·배포·워크플로 수동 실행·실주문·실메시지 발송·
노션 운영 데이터 수정 전부 하지 않았다(요청 범위 밖).

**남은 미확인 항목** (아래 "자동매도 체결 확인 추가 수정"에서 갱신됨 —
이 목록은 그 시점 기준으로 이제 낡았다. 취소선 항목은 추가 수정으로
해결됨, 나머지는 그대로 유효):
- 실제 KIS 모의투자 서버로 진짜 부분체결·미체결 응답을 받아본 검증은
  아님(mock 기반 단위/통합 테스트만) — 실 배포 전 실거래 로그로 재확인 권장
  (PR #3처럼 병합 후 실거래 로그 읽기 전용 확인 절차를 따를 것). **여전히
  유효** — 추가 수정도 mock 기반 검증만 했음(아래 섹션 참고)
- 같은 티커를 하루 안에 전량 청산 후 재진입하는 극단적 엣지 케이스
  (매도 주문 조회가 재진입분과 섞일 가능성)는 이번 수정 범위 밖 —
  기존에도 다루지 않던 경우라 새로 만든 문제는 아님. 추가 수정에서
  매수일(entry_date) 기준 조회 범위 제한으로 상당 부분 완화됐으나,
  매수일 자체를 못 구하면(조회 실패) 당일로만 좁혀 안전하게 후퇴함 —
  완전히 없앤 건 아님
- ~~`get_order_execution()`은 "미체결(0주)"과 "주문 자체를 못 찾음"을
  구분하지 않는다~~ → **해결됨**(아래 "자동매도 체결 확인 추가 수정" 4번,
  `get_order_execution()`이 이제 찾으면 항상 dict, 못 찾으면 None을 돌려줌)

## 자동매도 체결 확인 추가 수정 (2026-09-15, 위 수정에 대한 리뷰 후속 — `main` 미병합)

**배경**: 바로 위 "자동매도 체결 확인 수정"(접수/체결 분리)에 대한 추가
리뷰에서, 그 수정 자체가 운영에서 막히거나 미완료 주문을 놓칠 수 있는
문제 4건이 새로 드러났다. 브랜치를 분리했다 —
`fix/auto-sell-recovery-review`(base: 위 수정이 담긴 커밋 `e07d384`).
`claude/holdings-search-error-kfynqa`·`main` 어느 쪽도 아직 이 커밋을
포함하지 않는다.

**수정 파일**: `.github/workflows/auto-trade.yml`, `kis_client.py`,
`notion_repo.py` (신규매수·추가매수 경로, `MAX_ORDERS_PER_DAY=0`·
`MAX_PYRAMID_ORDERS_PER_DAY`는 미변경)

1. **필수 환경변수 누락** — `auto-trade.yml`의 "Watch for breakouts and
   auto-trade" 스텝에 `NOTION_LEDGER_DB_ID`가 빠져 있었다. 위 수정이
   추가한 `ledger_exists_for_holding()`/`create_ledger_record()`가 이
   변수를 요구하는데 없으면 `KeyError`로 실패한다(fail-closed라 매도
   자체·중복 방지는 안 깨지지만, 체결 확정이 실행마다 "확인 불가"로
   막혀 실제 체결된 매도가 노션에 영원히 "보유"로 남는다). 해당 스텝
   env에 `NOTION_LEDGER_DB_ID: ${{ secrets.NOTION_LEDGER_DB_ID }}` 추가.
   **이 시크릿이 실제로 GitHub 저장소에 등록돼 있는지는 확인하지
   못했다** — 이 세션엔 저장소 시크릿 이름을 조회할 도구/권한이 없다.
   `evening.yml`·`weekly.yml`이 이미 같은 시크릿을 참조 중이라는 간접
   신호는 있지만 그것만으로 등록 여부를 단정하지 않았다.

2. **날짜가 바뀌면 복구가 끊김** — `fetch_latest_sell_order_today()`·
   `ledger_exists_for_holding_today()`·`get_order_execution()`이 전부
   "오늘"에 고정돼 있어, 전날 낸 매도 주문은 다음 거래일에 조회 자체가
   안 됐다(`get_order_execution`은 `INQR_STRT_DT`=`INQR_END_DT`=오늘로만
   KIS를 조회했다 - 전날 주문번호는 오늘자 리포트에 없어 매번 못 찾음).
   - `notion_repo.fetch_pending_sell_orders(ticker, account_type, since)`
     로 교체 - "오늘"이 아니라 `since`(이 보유종목의 매수일)부터
     지금까지 전부 조회한다. 과거에 이미 청산된 별개 포지션의 매도
     기록과 섞이지 않도록 매수일 이전 기록은 애초에 제외한다.
   - `notion_repo.ledger_exists_for_holding(page_id)`로 교체 - 청산일
     날짜 필터를 없애고 "보유종목" relation만으로 판단한다. 실제
     체결일(전날)과 재처리일(오늘)이 달라도 중복 방지가 깨지지 않는다.
   - `kis_client.get_order_execution(access_token, order_no, order_date)`
     - 조회 기간을 `[order_date, 오늘]`로 확장. 주문일이 오늘보다
     이전인데 미체결/부분체결이면, 당일가 주문은 거래소가 장마감에
     남은 수량을 자동 취소한다는 규칙상 잔량이 이미 만료된 것으로
     확정한다(더 기다리지 않음 - 당일 주문이면 여전히 대기).
   - 매매일지 청산일(`exit_date`)도 재처리 회차의 날짜가 아니라 실제
     주문일을 쓰도록 호출부(`_write_ledger_and_close`)를 고쳤다 -
     안 그러면 지연일수·보유기간 계산이 다 틀어진다.

3. **미확정 주문 추적 순서** — `run_auto_sell()`이 매도가능수량이 0일
   때만 이전 주문을 확인하던 걸(`_finalize_pending_sell`), 수량과
   무관하게 항상 먼저 확인하도록 재구성했다(`_reconcile_pending_sell_orders`).
   예전엔 매도가능수량이 아직 양수면(부분체결 직후 등) 확인을 건너뛰고
   새 매도를 또 낼 수 있었다. "주문중"/"실패"(접수 여부 불명 - 응답
   시간초과·상태 기록 갱신 실패로 `order_no`가 비어 있을 수 있음)는
   "주문 없음"으로 취급하지 않고, 확인 전까지 신규 매도를 보류하며
   경고한다(종목별 억제 - `_sell_order_ambiguous_reason_key`). 여러 건이
   남아 있으면 전부 확인한다(최신 1건만 보고 나머지를 버리지 않음).
   개별 주문의 전량체결을 곧바로 포지션 전체 청산으로 보지 않는다 -
   증권사 잔고에 잔여 보유가 남아 있으면 그 주문 하나만 소화하고
   포지션은 열어 둔다. 부분체결 후 만료된 잔량은 기존 청산 판단
   (`_sell_reason`)으로 그대로 이어진다(무조건 재주문도, 영구 보류도
   아님). 계좌구분(`ACCOUNT_TYPE`)·매수일(`entry_date`)로 다른 계좌·
   과거 포지션과 구분한다.

4. **숫자 유효성** — `_is_valid_qty(value, allow_zero=)`를 추가해 수량이
   유효한 양(또는 0)의 정수인지 확인한다. 특히 수량 누락(`None`)을
   조용히 0으로 대체하지 않는다 - 그러면 `filled_qty >= requested_qty`
   비교에서 요청 수량이 없어도(`None`) 항상 참이 돼 전량체결로 오인될
   위험이 있었다. 체결가는 기존 `_is_valid_price()`(`math.isfinite(v)
   and v > 0`)를 재사용해 NaN·inf·0 이하를 모두 무효로 본다.
   `get_order_execution()`도 "0체결"(찾았지만 체결 없음, `dict` 반환)과
   "주문 자체를 못 찾음"(`None` 반환)을 이제 구분해서 돌려준다 - 못
   찾은 걸 "미체결로 만료"로 단정하면 실제로는 체결된 걸 조회 문제로
   놓치고 중복 매도로 이어질 위험이 있었다.

**검증**: `tests/test_auto_sell_fill_confirmation.py`를 전면 재작성
(기존 16건 + 신규 20건 = 36건). 핵심 조회 함수
(`fetch_pending_sell_orders`/`ledger_exists_for_holding`/
`get_order_execution`)까지 실제로 monkeypatch로 대체해 날짜·필터 문제가
다른 mock에 가려지지 않게 했다. `python -m pytest tests/ -q` →
**104 passed, 0 failed**(기존 84건 전부 그대로 통과). 요청받은 시나리오:
  1. 실제 워크플로(`auto-trade.yml`)에 `NOTION_LEDGER_DB_ID`가 전달되는지
     (파일 내용을 직접 파싱해 확인 - 실행 없이)
  2. 전날 전량 체결 후 원장 실패 → 다음 거래일 복구(청산일=실제 주문일)
  3. 전날 원장 성공·청산 처리 실패 → 다음 거래일 중복 없이 복구(매매일지
     재조회로 확인, 체결 재조회 자체를 안 함)
  4. 접수 후 상태 기록 실패("실패") 또는 응답 시간초과("주문중") → 신규
     매도 보류, "주문 없음"으로 취급하지 않음
  5. 미확정 주문이 있는데 sellable이 양수인 경우 → 먼저 확인 후 결론에
     따라 진행
  6. 같은 포지션에 복수 매도 주문이 있는 경우 → 전부 확인, 최신 것만
     보고 버리지 않음
  7. NaN·inf 가격 및 누락/무효 수량 → 전부 무효 처리, 확정하지 않음
  8. `run_auto_sell()`부터 복구까지 이어지는 통합 사례(같은 날 함수
     재호출이 아니라 `order_date`를 명시적으로 전날로 고정해 검증 -
     프로세스 재시작 자체를 재현하진 않았으나 "오늘"과 "전날"이 실제로
     다르게 취급되는지는 확인)

**운영 반영**: 안 됨. `fix/auto-sell-recovery-review` 브랜치에만 있고
`main`은 물론 `claude/holdings-search-error-kfynqa`에도 병합되지 않았다.
`main` 병합·배포·워크플로 수동 실행·실주문/모의주문·실메시지 발송·노션
운영 데이터 수정 전부 하지 않았다(요청 범위 밖).

**남은 미확인 항목**:
- `NOTION_LEDGER_DB_ID` 시크릿의 실제 등록 여부(위 1번) - 확인 불가,
  미확인으로만 보고
- 부분체결 후 만료된 주문의 체결분은 매매일지에 반영하지 않는다 - 이
  코드베이스의 매매일지가 "전량청산" 모델이라 부분청산 전용 기록 구조가
  없다. 확인된 체결분은 증권사 잔고엔 이미 반영돼 있고 잔여 보유도
  정상적으로 다음 매도 판단에 넘어가지만, 매매일지 P&L 통계엔 그
  부분체결이 남지 않는다 - 필요해지면 매매일지에 부분청산 전용 필드를
  추가하는 별도 작업이 필요함
- 날짜범위를 넓힌 `get_order_execution()` 조회가 실제 KIS
  `inquire-daily-ccld`에서도 기대한 대로(여러 날짜 범위 조회에 지정
  주문번호가 그 원래 날짜 그대로 나오는지) 동작하는지는 mock으로만
  가정했다 - 실 배포 전 실거래 로그로 재확인 권장(PR #3와 같은 절차)
