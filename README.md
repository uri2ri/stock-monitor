# stock-monitor

터틀 트레이딩(ATR 2배 트레일링 스탑) 기반 포트폴리오 점검 도구.
노션 DB의 보유 종목을 매일 아침 점검해 손절선을 갱신하고,
카카오톡 요약과 메일 상세 리포트를 보낸다.

작업 이력·검증 결과·남은 작업은 [PROJECT_STATUS.md](PROJECT_STATUS.md),
매도 체결 확인 변경의 상세는 [SELL_CONFIRMATION.md](SELL_CONFIRMATION.md),
돌파 후 미진입 추적의 설정·스키마·동작은 [BREAKOUT_TRACKING.md](BREAKOUT_TRACKING.md)를
참고하세요.

## 구성

| 파일 | 역할 |
| --- | --- |
| `core.py` | ATR·손절선·판정·리스크 계산 (I/O 없음) |
| `notion_repo.py` | 노션 DB 읽기/쓰기 |
| `kakao.py` | 카카오톡 "나에게 보내기" (200자 요약) |
| `mailer.py` | Gmail SMTP 상세 리포트 메일 |
| `daily_report.py` | 매일 아침 실행 진입점 |
| `app.py` | Streamlit 웹앱 |
| `breakout_tracker.py` | 돌파 후 미진입 추적 (기록·갱신 규칙) |
| `breakout_view.py` | 추적 화면·아침 리포트 요약 (읽기 전용) |
| `breakout_track_update.py` | 추적 일별 갱신 진입점 (야간 스캔 뒤) |

## 실행

```bash
pip install -r requirements.txt
cp .env.example .env   # 값 채우기
python daily_report.py
```

GitHub Actions(`.github/workflows/daily.yml`)는 KST 평일 07:00에 자동 실행되며,
`.env`의 각 항목을 리포지토리 Secrets에 동일한 이름으로 등록해야 한다.

## 환경변수

| 이름 | 설명 |
| --- | --- |
| `NOTION_TOKEN` / `NOTION_DB_ID` | 노션 통합 토큰과 DB ID |
| `KAKAO_REST_API_KEY` / `KAKAO_CLIENT_SECRET` / `KAKAO_REFRESH_TOKEN` | 카카오 로그인 앱 정보 |
| `TOTAL_CAPITAL` | 총 운용자금 (원) |
| `GMAIL_ADDRESS` / `GMAIL_APP_PASSWORD` / `GMAIL_TO` | 메일 발송 계정·앱 비밀번호·수신자 (없으면 발송 생략) |
| `NOTION_BREAKOUT_TRACK_DB_ID` | 돌파 후 미진입 추적 DB. **비워두면 추적 기능 전체가 비활성**이고 워크플로는 실패하지 않는다 |
| `BREAKOUT_TRACK_WRITER` | 추적 행을 만들고 고치는 유일한 프로세스 표시. auto-trade 워크플로에만 `auto-trade`로 준다 |

추적 기능은 **관찰 전용**이다. 매수 규칙을 만들지 않고 주문을 내지 않으며,
진입·추가매수·청산 조건과 신규매수 상한에 관여하지 않는다. 자세한 건
[BREAKOUT_TRACKING.md](BREAKOUT_TRACKING.md)에 있다.

## 웹앱 시크릿 (app.py)

`.streamlit/secrets.toml.example`을 `secrets.toml`로 복사해 값을 채우세요. Community Cloud에서는
앱 설정 → Secrets에 같은 내용을 TOML 형식으로 넣습니다 — **리포지토리 GitHub Actions Secrets와는
별개의 저장소**이므로 두 곳 모두 등록해야 합니다.
`secrets.toml`은 커밋되지 않습니다. 조회·차트·계산은 비밀번호 없이 동작합니다.

