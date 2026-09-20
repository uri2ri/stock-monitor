"""체결 내역 전용 읽기 진단 - 주문/취소/정정은 절대 호출하지 않는다.

`get_order_execution()`이 접수된 매도 주문을 "미체결"로 판정하는 원인을
가리기 위한 1회성 조회다. 확인하려는 것은 딱 두 가지다:

  1) 그 주문번호가 응답에 **있는가** (없으면 조회 파라미터·번호 표기 문제)
  2) 있다면 `tot_ccld_qty`·`avg_prvs`·`rmn_qty`가 **무엇인가**

`get_order_execution()`과 **똑같은 TR ID·URL·파라미터**를 쓴다 - 그래야
여기서 나온 결과가 실제 실행 경로에 대한 증거가 된다. 다른 점은 응답을
해석하지 않고 그대로 보여준다는 것뿐이다.

안전장치:
  - 주문 전송·정정·취소 함수를 import도 호출도 하지 않는다.
  - 노션에 읽지도 쓰지도 않는다 (NOTION_* 환경변수를 주지 않으면
    토큰 캐시 조회·저장이 fail-open으로 건너뛰어진다 - get_access_token
    docstring 참고). 카카오 발송도 자격증명이 없으면 일어나지 않는다.
  - 토큰·appkey·appsecret은 출력하지 않는다. 계좌번호는 마스킹한다.

사용:
    python tools/inspect_fill.py --order-no 0000006229 --date 2026-09-17
"""

from __future__ import annotations

import argparse
import json
import os
import sys

# `python tools/inspect_fill.py`로 부르면 sys.path[0]이 tools/라 저장소
# 루트의 kis_client를 못 찾는다. 루트를 명시적으로 얹는다.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests

import kis_client

# 응답에서 찍을 칸만 명시한다 - 화이트리스트라 예상 못 한 칸이 섞여
# 나와도 로그로 새지 않는다.
ROW_FIELDS = (
    "ord_dt",           # 주문일자
    "odno",             # 주문번호  ← 대조 대상
    "orgn_odno",        # 원주문번호 (정정·취소면 채워진다)
    "pdno",             # 종목코드
    "prdt_name",        # 종목명
    "sll_buy_dvsn_cd",  # 01=매도 / 02=매수
    "ord_qty",          # 주문수량
    "tot_ccld_qty",     # 총체결수량  ← 체결 판정 근거
    "rmn_qty",          # 잔여수량
    "avg_prvs",         # 평균체결가
    "cncl_yn",          # 취소여부
    "ccld_cndt_name",   # 체결조건
)

# 매매 경로(10초)보다 길게 잡는다 - 아래 _query() 주석 참고.
QUERY_TIMEOUT_SECONDS = 30


def _mask(account: str) -> str:
    cano, prdt = kis_client._parse_account(account)
    return f"{cano[:2]}{'*' * max(len(cano) - 2, 0)}-{prdt}"


def main() -> int:
    ap = argparse.ArgumentParser(description="KIS 체결 내역 읽기 전용 조회")
    ap.add_argument("--order-no", required=True,
                    help="대조할 주문번호 (노션 '주문번호' 칸 값 그대로)")
    ap.add_argument("--date", required=True,
                    help="주문일 YYYY-MM-DD (노션 '주문일시'의 날짜)")
    args = ap.parse_args()

    day = args.date.replace("-", "")
    if len(day) != 8 or not day.isdigit():
        print(f"날짜 형식 오류: {args.date} (YYYY-MM-DD)", file=sys.stderr)
        return 2

    account = os.environ["KIS_ACCOUNT"]
    cano, acnt_prdt_cd = kis_client._parse_account(account)
    print(f"[조회] 계좌 {_mask(account)} · 조회일 {day} "
          f"· 대조 주문번호 {args.order_no!r} (길이 {len(args.order_no)})")

    token = kis_client.get_access_token()

    # get_order_execution()과 동일한 헤더·파라미터. 다만 타임아웃은 길게
    # 잡고 재시도를 건다 - 2026-09-18 진단 3회가 전부 이 조회의 10초 읽기
    # 시간초과로 끝나 정작 보려던 응답을 한 번도 못 받았다. 진단은 매매
    # 경로가 아니라 사람이 기다리는 1회성 조회라, 실행 시간을 조금 더 쓰더라도
    # 답을 받아오는 쪽이 낫다(워크플로 제한 5분 안에 든다).
    def _query():
        return requests.get(
            f"{kis_client.BASE_URL}/uapi/domestic-stock/v1/trading/inquire-daily-ccld",
            headers={
                "content-type": "application/json; charset=utf-8",
                "authorization": f"Bearer {token}",
                "appkey": os.environ["KIS_APP_KEY"],
                "appsecret": os.environ["KIS_APP_SECRET"],
                "tr_id": kis_client.INQUIRE_CCLD_TR_ID,
            },
            params={
                "CANO": cano, "ACNT_PRDT_CD": acnt_prdt_cd,
                "INQR_STRT_DT": day, "INQR_END_DT": day,
                "SLL_BUY_DVSN_CD": "00", "INQR_DVSN": "00",
                "PDNO": "", "CCLD_DVSN": "00",
                "ORD_GNO_BRNO": "", "ODNO": "",
                "INQR_DVSN_3": "00", "INQR_DVSN_1": "",
                "CTX_AREA_FK100": "", "CTX_AREA_NK100": "",
            },
            timeout=QUERY_TIMEOUT_SECONDS,
        )

    try:
        resp = kis_client._read_with_retry("체결 내역 조회", _query)
    except Exception as e:                  # noqa: BLE001
        print(f"[조회 실패] {type(e).__name__}: {e}", file=sys.stderr)
        print("[조회 실패] KIS 응답 자체를 못 받았다 - 이 결과만으로는 "
              "주문번호 표기 문제인지 체결수량 응답 문제인지 가릴 수 없다.",
              file=sys.stderr)
        return 1
    print(f"[응답] HTTP {resp.status_code} · tr_cont={resp.headers.get('tr_cont', '')!r}")

    data = resp.json()
    print(f"[응답] rt_cd={data.get('rt_cd')!r} msg_cd={data.get('msg_cd')!r} "
          f"msg1={str(data.get('msg1', '')).strip()!r}")

    rows = data.get("output1")
    if not isinstance(rows, list):
        print(f"[응답] output1이 리스트가 아님: {type(rows).__name__}")
        return 1

    print(f"[응답] output1 {len(rows)}건\n")
    exact = padded = 0
    for i, row in enumerate(rows, 1):
        shown = {k: row.get(k) for k in ROW_FIELDS if k in row}
        print(f"  #{i} {json.dumps(shown, ensure_ascii=False)}")

        odno = str(row.get("odno") or "")
        if odno == args.order_no:
            exact += 1
            print("      → 주문번호 정확일치 (현재 코드가 잡는 행)")
        elif odno.strip().lstrip("0") and \
                odno.strip().lstrip("0") == args.order_no.strip().lstrip("0"):
            padded += 1
            print(f"      → 0 패딩만 다름: 응답 {odno!r} vs 노션 {args.order_no!r} "
                  f"(현재 코드는 이 행을 건너뛴다)")

    print(f"\n[대조] 정확일치 {exact}건 · 0패딩만 다른 일치 {padded}건")
    if exact == 0 and padded == 0:
        print("[대조] 이 주문번호는 응답에 없다 - 번호 표기가 아니라 "
              "조회 범위(날짜·구분) 또는 연속조회 쪽을 봐야 한다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
