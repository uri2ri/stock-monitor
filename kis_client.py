"""
kis_client.py - 한국투자증권(KIS) Open API 연동

1단계: 접근토큰 발급 -> 현재가 조회.
2단계: 모의투자 시장가 매수 주문 1건 전송 -> 주문번호 확인 -> 체결 여부 조회.
3단계: 자금 게이트 - 후보를 실제로 주문 넣기 전에 "이 계좌 규모로 감당되는가"를
       거른다. 신호 판정(20일 고가·갭·손절선)은 core.py 소관이라 여기서는
       core.calc_position()·core.MAX_UNITS* 상수만 가져다 쓰고 다시 만들지 않는다.
매도 주문은 다루지 않는다 (매도는 증권사 자동감시주문이 담당).

같은 날 같은 종목 중복 주문 방지, 일일 주문 건수 상한은 노션 "자동주문 기록" DB를
조회해서 판단한다 (NOTION_ORDERS_DB_ID). GitHub Actions는 실행마다 컨테이너가 새로
뜨고 사라져 로컬 파일이나 메모리 카운터로는 실행 간 상태를 남길 수 없다.

fail-closed: 이 조회가 실패하면(네트워크·인증 만료 등) "0건"으로 간주하고 진행하지
않는다. 상태를 모르는 채로 주문하는 것보다 안 사는 게 낫다 - 조회 실패는 그 자체로
주문 중단 사유이고 카톡으로 알린다.

접근토큰은 이번 단계에서 캐싱하지 않는다 (저장소가 아직 public). 실행마다 새로
발급받고, 발급 실패·거부도 조용히 넘어가지 않고 카톡으로 알린다.

로컬 테스트: python kis_client.py
필요한 .env 값: KIS_APP_KEY, KIS_APP_SECRET, KIS_ACCOUNT, NOTION_TOKEN,
NOTION_ORDERS_DB_ID (모의투자)
"""

from __future__ import annotations

import logging
import os
import time
from datetime import date, datetime
from typing import Optional
from zoneinfo import ZoneInfo

import requests

import core
import kakao
import notion_repo

# .env 지원 (로컬 테스트용, 없으면 무시)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logger = logging.getLogger(__name__)

# 모의투자 서버
BASE_URL = "https://openapivts.koreainvestment.com:29443"

# 모의투자 주식 현금 매수 주문 TR ID
ORDER_BUY_TR_ID = "VTTC0802U"
# 모의투자 주식 일별 주문체결 조회 TR ID
INQUIRE_CCLD_TR_ID = "VTTC8001R"
# 모의투자 주식 잔고조회 TR ID
INQUIRE_BALANCE_TR_ID = "VTTC8434R"

KST = ZoneInfo("Asia/Seoul")

ACCOUNT_TYPE = "모의"  # 이 파일은 모의투자 전용 - 노션 기록의 계좌구분은 항상 이 값
# 첫 실행(배관 확인 + 배선을 동시에 처음 돌리는 상황)은 실패 원인을 좁히려고
# 하루 1건으로 낮춰둔다. 며칠 정상 동작을 확인한 뒤 3으로 올릴 것.
MAX_ORDERS_PER_DAY = 1
WARNING_SUPPRESS_WINDOW_SECONDS = 3600  # 같은 사유의 시스템 경고는 이 간격 안엔 한 번만

# ── 시간 게이트 ─────────────────────────────────────────────
# 이 범위 밖이면 토큰 발급조차 하지 않고 자동매수 경로 전체를 건너뛴다.
# 상수 하나만 바꾸면 백테스트로 09:10 vs 10:00 등을 비교할 수 있다.
# 휴장일은 따로 판단하지 않는다 - 휴장일엔 시세 조회가 실패하거나 전일
# 데이터가 나와 candidates가 자연히 비거나 걸러진다.
TRADE_START_TIME = "09:10"
TRADE_END_TIME = "15:20"

# ── 자금 게이트 설정 ────────────────────────────────────────
MAX_STOCK_PRICE = 150_000   # 주가 상한 - 1주 반올림 오차 방지용
MAX_UNIT_RATIO = 0.20       # 1유닛 매수금액이 계좌평가액의 이 비율을 넘으면 제외
# ACCOUNT_SIZE는 상수로 두지 않는다. 계좌 평가액은 그때그때 달라지므로
# get_account_balance()로 매번 KIS 잔고조회 API에서 실시간으로 가져온다.


def get_access_token() -> str:
    """모의투자 접근토큰을 발급받는다. 토큰 값은 반환만 하고 출력하지 않는다.

    캐싱하지 않는다 - 실행마다 새로 발급받는다 (저장소가 아직 public이라
    토큰을 파일에 남기지 않으려는 것. 캐싱은 저장소를 private으로 옮긴
    뒤 별도로 한다). KIS는 잦은 재발급 시 이용 제한이 걸릴 수 있는데,
    발급 실패·거부를 조용히 넘어가지 않고 카톡으로 알린 뒤 예외를 올린다.
    """
    app_key = os.environ["KIS_APP_KEY"]
    app_secret = os.environ["KIS_APP_SECRET"]

    try:
        resp = requests.post(
            f"{BASE_URL}/oauth2/tokenP",
            json={
                "grant_type": "client_credentials",
                "appkey": app_key,
                "appsecret": app_secret,
            },
            timeout=10,
        )
        resp.raise_for_status()
        token = resp.json().get("access_token")
        if not token:
            raise RuntimeError("토큰 발급 응답에 access_token이 없습니다.")
    except Exception as e:                  # noqa: BLE001
        logger.error("KIS 접근토큰 발급 실패: %s", e)
        _notify_warning_throttled(WARN_TOKEN_FAILED, f"[KIS] ⚠ 접근토큰 발급 실패 - {e}")
        raise
    return token