- `APP_PASSWORD` — AI 의견 버튼 잠금용. 비워두면 AI 기능만 잠긴 채 나머지는 정상 동작.
- `GEMINI_API_KEY` — AI 의견 호출용 (Google AI Studio 발급).
- `DART_API_KEY` — AI 의견의 "사업 맥락"용 (opendart.fss.or.kr 발급). 비워두면 공시 조회만 건너뜀.
- `NOTION_TOKEN` / `NOTION_DB_ID` — 종목분석 페이지의 "■ 상관군 유닛" 카운터용 (`app.py`의
  `load_corr_units`). 값이 없거나 잘못되면 이 섹션만 비고 나머지 화면은 정상 동작한다.
  GitHub Actions Secrets에 등록된 값과 이름은 같지만 **Cloud Secrets에 별도로 넣어야 웹앱에 반영된다.**
- `NOTION_BREAKOUT_TRACK_DB_ID` — "돌파 후 미진입" 탭용. 없으면 그 탭만 비활성으로
  표시되고 나머지 화면은 정상 동작한다. 이 탭은 읽기 전용이다.

Gmail 앱 비밀번호는 [Google 계정 → 보안](https://myaccount.google.com/apppasswords)에서
2단계 인증을 켠 뒤 "앱 비밀번호"를 생성해 나오는 16자리를 `GMAIL_APP_PASSWORD`에 넣는다.

## 관측 outbox 처리 순서

`breakout_outbox.py`는 저널의 `prepare_cursor`와 `deliver_cursor`에 마지막으로
검사한 항목 ID를 저장하고 다음 회차에는 그 다음부터 순환한다. 기존 저널에
커서가 없거나 해당 항목이 완료되어 삭제됐으면 가장 오래된 항목부터 시작한다.
원본 이벤트·계획과 최초 관측값은 재정렬하거나 변경하지 않는다.

prepare는 회차당 최대 100개 이벤트, deliver는 최대 20개 계획과 60초 제한을
유지한다. 보류·동일 종목 순서 대기 항목도 검사 건수에 포함된다. 종목별로
앞선 미완료 이벤트/계획이 있으면 후속 항목은 처리하지 않으며, 다른 종목은
다음 회차에서 처리 기회를 받는다. 커서는 저널과 함께 저장되므로 재시작 후에도
이어진다. deliver는 외부 요청 전에 커서를 저장하고, prepare의 커서는 계획과
함께 저장한다. prepare 저장 이전 중단은 외부 쓰기 없이 이전 회차를 재시도한다.
워크플로의 기존 저널 push 절차는 그대로 필요하다.

생성 결과가 불명확한 계획은 조회로만 확인하고 POST를 자동 재전송하지 않는다.
이 변경은 조회 범위, 건수·시간 제한, 기존 생성 중복 방지 정책을 완화하지 않는다.

새 계획의 `queued` 상태는 아직 POST하지 않았음을 의미한다. 다음 워크플로의
owner가 달라도 처리할 수 있다. worker는 생성 직전에 `creating` 표시와 순환
위치를 저널에 저장하고, 해당 파일만 commit한 뒤 현재 브랜치에 일반 push한다.
push 성공 이후에만 POST한다. add/commit은 각각 15초, push는 30초로 제한하며
실패·시간 초과 시 POST하지 않는다. 기존 20건/60초 반복 검사와 워크플로 제한은
유지되며 진행 중인 단일 요청은 자체 timeout을 따른다.

생성 응답이나 최종 결과 push가 유실돼도 다음 실행은 원격의 `creating` 표시를
복원하므로 재생성하지 않는다. push 성공 여부 자체가 불명확하거나 표시 저장 후
POST 전에 중단된 경우도 보수적으로 조회·수동 확인 대상으로 남긴다. 원격에
`queued`가 남았다면 POST가 시작되지 않았으므로 이후 회차에서 처리 가능하다.
과거 형식의 다른 owner 소유 `prepared`는 미시도를 입증할 수 없어 자동 승계하지
않는다. 해당 과거 보류 항목은 별도 확인이 필요하다. force push/rebase나 운영
저널의 수동 상태 변경으로 이 보호를 우회하지 않는다.

회귀 검증: `python -m pytest tests/test_breakout_outbox.py -q`.
전체 검증: `python -m pytest -q`.
