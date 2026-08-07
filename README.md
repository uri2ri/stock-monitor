# stock-monitor

터틀 트레이딩(ATR 2배 트레일링 스탑) 기반 포트폴리오 점검 도구.
노션 DB의 보유 종목을 매일 아침 점검해 손절선을 갱신하고,
카카오톡 요약과 메일 상세 리포트를 보낸다.

## 구성

| 파일 | 역할 |
| --- | --- |
| `core.py` | ATR·손절선·판정·리스크 계산 (I/O 없음) |
| `notion_repo.py` | 노션 DB 읽기/쓰기 |
| `kakao.py` | 카카오톡 "나에게 보내기" (200자 요약) |
| `mailer.py` | Gmail SMTP 상세 리포트 메일 |
| `daily_report.py` | 매일 아침 실행 진입점 |
| `app.py` | Streamlit 웹앱 |

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

Gmail 앱 비밀번호는 [Google 계정 → 보안](https://myaccount.google.com/apppasswords)에서
2단계 인증을 켠 뒤 "앱 비밀번호"를 생성해 나오는 16자리를 `GMAIL_APP_PASSWORD`에 넣는다.