def get_current_price(access_token: str, stock_code: str = "005930") -> str:
    """access_token으로 종목 현재가를 조회한다."""
    app_key = os.environ["KIS_APP_KEY"]
    app_secret = os.environ["KIS_APP_SECRET"]

    headers = {
        "content-type": "application/json; charset=utf-8",
        "authorization": f"Bearer {access_token}",
        "appkey": app_key,
        "appsecret": app_secret,
        "tr_id": "FHKST01010100",
    }
    params = {
        "FID_COND_MRKT_DIV_CODE": "J",
        "FID_INPUT_ISCD": stock_code,
    }

    resp = requests.get(
        f"{BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-price",
        headers=headers,
        params=params,
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()

    price = data.get("output", {}).get("stck_prpr")
    if not price:
        raise RuntimeError(f"현재가 조회 실패: {data.get('msg1', '알 수 없는 오류')}")
    return price


# ── 안전장치 (fail-closed) ──────────────────────────────────
#
# 중복 주문 방지·일일 주문 상한은 노션 "자동주문 기록" DB(NOTION_ORDERS_DB_ID)를
# 조회해서 판단한다. GitHub Actions는 실행마다 컨테이너가 새로 뜨고 사라져
# 로컬 파일·메모리 카운터로는 실행 간 상태가 안 남는다 - 09:00에 3건 주문하고
# 09:10에 또 3건 나가는 사고가 그래서 생긴다.
#
# fail-closed 원칙: 이 조회가 실패하면(네트워크·인증 만료 등) 절대 "0건"으로
# 간주하고 진행하지 않는다. 상태를 모르는 채로 주문하는 것보다 안 사는 게
# 낫다 - 조회 실패는 그 자체로 주문 중단 사유이고 카톡으로 알린다.

def _today() -> date:
    return datetime.now(KST).date()


def _trade_start_dt(day: date) -> datetime:
    """오늘(day)의 TRADE_START_TIME 시각(KST, tz-aware)."""
    return datetime.combine(
        day, datetime.strptime(TRADE_START_TIME, "%H:%M").time(), tzinfo=KST,
    )


def _within_trading_hours(now: Optional[datetime] = None) -> bool:
    now = now or datetime.now(KST)
    start = datetime.strptime(TRADE_START_TIME, "%H:%M").time()
    end = datetime.strptime(TRADE_END_TIME, "%H:%M").time()
    return start <= now.time() <= end


def _notify_failure(message: str) -> None:
    """주문 실패/차단을 기존 카톡 알림 파이프라인으로 통지한다. 매번 보낸다 -
    주문 성공·실패·거부·중복·상한 알림은 억제 대상이 아니다.

    알림 전송 자체가 실패해도(네트워크 등) 주문 흐름을 죽이지 않는다.
    """
    try:
        kakao.send_kakao_message(message)
    except Exception as e:                  # noqa: BLE001
        logger.warning("실패 알림 전송도 실패했습니다 (%s)", e)


# 억제 대상 시스템 경고의 사유 키. 노션 "자동주문 기록" DB에 상태="경고" 행의
# 사유 칸에 그대로 저장되고, 같은 키로 최근 발송 시각을 다시 조회한다.
WARN_ORDER_STATUS_UNKNOWN = "주문 상태 조회 실패"
WARN_TOKEN_FAILED = "토큰 발급 실패"
WARN_NOTION_RECORD_FAILED = "노션 기록 실패"
WARN_NO_SECTOR_MAP = "업종 맵 없음"
WARN_PENDING_ORDERS = "주문중 상태로 남은 행 있음"
# 반복 거부는 종목마다 별도 사유로 취급한다(종목코드를 키에 포함) - 한
# 종목의 반복 거부 경고가 다른 종목의 같은 경고를 억제해버리면 안 된다.
WARN_REPEATED_REJECTION_PREFIX = "같은 종목 반복 거부"

MAX_REJECTIONS_PER_STOCK = 3  # 같은 종목 오늘 거부 누적 이 값 이상이면 재시도 중단


def _repeated_rejection_reason_key(stock_code: str) -> str:
    return f"{WARN_REPEATED_REJECTION_PREFIX} - {stock_code}"


def _notify_warning_throttled(reason_key: str, message: str) -> None:
    """반복되는 시스템 경고(조회/발급/기록 실패류)를 억제해서 보낸다.

    같은 reason_key로 WARNING_SUPPRESS_WINDOW_SECONDS(1시간) 안에 이미
    보냈으면 이번엔 카톡을 건너뛴다 - cron-job.org가 10분마다 불러도 노션/KIS가
    몇 시간 죽어 있으면 같은 경고가 수십 통 오는 걸 막는다. 억제되더라도
    logger.warning은 매번 남긴다 (알림만 줄이고 기록까지 줄이면 안 됨).

    억제 상태는 메모리·로컬파일에 두지 않는다 (컨테이너가 매번 새로 뜨면
    무의미하다) - 노션 "자동주문 기록" DB에 상태="경고" 행으로 남기고 그
    최근 시각을 조회한다.

    fail-open 주의: 이 억제 판단 자체가 실패해도(노션 조회 불가가 바로 이
    경고의 원인일 수 있다) 절대 주문을 막는 방향으로 쓰면 안 된다. 판단이
    안 되면 그냥 보낸다 - 알림 과다는 불편이지만 알림 누락은 사고다. 이
    파일의 다른 노션 조회(_check_order_allowed 등)는 반대로 fail-closed인데,
    그건 "판단 안 되면 주문 안 한다"이고 여기는 "판단 안 되면 그냥 알린다"라
    방향이 다르다.
    """
    logger.warning(message)

    try:
        last = notion_repo.latest_warning_at(reason_key)
        suppress = (
            last is not None
            and (datetime.now(KST) - last).total_seconds() < WARNING_SUPPRESS_WINDOW_SECONDS
        )
    except Exception as e:                  # noqa: BLE001
        logger.warning("경고 억제 판단 실패 (%s) - fail-open으로 그냥 보냅니다", e)
        suppress = False

    if suppress:
        logger.info("경고 억제됨 (최근 %d초 내 동일 사유): %s",
                    WARNING_SUPPRESS_WINDOW_SECONDS, reason_key)
        return

    _notify_failure(message)

    try:
        notion_repo.create_order_record(
            name=reason_key, ticker="", order_no="", qty=0, price=0,
            status="경고", reason=reason_key, account_type=ACCOUNT_TYPE,
            when=datetime.now(KST),
        )
    except Exception as e:                  # noqa: BLE001
        logger.warning("경고 기록 실패 (%s) - 다음 억제 판단에 이번 발송이 "
                       "안 잡힐 수 있습니다", e)


def _check_order_allowed(stock_code: str) -> Optional[str]:
    """중복 주문·반복 거부·일일 상한을 노션 DB 조회로 판단한다.

    반환: 막힌 이유(사람이 읽을 메시지) 또는 통과라면 None.
    조회 자체가 실패하면 fail-closed로 막는다 (0건으로 간주하지 않음).
    """
    day = _today()
    try:
        # 주의: 이 중복 방지는 "1유닛 신규 진입" 전용 규칙임.
        # 터틀 추가매수(2·3·4유닛)를 자동화할 경우, 같은 날 같은 종목의
        # 정당한 추가매수까지 차단됨. 그때는 종목코드 단독이 아니라
        # 종목코드+유닛번호 기준으로 변경해야 함.
        # (현재는 추가매수를 증권사 앱 지정가 알림으로 수동 처리 중)
        #
        # "거부"는 이 중복 방지에서 뺀다(has_order_today가 이미 그렇게
        # 걸러준다) - 거래소가 명시적으로 안 받았다는 뜻이라 체결 가능성이
        # 사실상 없다. 터틀은 돌파 당일 진입이 핵심이라, 장 초반 일시적
        # 오류(호가 미개시·수량 오류·네트워크)로 거부됐다고 그날을 통째로
        # 막으면 다음날 갭 때문에 추격금지에 걸릴 위험이 크다. 반대로
        # "실패"는 응답을 못 받았을 뿐 실제로는 체결됐을 수 있어 차단을
        # 유지한다("주문중"과 같은 취급).
        if notion_repo.has_order_today(stock_code, day):
            return f"{stock_code} 오늘 이미 주문했습니다 (중복 방지 - 1유닛 진입 기준)"

        # 거부 재시도를 무한 허용하진 않는다 - 같은 종목이 오늘 계속
        # 거부된다는 건 일시적 오류가 아니라 구조적 문제(계좌 상태·종목
        # 자체 이슈 등)라는 신호다. 이때부턴 재시도를 막고 경고 1회
        # (종목별 1시간 억제 - 다른 종목의 반복 거부까지 같이 억제되면 안 됨).
        #
        # 스트라이크는 TRADE_START_TIME 이후 거부만 센다 - 장 열리기 전
        # (호가 미확정 구간)에 거부가 쌓이면, 정작 09:10에 진짜 진입해야
        # 할 때 이미 차단 상태가 되는 문제가 있었다.
        rejected = notion_repo.count_rejected_orders_today(
            stock_code, day, since=_trade_start_dt(day),
        )
        if rejected >= MAX_REJECTIONS_PER_STOCK:
            _notify_warning_throttled(
                _repeated_rejection_reason_key(stock_code),
                f"[KIS] ⚠ {stock_code} 오늘 거부 {rejected}건 누적 - 구조적 문제로 "
                f"보고 재시도를 중단합니다. 원인 확인 필요",
            )
            return f"{stock_code} 오늘 거부 {rejected}건 누적 - 재시도 중단"

        counts = notion_repo.count_orders_by_status_today(day, ACCOUNT_TYPE)
        success_n, pending_n = counts.get("성공", 0), counts.get("주문중", 0)
        if success_n + pending_n >= MAX_ORDERS_PER_DAY:
            logger.info(
                "일일 상한 상세: 성공 %d건 + 주문중 %d건 / 상한 %d건",
                success_n, pending_n, MAX_ORDERS_PER_DAY,
            )
            return (
                f"일일 상한 도달 (성공 {success_n}건 + 주문중 {pending_n}건 "
                f"/ 상한 {MAX_ORDERS_PER_DAY}건)"
            )
    except Exception as e:                  # noqa: BLE001
        logger.error("노션 주문 상태 조회 실패 - fail-closed로 주문을 중단합니다: %s", e)
        _notify_warning_throttled(
            WARN_ORDER_STATUS_UNKNOWN,
            "[KIS] ⚠ 주문 상태 조회 실패 — 안전을 위해 주문 중단",
        )
        return "주문 상태 조회 실패 (fail-closed)"
    return None


def _create_pending_record(*, name: str, stock_code: str, qty: float,
                            price: float) -> str:
    """주문 전송 "전"에 상태="주문중"으로 노션에 1행을 미리 만든다.

    기록할 수 없으면 주문하지 않는다: 이 사전 기록이 실패하면 호출자는
    주문을 아예 내지 않는다(fail-closed). 전송 -> 기록 순서였던 이전
    구조는, 주문은 나갔는데 기록이 실패하면 다음 실행이 그 사실을 몰라
    같은 종목을 또 사는 구멍이 있었다. 순서를 뒤집어 그 구멍을 없앤다.

    예외를 그대로 올린다 - 호출자(place_market_buy_order)가 fail-closed로
    처리한다.
    """
    return notion_repo.create_order_record(
        name=name, ticker=stock_code, order_no="", qty=qty, price=price,
        status="주문중", reason="", account_type=ACCOUNT_TYPE,
        when=datetime.now(KST),
    )


def _update_order_record(page_id: str, *, status: str,
                          order_no: str = "", reason: str = "") -> None:
    """사전에 만들어둔 "주문중" 행을 주문 결과로 갱신한다 (새 행을 만들지 않음).

    이 시점엔 이미 주문이 실제로 나간 뒤라(성공·실패·거부 어느 쪽이든)
    fail-open이다: 갱신이 실패해도 주문 결과 자체는 막지 않는다. 대신
    카톡으로 경고하고 넘어간다. 갱신이 안 되면 행은 "주문중"으로 남는데,
    그 덕분에 has_order_today 기준 중복 방지는 계속 작동한다(다음 실행이
    같은 종목을 또 사지는 않는다) - 다만 일일 상한 계산에서도 "주문중"을
    센다는 전제가 있어야 한다(notion_repo.count_success_orders_today).

    유령 행 트레이드오프: 사전 기록 후 전송 중 프로세스가 죽으면 "주문중"
    행이 영영 안 지워져 그 종목이 하루 동안 차단될 수 있다. 이건 감수한다
    - 실제로 안 산 종목을 하루 못 사는 것보다, 실제로 산 종목을 모르고
    또 사는 게 훨씬 나쁘다. 이 행을 코드가 자동으로 정리하지도 않는다
    (사람이 실제 체결 여부를 확인하고 정리해야 한다) - 대신
    warn_pending_orders_at_startup()이 매 실행 시작 시 알린다.
    """
    try:
        notion_repo.update_order_record(page_id, status=status,
                                         order_no=order_no, reason=reason)
    except Exception as e:                  # noqa: BLE001
        logger.error("노션 주문 기록 갱신 실패 (page_id=%s, status=%s): %s",
                     page_id, status, e)
        _notify_warning_throttled(
            WARN_NOTION_RECORD_FAILED,
            f"[KIS] ⚠ 주문 결과 기록 갱신 실패({status}) - 노션엔 '주문중'으로 "
            f"남아있어 오늘 중복 방지는 유지되지만 실제 결과를 확인해야 "
            f"합니다: {e}",
        )


def warn_pending_orders_at_startup() -> None:
    """실행 시작 시 오늘 "주문중"으로 남은 유령 행이 있으면 알린다.

    자동으로 정리하지 않는다 - 사람이 실제 체결 여부를 확인하고 지워야
    한다(_update_order_record의 유령 행 트레이드오프 참고). 이 조회는
    안전장치가 아니라 알림용이라, 실패해도 주문 흐름을 막지 않고 로그만
    남기고 넘어간다. 같은 사유 경고는 1시간 억제 대상이다.
    """
    try:
        pending = notion_repo.list_pending_orders_today(_today())
    except Exception as e:                  # noqa: BLE001
        logger.warning("주문중 유령 행 조회 실패 (%s) - 이번엔 건너뜁니다", e)
        return

    if not pending:
        return

    names = ", ".join(p["name"] or p["ticker"] for p in pending)
    _notify_warning_throttled(
        WARN_PENDING_ORDERS,
        f"[KIS] ⚠ 주문중 상태로 남은 행 있음: {names} — 실제 체결 여부 확인 필요",
    )


# ── 주문 ────────────────────────────────────────────────────

def _parse_account(account: str) -> tuple[str, str]:
    """KIS_ACCOUNT를 계좌번호 앞 8자리(CANO)/상품코드 뒤 2자리(ACNT_PRDT_CD)로 나눈다.

    "12345678-01" 또는 "1234567801" 두 형식을 모두 허용한다.
    """
    account = account.strip()
    if "-" in account:
        cano, prdt_cd = account.split("-", 1)
        return cano, prdt_cd
    return account[:8], account[8:10]


def _get_hashkey(app_key: str, app_secret: str, body: dict) -> str:
    resp = requests.post(
        f"{BASE_URL}/uapi/hashkey",
        headers={
            "content-type": "application/json; charset=utf-8",
            "appkey": app_key,
            "appsecret": app_secret,
        },
        json=body,
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["HASH"]


def _send_market_buy(access_token: str, stock_code: str, qty: int) -> dict:
    """모의투자 시장가 매수 주문을 KIS API로 전송하고 응답 body를 그대로 반환한다.

    HTTP 레벨 오류(인증 실패 등)는 예외로 던지고, "주문 거부" 같은 업무 레벨
    실패는 rt_cd != "0"인 정상 응답으로 돌아오므로 호출자가 판단한다.
    """
    app_key = os.environ["KIS_APP_KEY"]
    app_secret = os.environ["KIS_APP_SECRET"]
    cano, acnt_prdt_cd = _parse_account(os.environ["KIS_ACCOUNT"])

    body = {
        "CANO": cano,
        "ACNT_PRDT_CD": acnt_prdt_cd,
        "PDNO": stock_code,
        "ORD_DVSN": "01",  # 시장가
        "ORD_QTY": str(qty),
        "ORD_UNPR": "0",
    }
    hashkey = _get_hashkey(app_key, app_secret, body)

    headers = {
        "content-type": "application/json; charset=utf-8",
        "authorization": f"Bearer {access_token}",
        "appkey": app_key,
        "appsecret": app_secret,
        "tr_id": ORDER_BUY_TR_ID,
        "custtype": "P",
        "hashkey": hashkey,
    }
    resp = requests.post(
        f"{BASE_URL}/uapi/domestic-stock/v1/trading/order-cash",
        headers=headers,
        json=body,
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def place_market_buy_order(access_token: str, stock_code: str, qty: int,
                            *, name: Optional[str] = None,
                            ref_price: float = 0.0) -> dict:
    """모의투자 시장가 매수 주문 1건을 시도한다.

    순서: 중복 주문·일일 상한 확인(노션 조회, fail-closed) -> 사전 기록
    (상태="주문중", fail-closed) -> 실제 전송 -> 사후 기록 갱신(fail-open).

    사전 기록을 전송보다 먼저 하는 이유: 전송 -> 기록 순서면 주문은
    나갔는데 기록이 실패했을 때 다음 실행이 그 사실을 몰라 같은 종목을
    또 산다. 기록할 수 없으면 애초에 주문을 내지 않는 쪽으로 순서를
    뒤집었다 - "주문중" 행 자체가 이미 중복 방지 역할을 하므로, 전송
    후 결과 갱신이 실패해도(fail-open) 그 행이 "주문중"으로 남아
    같은 종목 재주문은 계속 막힌다.

    Args:
        name: 노션 기록용 종목명. 없으면 종목코드로 대신한다.
        ref_price: 노션 "주문가" 참고용 (시장가라 체결 전엔 정확한 값을 모른다).
    반환: {"status": "sent"|"rejected"|"error"|"blocked", ...}
    """
    display_name = name or stock_code

    blocked_reason = _check_order_allowed(stock_code)
    if blocked_reason is not None:
        msg = f"[KIS] 매수 주문 건너뜀 - {blocked_reason}"
        logger.warning(msg)
        # "주문 상태 조회 실패"는 _check_order_allowed 안에서 이미 알렸다.
        if "조회 실패" not in blocked_reason:
            _notify_failure(msg)
        return {"status": "blocked", "msg": blocked_reason}

    try:
        page_id = _create_pending_record(
            name=display_name, stock_code=stock_code, qty=qty, price=ref_price,
        )
    except Exception as e:                  # noqa: BLE001
        msg = f"[KIS] 매수 주문 건너뜀 - 사전 기록 실패로 주문 중단: {stock_code} {e}"
        logger.error(msg)
        _notify_failure(msg)
        return {"status": "blocked", "msg": f"사전 기록 실패 (fail-closed): {e}"}

    try:
        result = _send_market_buy(access_token, stock_code, qty)
    except Exception as e:                  # noqa: BLE001
        msg = f"[KIS] 매수 주문 전송 실패 - {stock_code} {qty}주: {e}"
        logger.error(msg)
        _notify_failure(msg)
        _update_order_record(page_id, status="실패", reason=str(e))
        return {"status": "error", "msg": str(e)}

    if result.get("rt_cd") != "0":
        reason = result.get("msg1", "알 수 없는 오류")
        msg = f"[KIS] 매수 주문 거부 - {stock_code} {qty}주: {reason}"
        logger.warning(msg)
        _notify_failure(msg)
        _update_order_record(page_id, status="거부", reason=reason)
        return {"status": "rejected", "msg": reason}

    order_no = result["output"]["ODNO"]
    _update_order_record(page_id, status="성공", order_no=order_no, reason="")
    return {"status": "sent", "order_no": order_no}


def get_order_execution(access_token: str, order_no: str) -> dict | None:
    """오늘 주문의 체결 여부를 조회한다. 체결 전이거나 못 찾으면 None."""
    app_key = os.environ["KIS_APP_KEY"]
    app_secret = os.environ["KIS_APP_SECRET"]
    cano, acnt_prdt_cd = _parse_account(os.environ["KIS_ACCOUNT"])
    today = _today().strftime("%Y%m%d")

    headers = {
        "content-type": "application/json; charset=utf-8",
        "authorization": f"Bearer {access_token}",
        "appkey": app_key,
        "appsecret": app_secret,
        "tr_id": INQUIRE_CCLD_TR_ID,
    }
    params = {
        "CANO": cano,
        "ACNT_PRDT_CD": acnt_prdt_cd,
        "INQR_STRT_DT": today,
        "INQR_END_DT": today,
        "SLL_BUY_DVSN_CD": "00",
        "INQR_DVSN": "00",
        "PDNO": "",
        "CCLD_DVSN": "00",
        "ORD_GNO_BRNO": "",
        "ODNO": "",
        "INQR_DVSN_3": "00",
        "INQR_DVSN_1": "",
        "CTX_AREA_FK100": "",
        "CTX_AREA_NK100": "",
    }
    resp = requests.get(
        f"{BASE_URL}/uapi/domestic-stock/v1/trading/inquire-daily-ccld",
        headers=headers,
        params=params,
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()

    for row in data.get("output1", []):
        if row.get("odno") != order_no:
            continue
        filled_qty = int(row.get("tot_ccld_qty") or 0)
        if filled_qty <= 0:
            return None
        return {
            "filled_qty": filled_qty,
            "avg_price": float(row.get("avg_prvs") or 0),
        }
    return None


# ── 자금 게이트 ─────────────────────────────────────────────

def get_account_balance(access_token: str) -> dict:
    """모의투자 잔고조회 API로 계좌평가액·예수금(가용현금)·보유종목을 실시간 조회한다.

    반환: {"account_size": 총평가금액, "available_cash": 예수금총금액,
           "holdings": [{"ticker","qty"}, ...]}  (이 모의계좌 자체 보유분)
    """
    app_key = os.environ["KIS_APP_KEY"]
    app_secret = os.environ["KIS_APP_SECRET"]
    cano, acnt_prdt_cd = _parse_account(os.environ["KIS_ACCOUNT"])

    headers = {
        "content-type": "application/json; charset=utf-8",
        "authorization": f"Bearer {access_token}",
        "appkey": app_key,
        "appsecret": app_secret,
        "tr_id": INQUIRE_BALANCE_TR_ID,
    }
    params = {
        "CANO": cano,
        "ACNT_PRDT_CD": acnt_prdt_cd,
        "AFHR_FLPR_YN": "N",
        "OFL_YN": "",
        "INQR_DVSN": "02",
        "UNPR_DVSN": "01",
        "FUND_STTL_ICLD_YN": "N",
        "FNCG_AMT_AUTO_RDPT_YN": "N",
        "PRCS_DVSN": "00",
        "CTX_AREA_FK100": "",
        "CTX_AREA_NK100": "",
    }
    resp = requests.get(
        f"{BASE_URL}/uapi/domestic-stock/v1/trading/inquire-balance",
        headers=headers,
        params=params,
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()

    output2 = data.get("output2") or [{}]
    row = output2[0]
    holdings = [
        {"ticker": h.get("pdno", ""), "qty": int(float(h.get("hldg_qty") or 0))}
        for h in data.get("output1") or []
        if int(float(h.get("hldg_qty") or 0)) > 0
    ]
    return {
        "account_size": float(row.get("tot_evlu_amt") or 0),
        "available_cash": float(row.get("dnca_tot_amt") or 0),
        "holdings": holdings,
    }


def _get_sector_map() -> dict[str, str]:
    """캐시된 업종 맵(data/sector_map.json). 상관군 그룹 구분에 쓴다.

    상관군(corr_group)은 노션에 사람이 직접 매기는 값이라 시스템이 미리
    알 수 없다 (intraday_watch.py의 _unit_suffix와 같은 전제). 업종을
    대용으로 쓴다. 못 불러오면 빈 dict - 그룹 구분 없이 전체 캡만 적용된다.
    """
    try:
        import screener
        day = datetime.now(KST).strftime("%Y%m%d")
        return screener.load_sector_map(day)
    except Exception as e:                  # noqa: BLE001
        logger.warning("업종 맵을 불러오지 못했습니다 (%s) - 상관군별 캡 없이 "
                       "전체 캡만 적용합니다", e)
        return {}


def get_mock_account_corr_units(account_size: float, holdings: list[dict]) -> dict:
    """모의계좌 자체 보유분만으로 상관군별·전체 누적 유닛을 계산한다.

    노션 점검표(실계좌 보유)는 절대 참조하지 않는다 - 실계좌와 모의계좌는
    격리를 위해 분리했고, 유닛주수 계산 기준(계좌 규모)도 서로 달라
    합산이 성립하지 않는다. core.calc_position()·core.fetch_ohlcv()·
    core.calc_atr()만 가져다 쓰고 판정 로직 자체는 다시 만들지 않는다.

    반환: {"total_units": float, "groups": {업종명: units}}
    """
    if not holdings:
        return {"total_units": 0.0, "groups": {}}

    sector_map = _get_sector_map()
    total = 0.0
    groups: dict[str, float] = {}
    for h in holdings:
        ticker, qty = h["ticker"], h["qty"]
        try:
            df = core.fetch_ohlcv(ticker)
            atr = core.calc_atr(df)
        except Exception as e:              # noqa: BLE001
            logger.warning("모의계좌 보유종목 %s ATR 조회 실패 (%s) - "
                           "유닛 집계에서 제외합니다", ticker, e)
            continue
        unit_shares = core.calc_position(atr, account_size).unit_shares
        if unit_shares <= 0:
            continue
        units = qty / unit_shares
        total += units
        sector = sector_map.get(ticker, "")
        if sector:
            groups[sector] = groups.get(sector, 0.0) + units

    return {"total_units": total, "groups": groups}


def select_buy_candidates(access_token: str, candidates: list[dict]) -> list[dict]:
    """매수 후보를 자금 게이트로 거른다. 실제 주문은 걸지 않는다 (호출자 몫).

    candidates 원소: {"ticker","name","price","atr20","gap_atr","sector"}
        (intraday_watch.py의 judge() 결과 dict와 같은 모양)

    상관군 캡의 기준(현재 보유 유닛)은 항상 이 함수 안에서 KIS 잔고조회
    API로 직접 구한다 (get_mock_account_corr_units) - 노션 실계좌 보유를
    외부에서 넘겨받지 않는다. 그래서 이 값이 "결측이라 조용히 스킵"될
    길이 없다.

    순서: 갭 오름차순 정렬 → 가격 상한 → 유닛금액 비율(핵심 게이트) →
          상관군 캡 → 가용현금 → 일일 주문 상한.
    막힌 종목은 사유별로 기존 카톡 알림 경로로 통지하고 다음 순위로 넘어간다.
    한 번의 호출 안에서 여러 종목이 선정되면, 그만큼 유닛·현금 여력을
    누적해서 소모한다 (같은 상관군 후보를 연달아 다 뽑아버리는 일이 없게).
    반환: 통과한 후보 리스트 (각 원소에 "unit_shares" 추가), 갭 오름차순 그대로.
    """
    balance = get_account_balance(access_token)
    account_size = balance["account_size"]

    corr = get_mock_account_corr_units(account_size, balance["holdings"])
    if balance["holdings"] and not corr["groups"]:
        _notify_warning_throttled(
            WARN_NO_SECTOR_MAP,
            "[KIS] ⚠ 업종 맵 없이 실행됨 - 상관군별 캡 미적용(전체 캡만 적용)",
        )

    # 1. 갭 오름차순 정렬 - core.sort_by_gap()을 쓴다. app.py·intraday_watch.py와
    #    같은 정렬을 세 군데서 각자 구현하던 걸 core.py 한 곳으로 모았다.
    ordered = core.sort_by_gap(candidates)

    # 이번 호출에서 선정되는 종목만큼 실시간으로 깎아가며 다음 후보를 본다.
    group_units = dict(corr["groups"])
    total_units = corr["total_units"]
    cash_remaining = balance["available_cash"]

    # 일일 주문 상한도 노션 조회 기준 (fail-closed). 조회 자체가 실패하면
    # "0건"으로 간주해 진행하지 않고, 이번 라운드는 통째로 건너뛴다.
    try:
        already_success = notion_repo.count_success_orders_today(_today(), ACCOUNT_TYPE)
    except Exception as e:                  # noqa: BLE001
        logger.error("노션 주문 상태 조회 실패 - fail-closed로 이번 라운드를 건너뜁니다: %s", e)
        _notify_warning_throttled(
            WARN_ORDER_STATUS_UNKNOWN,
            "[KIS] ⚠ 주문 상태 조회 실패 — 안전을 위해 주문 중단",
        )
        return []

    remaining_slots = max(0, MAX_ORDERS_PER_DAY - already_success)

    selected: list[dict] = []
    for c in ordered:
        name, price, atr = c["name"], c["price"], c["atr20"]

        # 2. 가격 상한
        if price > MAX_STOCK_PRICE:
            _notify_failure(f"[KIS] 가격 상한 초과: {name}")
            continue

        # 3. 유닛금액 과다 - 핵심 게이트. 유닛주수는 스캔 시점 값을 재사용하지
        #    않고 지금 막 조회한 실시간 계좌평가액으로 core.calc_position을
        #    다시 호출한다 ("이 계좌 규모로 감당되는가"는 지금 기준이어야 함).
        unit_shares = core.calc_position(atr, account_size).unit_shares
        if unit_shares <= 0:
            _notify_failure(f"[KIS] 유닛 계산 불가(ATR 이상): {name}")
            continue
        unit_amount = unit_shares * price
        ratio = unit_amount / account_size if account_size > 0 else float("inf")
        if ratio > MAX_UNIT_RATIO:
            # 1000만원 안팎 계좌에서는 ATR%가 낮은(=변동성 작은) 종목일수록
            # 유닛주수가 커져 유닛금액도 커진다. 여기서 자주 걸리는 건
            # 버그가 아니라 계좌 규모 대비 종목이 안 맞는다는 의도된 신호다.
            msg = f"[KIS] 유닛금액 과다(계좌 {ratio * 100:.1f}%): {name}"
            logger.info(msg)
            _notify_failure(msg)
            continue

        # 4. 상관군 캡 (종목당 4유닛 · 상관군당 6유닛 · 전체 12유닛)
        #    돌파 후보는 아직 보유 전이라 이 종목 자체의 기존 유닛은 0이다.
        sector = c.get("sector", "")
        stock_units_after = 1
        group_units_after = group_units.get(sector, 0) + 1
        total_units_after = total_units + 1
        if (stock_units_after > core.MAX_UNITS
                or group_units_after > core.MAX_UNITS_GROUP
                or total_units_after > core.MAX_UNITS_TOTAL):
            _notify_failure(f"[KIS] 상관군 캡: {name}")
            continue

        # 5. 현금 부족 - 마지막 그물
        if unit_amount > cash_remaining:
            _notify_failure(f"[KIS] 현금 부족: {name}")
            continue

        # 6. 일일 주문 건수 상한 - 갭이 낮은(우선순위 높은) 순으로 이미
        #    남은 자리를 다 채웠으면 나머지는 자격이 있어도 밀린다.
        if len(selected) >= remaining_slots:
            _notify_failure(f"[KIS] 우선순위 밀림: {name}")
            continue

        selected.append({**c, "unit_shares": unit_shares})
        group_units[sector] = group_units.get(sector, 0) + 1
        total_units += 1
        cash_remaining -= unit_amount

    return selected


# ── 진입점 (intraday_watch.py가 호출) ───────────────────────

def run_auto_trade(candidates: list[dict]) -> None:
    """돌파 후보를 받아 자금 게이트를 통과한 종목만 실제로 매수한다.

    intraday_watch.py가 돌파를 잡을 때마다 호출하는 이 파일의 진입점이다.
    candidates 원소 형태는 intraday_watch.judge()의 결과와 같다
    ({"ticker","name","sector","price","atr20","gap_atr", ...}).

    유령 행 경고(warn_pending_orders_at_startup)를 candidates가 있으면
    항상 먼저 호출한다 - 이 함수가 매 실행마다 반드시 거치는 진입점이라,
    __main__에만 있으면 import로 호출될 때(intraday_watch.py 안에서)
    조용히 빠지는 문제가 없다. 실제 매수 여부와 무관한 건강 체크라
    아래 시간 게이트보다 먼저 돈다(장 시작 전이라도 알아야 하므로).

    유령 행 경고 다음이 시간 게이트다: TRADE_START_TIME~TRADE_END_TIME
    밖이면 토큰 발급조차 하지 않고 로그만 남기고 즉시 반환한다 - 장
    시작 전 호가 미확정 구간에 쏟아지는 거부를 원천적으로 피한다.
    휴장일은 따로 판단하지 않는다: 휴장일엔 candidates 자체가
    비거나(스캔이 전일 데이터를 걸러냄) 시세 조회가 실패해 자연히
    걸러진다.
    """
    if not candidates:
        return

    # 유령 행 경고는 거래 시간 게이트보다 먼저 - 장 시작 전이라도 이전에
    # 죽은 "주문중" 행이 있으면 바로 알아야 한다. 실제 매수 여부와는
    # 별개의 건강 체크라 시간 게이트를 타지 않는다.
    warn_pending_orders_at_startup()

    if not _within_trading_hours():
        logger.info(
            "거래 시간(%s~%s) 아님 - 자동매수 건너뜀 (현재 %s)",
            TRADE_START_TIME, TRADE_END_TIME, datetime.now(KST).strftime("%H:%M"),
        )
        return

    token = get_access_token()
    selected = select_buy_candidates(token, candidates)
    if not selected:
        logger.info("자동매수 후보 없음 (게이트 통과 0건, 후보 %d건)", len(candidates))
        return

    for c in selected:
        result = place_market_buy_order(
            token, c["ticker"], c["unit_shares"],
            name=c["name"], ref_price=c["price"],
        )
        logger.info("자동매수: %s(%s) -> %s", c["name"], c["ticker"], result)


if __name__ == "__main__":
    TEST_STOCK_CODE = "005930"
    TEST_QTY = 1

    warn_pending_orders_at_startup()

    token = get_access_token()
    result = place_market_buy_order(token, TEST_STOCK_CODE, TEST_QTY, name="삼성전자")

    if result["status"] == "sent":
        order_no = result["order_no"]
        time.sleep(1)  # 체결 처리 대기
        fill = get_order_execution(token, order_no)
        if fill:
            print(f"주문번호: {order_no}")
            print(f"체결가: {fill['avg_price']}원")
            print(f"체결수량: {fill['filled_qty']}주")
        else:
            print(f"주문번호: {order_no} - 아직 미체결")
    else:
        print(f"[{result['status']}] {result['msg']}")
