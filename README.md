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

Gmail 앱 비밀번호는 [Google 계정 → 보안](https://myaccount.google.com/apppasswords)에서
2단계 인증을 켠 뒤 "앱 비밀번호"를 생성해 나오는 16자리를 `GMAIL_APP_PASSWORD`에 넣는다.
