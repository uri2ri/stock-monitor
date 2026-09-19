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
