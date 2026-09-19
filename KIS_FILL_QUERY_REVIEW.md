# KIS 체결 조회 대조 (2026-09-18)

실제 계좌 API는 호출하지 않았다. 아래는 공개 공식 샘플과 로컬 코드의 정적 비교다.

| 항목 | 현재 코드/진단 | 공식 현행 샘플 |
| --- | --- | --- |
| 경로 | /uapi/domestic-stock/v1/trading/inquire-daily-ccld | 동일 |
| 최근 3개월 모의 TR | VTTC8001R | VTTC0081R |
| 최근 3개월 실전 TR | 본 앱은 모의 전용 | TTTC0081R |
| 날짜 | 주문일 시작=종료 | 시작/종료일 입력 |
| 매매/체결/조회3 | 00/00/00 | 전체/전체/전체 |
| 조회순서 | 00 | 역순 |
| 종목/주문/지점 | 빈 값 | 선택 입력 |
| 거래소 | 미전송 | EXCG_ID_DVSN_CD 기본 KRX |
| 연속조회 | F/M이면 안전 보류 | 컨텍스트 키와 N으로 후속 조회 |

공식 [현행 조회 샘플](https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/inquire_daily_ccld/inquire_daily_ccld.py)은 모의/실전과 기간별 TR을 구분한다. [legacy 샘플](https://github.com/koreainvestment/open-trading-api/blob/main/legacy/Sample01/kis_domstk.py)은 이전 TTTC8001R 계열을 사용한다. 따라서 TR 세대와 거래소 조건은 추가 검증 후보지만 GS의 빈 응답 원인이라고 확정할 수 없다. 이번 최소 안전 수정에서는 검증되지 않은 TR 교체를 운영 코드에 섞지 않았다.

모의계좌 조회 페이지 제한과 연속조회도 고려해야 한다. 현재 코드는 불완전 페이지를 근거로 확정하지 않도록 예외로 보류한다. 페이지 처리 개선 및 TR/거래소 조합의 실제 확인은 별도 승인된 읽기 진단 범위다. 과거일 조회 불가능이나 잔고 복구의 유일성은 입증되지 않았다.

잔고 0은 특정 주문 증빙이 아니다. 미확정 주문을 유지하고 주문번호·날짜에 해당하는 수량·평균가 증빙을 확보한 뒤 회계 경로를 재실행한다. GS 113,000원은 승인된 대체 기록이지 검증 체결가격이 아니며 본 작업에서 기존 데이터를 변경하지 않는다.
