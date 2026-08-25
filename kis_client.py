"""
kis_client.py - 한국투자증권(KIS) Open API 연동

1단계: 접근토큰 발급 -> 현재가 조회.
2단계: 모의투자 시장가 매수 주문 1건 전송 -> 주문번호 확인 -> 체결 여부 조회.
3단계: 자금 게이트 - 후보를 실제로 주문 넣기 전에 "이 계좌 규모로 감당되는가"를
       거른다. 신호 판정(20일 고가·갭·손절선)은 core.py 소관이라 여기서는
       core.calc_position()·core.MAX_UNITS* 상수만 가져다 쓰고 다시 만들지 않는다.
매도: 터틀 청산(손절선 이탈·추세청산)을 run_auto_sell()이 실주문으로 잇는다.
판정 자체는 core.evaluate_holding()과 아침 배치가 이미 하고 있어 여기서 다시
만들지 않는다. 중복 매도는 노션 장부가 아니라 증권사 잔고(주문가능수량)로 막는다.

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
NOTION_ORDERS_DB_ID, NOTION_DB_ID (모의투자). 하나라도 없으면 자동매수·매도를
통째로 건너뛴다 - AUTO_TRADE_ENV 참고.
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
# 모의투자 주식 현금 매도 주문 TR ID
ORDER_SELL_TR_ID = "VTTC0801U"
# 모의투자 주식 일별 주문체결 조회 TR ID
INQUIRE_CCLD_TR_ID = "VTTC8001R"
# 모의투자 주식 잔고조회 TR ID
INQUIRE_BALANCE_TR_ID = "VTTC8434R"

KST = ZoneInfo("Asia/Seoul")

# 자동매매에 필요한 환경변수. 하나라도 없으면 이 실행 환경은 자동매매용이
# 아니라고 보고 매수·매도를 통째로 건너뛴다.
#
# intraday_watch.py는 두 워크플로가 공유한다: auto-trade.yml(자동매매,
# 자격증명 있음)과 intraday.yml(돌파 알림 전용, 자격증명 없음). 후자에서
# 노션·KIS 조회가 실패하는 건 정상이므로 장애로 다루면 안 된다 - 실패로
# 보고 카톡을 보내면 10분마다 경고가 쏟아진다(경고 억제 판단조차 노션을
# 타서 같이 죽으므로 억제도 안 걸린다). 설정 안 됨은 장애가 아니라 구성이다.
AUTO_TRADE_ENV = (
    "KIS_APP_KEY", "KIS_APP_SECRET", "KIS_ACCOUNT",
    "NOTION_TOKEN", "NOTION_ORDERS_DB_ID", "NOTION_DB_ID",
)


def _auto_trade_configured() -> bool:
    """이 실행 환경에 자동매매 자격증명이 갖춰져 있는가. 없으면 조용히 건너뛴다."""
    missing = [k for k in AUTO_TRADE_ENV if not os.environ.get(k)]
    if missing:
        logger.info("자동매매 환경변수 미설정 (%s) - 자동매수·자동매도를 건너뜁니다",
                    ", ".join(missing))
        return False
    return True


ACCOUNT_TYPE = "모의"  # 이 파일은 모의투자 전용 - 노션 기록의 계좌구분은 항상 이 값
# 하루 상한은 신규 진입과 추가매수를 따로 센다. 한 예산에 섞으면 추가매수가
# 신규 진입 여력을 먹어 그날 새 돌파를 못 잡는다(반대도 마찬가지).
# 모의투자 기준이며 실전 전환 시 다시 판단할 것.
#
# 신규 진입: 첫 배선 확인 동안 1건으로 낮춰뒀다가 매수·매도 배관이 모두
# 실주문으로 확인된 뒤 3건으로 올렸다.
MAX_ORDERS_PER_DAY = 3
# 추가매수 전체: 종목 4·상관군 6·전체 12유닛 캡이 총 노출을 이미 묶으므로,
# 이 값은 "하루에 얼마나 빨리 키울 수 있나"만 제한한다.
MAX_PYRAMID_ORDERS_PER_DAY = 4
# 같은 종목 하루 추가매수 횟수. 3회면 1유닛에서 4유닛(만유닛)까지 하루 만에
# 시장가로 다 채워진다 - 그날 스파이크가 되돌리면 꼭대기에서 만유닛이 된다.
# 2회로 묶으면 하루 최대 3유닛까지만 가고 4번째는 다음 날로 넘어간다.
MAX_PYRAMID_ADDS_PER_STOCK = 2
WARNING_SUPPRESS_WINDOW_SECONDS = 3600  # 같은 사유의 시스템 경고는 이 간격 안엔 한 번만

# ── 시간 게이트 ─────────────────────────────────────────────
# 이 범위 밖이면 토큰 발급조차 하지 않고 자동매수 경로 전체를 건너뛴다.
# 상수 하나만 바꾸면 백테스트로 09:10 vs 10:00 등을 비교할 수 있다.
#
# 휴장일(주말·공휴일)은 이 시:분 범위만으로는 안 걸러진다 - 토요일도
# 09:10~15:20 안에 들어오기 때문이다. cron-job.org가 요일과 무관하게
# workflow_dispatch를 계속 쏴서(2026-08-22 토요일 확인), 휴장일에도
# 이 범위를 통과해 KIS 토큰 발급을 시도하다 서버가 응답하지 않아
# 타임아웃 경고가 반복됐다. _is_trading_day()가 별도로 막는다
# (아래 3개 진입 함수 전부에 적용).
TRADE_START_TIME = "09:10"
TRADE_END_TIME = "15:20"

# ── 자금 게이트 설정 ────────────────────────────────────────
MAX_STOCK_PRICE = 150_000   # 주가 상한 - 1주 반올림 오차 방지용
MAX_UNIT_RATIO = 0.20       # 1유닛 매수금액이 계좌평가액의 이 비율을 넘으면 제외
# ACCOUNT_SIZE는 상수로 두지 않는다. 계좌 평가액은 그때그때 달라지므로
# get_account_balance()로 매번 KIS 잔고조회 API에서 실시간으로 가져온다.


# 토큰 발급 재시도. KIS 모의투자 서버는 간헐적으로 응답이 늦어 read timeout이
# 난다. 토큰은 매수·매도 양쪽의 단일 관문이라 여기서 죽으면 그 회차는 손절
# 감시까지 통째로 건너뛴다 - 10분을 그냥 버리느니 짧게 한 번 더 시도한다.
#
# 네트워크 계층 오류(타임아웃·연결 끊김)에만 재시도한다. 4xx/5xx 응답은
# 다시 걸어도 같은 답이 오고(인증 오류·이용 제한 등) KIS는 잦은 재발급 자체를
# 제한하므로, 서버가 "대답을 한" 경우엔 재시도하지 않는다.
TOKEN_TIMEOUT_SECONDS = 20
TOKEN_RETRIES = 1
TOKEN_RETRY_WAIT_SECONDS = 2


# 이 프로세스 실행 동안만 유효한 토큰 캐시. GitHub Actions는 실행마다 새
# 컨테이너라 다음 실행으로 새지 않고, 8분짜리 워크플로 안에서는 KIS 토큰
# 유효기간(수 시간)이 끝날 일도 없다. 저장소가 public이라 파일에 남기는
# "영속 캐싱"은 여전히 하지 않는다 - 이건 그것과 다른, 한 실행 안에서의
# 중복 네트워크 호출만 막는 캐싱이다.
#
# 왜 필요한가: intraday_watch.py가 같은 주기 안에서 run_auto_sell()과
# run_auto_trade()를 각각 부르는데, 둘 다 독립적으로 get_access_token()을
# 호출한다. KIS는 짧은 간격의 재발급을 레이트리밋(403)하고 이건 타임아웃이
# 아니라서 재시도 대상도 아니라 그대로 실패한다 - 먼저 실행되는 쪽이 이미
# 성공한 토큰이 있는데도 나중 쪽이 새로 받으려다 걸려서 그 회차를 통째로
# 놓친다(2026-08-19 13:03 "토큰 발급 실패"가 이 충돌로 추정된다).
_cached_token: Optional[str] = None
_cached_token_error: Optional[Exception] = None


def get_access_token() -> str:
    """모의투자 접근토큰을 발급받는다. 토큰 값은 반환만 하고 출력하지 않는다.

    이 프로세스 실행 안에서 두 번째 호출부터는 첫 호출 결과(성공한 토큰
    또는 실패)를 그대로 재사용한다 - 위 _cached_token 설명 참고. 실패를
    캐싱하는 것도 의도적이다: 첫 시도가 레이트리밋으로 실패했다면 같은
    실행 안에서 곧바로 다시 시도해도 같은 응답이 온다.

    KIS는 잦은 재발급 시 이용 제한이 걸릴 수 있는데, 발급 실패·거부를
    조용히 넘어가지 않고 카톡으로 알린 뒤 예외를 올린다 (실행당 1회만).

    타임아웃은 짧게 한 번 재시도한다 (TOKEN_RETRIES). 알림은 재시도까지
    모두 실패했을 때만 보낸다 - 한 번 늦었다가 곧바로 성공한 건 사람이
    알 필요가 없다.
    """
    global _cached_token, _cached_token_error

    if _cached_token is not None:
        return _cached_token
    if _cached_token_error is not None:
        raise _cached_token_error

    app_key = os.environ["KIS_APP_KEY"]
    app_secret = os.environ["KIS_APP_SECRET"]

    last_error: Optional[Exception] = None
    for attempt in range(TOKEN_RETRIES + 1):
        try:
            resp = requests.post(
                f"{BASE_URL}/oauth2/tokenP",
                json={
                    "grant_type": "client_credentials",
                    "appkey": app_key,
                    "appsecret": app_secret,
                },
                timeout=TOKEN_TIMEOUT_SECONDS,
            )
            resp.raise_for_status()
            token = resp.json().get("access_token")
            if not token:
                raise RuntimeError("토큰 발급 응답에 access_token이 없습니다.")
            if attempt:
                logger.info("KIS 접근토큰 발급 성공 (재시도 %d회째)", attempt)
            _cached_token = token
            return token
        except (requests.Timeout, requests.ConnectionError) as e:
            # 서버가 대답을 못 준 경우만 재시도 대상이다.
            last_error = e
            if attempt < TOKEN_RETRIES:
                logger.warning("KIS 접근토큰 발급 지연 (%s) - %d초 뒤 재시도",
                               e, TOKEN_RETRY_WAIT_SECONDS)
                time.sleep(TOKEN_RETRY_WAIT_SECONDS)
                continue
            break
        except Exception as e:              # noqa: BLE001
            last_error = e
            break

    logger.error("KIS 접근토큰 발급 실패: %s", last_error)
    _notify_warning_throttled(
        WARN_TOKEN_FAILED, f"[KIS] ⚠ 접근토큰 발급 실패 - {last_error}")
    _cached_token_error = last_error
    raise last_error


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
    return core.today_kst()


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


# 요일 게이트 - 2026-08-22(토) cron-job.org가 시간대만 보고
# workflow_dispatch를 계속 쏴서(자체적으로 요일을 모른다), 토요일
# 내내 KIS 토큰 발급이 타임아웃나며 경고를 반복해 보낸 사고가 있었다.
# _within_trading_hours()는 시:분만 보고 요일은 안 보므로 주말에도
# 통과된다 - 여기서 따로 막는다. 한 프로세스(=한 회차) 안에서
# run_auto_sell·run_auto_pyramid·run_auto_trade가 모두 불릴 수 있어
# 날짜별로 캐싱해 pykrx 조회를 회차당 최대 1번으로 묶는다.
_trading_day_cache: dict[date, bool] = {}


def _is_trading_day(day: Optional[date] = None) -> bool:
    """오늘이 실제 거래일인가 (토·일·공휴일 전부 포함해서 판단).

    core.last_trading_date()가 pykrx 데이터의 마지막 날짜를 그대로
    쓰므로 별도 공휴일 목록을 유지할 필요가 없다. 조회 자체가
    실패하면 True를 돌려준다(fail-open) - 이 판정은 "낼지 말지"가
    아니라 "KIS를 불러볼지 말지"만 정하는 것이라, 모르면 기존처럼
    시도하고 진짜 장애면 아래 KIS 호출이 실패하며 기존 알림 경로로
    알려진다. 반대로 fail-closed(모르면 건너뜀)로 두면 pykrx만 일시
    장애여도 실제 거래일에 하루 종일 자동매매가 조용히 멈추는 새로운
    장애 유형을 만든다.
    """
    day = day or _today()
    if day not in _trading_day_cache:
        last = core.last_trading_date()
        _trading_day_cache[day] = True if last is None else last == day
    return _trading_day_cache[day]


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


def _check_order_allowed(stock_code: str,
                          order_type: str = notion_repo.ORDER_NEW) -> Optional[str]:
    """중복 주문·반복 거부·일일 상한을 노션 DB 조회로 판단한다.

    order_type에 따라 세는 기준이 달라진다:
      - 신규(ORDER_NEW): 같은 종목 하루 1건. 신규 진입은 종목당 한 번뿐이다.
      - 추가(ORDER_ADD): 같은 종목 하루 MAX_PYRAMID_ADDS_PER_STOCK건까지.
        터틀 피라미딩은 원래 여러 번 나가는 주문이라 boolean으로 막을 수 없다.
    일일 총량도 신규·추가를 각자 예산으로 센다.

    반환: 막힌 이유(사람이 읽을 메시지) 또는 통과라면 None.
    조회 자체가 실패하면 fail-closed로 막는다 (0건으로 간주하지 않음).
    """
    day = _today()
    is_add = order_type == notion_repo.ORDER_ADD
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
        if is_add:
            adds = notion_repo.count_orders_today(
                stock_code, day, order_type=notion_repo.ORDER_ADD)
            if adds >= MAX_PYRAMID_ADDS_PER_STOCK:
                return (f"{stock_code} 오늘 추가매수 {adds}회 - "
                        f"종목당 하루 {MAX_PYRAMID_ADDS_PER_STOCK}회 상한")
        elif notion_repo.has_order_today(stock_code, day,
                                         order_type=notion_repo.ORDER_NEW):
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

        cap = MAX_PYRAMID_ORDERS_PER_DAY if is_add else MAX_ORDERS_PER_DAY
        counts = notion_repo.count_orders_by_status_today(
            day, ACCOUNT_TYPE, order_type=order_type)
        success_n, pending_n = counts.get("성공", 0), counts.get("주문중", 0)
        if success_n + pending_n >= cap:
            logger.info(
                "일일 상한 상세(%s): 성공 %d건 + 주문중 %d건 / 상한 %d건",
                order_type, success_n, pending_n, cap,
            )
            return (
                f"{order_type} 일일 상한 도달 (성공 {success_n}건 + "
                f"주문중 {pending_n}건 / 상한 {cap}건)"
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
                            price: float,
                            order_type: str = notion_repo.ORDER_NEW) -> str:
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
        order_type=order_type,
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
                            ref_price: float = 0.0,
                            order_type: str = notion_repo.ORDER_NEW) -> dict:
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

    blocked_reason = _check_order_allowed(stock_code, order_type)
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
            order_type=order_type,
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


# ── 매도 ────────────────────────────────────────────────────
#
# 매수와 대칭이지만 게이트가 다르다. 매수는 "안 사는 게 안전"이라 자금·
# 일일 상한·중복 방지를 겹겹이 두지만, 매도는 반대로 "못 파는 게 위험"이다.
# 그래서 일일 상한과 자금 게이트를 적용하지 않는다 - 청산은 언제나 나갈 수
# 있어야 한다. 중복 매도만 막으면 되는데, 그건 노션 장부가 아니라 증권사
# 잔고(주문가능수량)로 판단한다 (run_auto_sell 참고).


def _send_market_sell(access_token: str, stock_code: str, qty: int) -> dict:
    """모의투자 시장가 매도 주문을 KIS API로 전송하고 응답 body를 그대로 반환한다.

    _send_market_buy와 본문·헤더 구조가 같고 tr_id만 다르다 (매도 0801U).
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
        "tr_id": ORDER_SELL_TR_ID,
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


def place_market_sell_order(access_token: str, stock_code: str, qty: int,
                             *, name: Optional[str] = None,
                             reason: str = "",
                             ref_price: float = 0.0) -> dict:
    """모의투자 시장가 매도 주문 1건을 시도한다 (청산은 항상 전량).

    기록 순서는 매수와 같다: 사전 기록("주문중", fail-closed) -> 전송 ->
    사후 갱신(fail-open). 사전 기록조차 못 하면 주문하지 않는다 - 판 걸
    모르면 다음 실행이 또 팔 수 있기 때문이다.

    Args:
        reason: 매도 사유 (손절/추세청산). 노션 기록과 카톡에 그대로 쓴다.
        ref_price: 노션 "주문가" 참고용 (시장가라 체결 전엔 정확한 값을 모른다).
    반환: {"status": "sent"|"rejected"|"error"|"blocked", ...}
    """
    display_name = name or stock_code

    try:
        page_id = notion_repo.create_order_record(
            name=display_name, ticker=stock_code, order_no="", qty=qty,
            price=ref_price, status="주문중", reason=reason,
            account_type=ACCOUNT_TYPE, when=datetime.now(KST),
            side=notion_repo.SIDE_SELL,
        )
    except Exception as e:                  # noqa: BLE001
        msg = f"[KIS] 매도 주문 건너뜀 - 사전 기록 실패로 주문 중단: {stock_code} {e}"
        logger.error(msg)
        _notify_failure(msg)
        return {"status": "blocked", "msg": f"사전 기록 실패 (fail-closed): {e}"}

    try:
        result = _send_market_sell(access_token, stock_code, qty)
    except Exception as e:                  # noqa: BLE001
        msg = f"[KIS] 매도 주문 전송 실패 - {stock_code} {qty}주: {e}"
        logger.error(msg)
        _notify_failure(msg)
        _update_order_record(page_id, status="실패", reason=f"{reason} / {e}")
        return {"status": "error", "msg": str(e)}

    if result.get("rt_cd") != "0":
        rejected_msg = result.get("msg1", "알 수 없는 오류")
        msg = f"[KIS] 매도 주문 거부 - {stock_code} {qty}주: {rejected_msg}"
        logger.warning(msg)
        _notify_failure(msg)
        _update_order_record(page_id, status="거부",
                             reason=f"{reason} / {rejected_msg}")
        return {"status": "rejected", "msg": rejected_msg}

    order_no = result["output"]["ODNO"]
    _update_order_record(page_id, status="성공", order_no=order_no, reason=reason)
    _notify_failure(
        f"[KIS] 🔻 자동매도 {display_name}({stock_code}) {qty}주 - {reason}"
    )
    return {"status": "sent", "order_no": order_no}


def _sell_reason(inp, price: float, today: date) -> Optional[str]:
    """이 종목을 지금 팔아야 하는 이유. 없으면 None.

    판정 로직 자체는 core.evaluate_holding()에 있고 아침 배치가 이미 돌려
    노션에 써둔다 - 여기서 그 판정을 다시 구현하지 않는다. 다만 손절선
    이탈만은 장중 현재가로 다시 확인한다 (배치는 전일 종가 기준이라 장중
    급락을 놓친다).

    - 추세청산(10일 저가)은 종가 기준 규칙이라 배치 판정을 그대로 따른다.
    - 손절선은 배치 판정(전일 종가) 또는 장중 현재가, 둘 중 하나만 걸려도
      판다. 손절선은 래칫이라 장중에 값이 내려갈 일이 없다.

    배치 판정은 '오늘 낸 것'만 신뢰한다 - 배치가 못 돈 날의 묵은 판정으로
    팔면 안 되기 때문이다 (checked_date 확인).
    """
    verdict_fresh = inp.checked_date == today
    if verdict_fresh and inp.recent_verdict in ("손절", "추세청산"):
        return f"{inp.recent_verdict} (아침 배치 판정)"

    stop = inp.prev_stop_loss
    if stop and price > 0 and price <= stop:
        return f"손절 (장중 {price:,.0f} ≤ 손절선 {stop:,.0f})"

    return None


def run_auto_sell(holdings: Optional[list[tuple[str, core.HoldingInput]]] = None) -> None:
    """보유 종목의 청산 판정을 실제 매도 주문으로 잇는다.

    intraday_watch.py가 매 실행마다 호출한다. 매수(run_auto_trade)와 달리
    일일 상한·자금 게이트가 없다 - 못 파는 게 더 위험하기 때문이다.

    holdings를 주지 않으면 노션에서 운용="자동"인 행만 직접 읽는다
    (NOTION_DB_ID 필요). (page_id, HoldingInput) 쌍이어야 한다 - 매도가
    체결되면 그 page_id로 보유종목 점검표를 청산 처리한다(아래 참고).
    조회가 실패하면 판정 자체가 불가능하므로 조용히 건너뛴다 - 여기서
    예외를 올리면 호출부의 돌파 감시·알림까지 같이 죽는다.

    운용="자동"으로 좁히는 이유: 매도 자체는 증권사 잔고(주문가능수량)로
    판단하지만, "무엇을 팔지"는 노션에서 가져온다. 여기서 필터링을 안
    하면 같은 티커의 실계좌 수동 보유 종목이 모의계좌의 매도가능수량과
    우연히 매칭돼 판정 대상에 섞여 들어갈 수 있다 - 자동매수가 실제로
    편입한 종목(find_auto_holding_page와 같은 범위)만 본다.

    중복 매도는 증권사 잔고로 막는다: 주문가능수량(sellable)이 0이면 이미
    팔았거나 매도가 미체결로 걸려 있다는 뜻이라 건너뛴다. 노션 장부 대신
    증권사를 원본으로 삼아, 기록이 어긋나도 두 번 팔지 않는다.

    수량은 노션 보유수량이 아니라 실제 주문가능수량을 쓴다 - 둘이 어긋날 때
    (수동 매매·부분 체결) 증권사에 있는 만큼만 파는 게 항상 안전하다.

    매도가 체결되면(청산은 항상 전량이므로 "성공"=완전 청산) 보유종목
    점검표의 구분을 "청산"으로 바꾼다 - 이걸 안 하면 실제로는 팔린
    종목이 노션엔 "보유"로 계속 남아서, 상관군·리스크 리포트가 이미
    없는 포지션을 계속 세고 사람이 앱에서 봐도 매도된 걸 알 길이 없다.
    """
    if not _auto_trade_configured():
        return

    # 시간 게이트를 노션 조회보다 먼저 - 장 밖에선 아무것도 조회하지 않는다.
    if not _within_trading_hours():
        logger.info(
            "거래 시간(%s~%s) 아님 - 자동매도 건너뜀 (현재 %s)",
            TRADE_START_TIME, TRADE_END_TIME, datetime.now(KST).strftime("%H:%M"),
        )
        return

    if not _is_trading_day():
        logger.info("휴장일 - 자동매도 건너뜀 (KIS 호출 생략)")
        return

    if holdings is None:
        try:
            # 운용="자동"만 본다 - 안 그러면 같은 티커의 실계좌 수동 보유
            # 종목이 모의계좌 매도가능수량과 우연히 매칭돼 팔릴 후보에
            # 섞여 들어갈 수 있다 (find_auto_holding_page와 같은 원칙).
            # page_id를 버리지 않는다 - 매도 체결 시 이 행을 청산 처리해야 한다.
            holdings = notion_repo.fetch_holdings(
                managed_by=notion_repo.MANAGED_AUTO,
            )
        except Exception as e:              # noqa: BLE001
            logger.error("보유 종목 조회 실패 - 자동매도를 건너뜁니다: %s", e)
            _notify_warning_throttled(
                WARN_ORDER_STATUS_UNKNOWN,
                "[KIS] ⚠ 보유 종목 조회 실패 — 자동매도 판단 불가로 건너뜀",
            )
            return

    if not holdings:
        return

    today = _today()
    token = get_access_token()

    try:
        balance = get_account_balance(token)
    except Exception as e:                  # noqa: BLE001
        logger.error("잔고조회 실패 - 자동매도를 건너뜁니다: %s", e)
        _notify_warning_throttled(
            WARN_ORDER_STATUS_UNKNOWN,
            "[KIS] ⚠ 잔고조회 실패 — 자동매도 판단 불가로 건너뜀",
        )
        return

    sellable = {h["ticker"]: h["sellable"] for h in balance.get("holdings", [])}

    for page_id, inp in holdings:
        qty = sellable.get(inp.ticker, 0)
        if qty <= 0:
            # 이 계좌에 없거나(수동 보유·다른 계좌) 이미 매도가 걸려 있다.
            continue

        try:
            price = float(get_current_price(token, inp.ticker))
        except Exception as e:              # noqa: BLE001
            logger.warning("[%s] 현재가 조회 실패 - 이번 회차 매도 판정 건너뜀: %s",
                           inp.ticker, e)
            continue

        reason = _sell_reason(inp, price, today)
        if reason is None:
            continue

        result = place_market_sell_order(
            token, inp.ticker, qty, name=inp.name, reason=reason, ref_price=price,
        )
        logger.info("자동매도: %s(%s) %d주 -> %s", inp.name, inp.ticker, qty, result)

        # 체결됐으면(청산은 항상 전량) 보유종목 점검표를 청산 처리한다.
        # 이걸 안 하면 노션엔 계속 "보유"로 남아 상관군·리스크 집계가
        # 이미 없는 포지션을 계속 세고, 사람도 앱에서 매도 여부를 알 수 없다.
        if result.get("status") == "sent":
            try:
                notion_repo.close_auto_holding(page_id)
            except Exception as e:          # noqa: BLE001
                logger.error("[%s] 매도는 체결됐으나 노션 청산 처리 실패: %s",
                             inp.name, e)
                _notify_failure(
                    f"[KIS] ⚠ {inp.name} 매도는 체결됐지만 노션 기록 실패 - "
                    "보유종목 점검표에서 구분을 수동으로 '청산'으로 바꿔주세요"
                )


# ── 추가매수(피라미딩) ──────────────────────────────────────
#
# 터틀은 1유닛 진입 뒤 0.5×ATR 오를 때마다 1유닛씩 MAX_UNITS까지 더한다.
# 신규 진입(run_auto_trade)과 다른 경로인 이유: 돌파 워치리스트는 아직
# 돌파하지 않은 종목(status="임박")만 보므로, 이미 산 종목은 거기 안 나온다.
#
# 안전장치는 신규 진입보다 오히려 촘촘해야 한다 - 10분마다 도는 루프에서
# 같은 유닛을 반복해서 사면 한 종목에 몇 배가 실린다:
#   1) 보유 유닛수를 노션 장부가 아니라 증권사 잔고에서 계산한다. 노션
#      기록이 실패해도(fail-open) 다음 회차가 같은 유닛을 또 사지 않는다.
#   2) place_market_buy_order()를 그대로 재사용한다 - 그 안의 중복 방지
#      (has_order_today)가 "한 종목 하루 1회"를 보장하고, 일일 주문 상한도
#      신규 진입과 같은 예산을 쓴다.
#   3) 다음 매수가를 넘어도 너무 멀리 뛰었으면(PYRAMID_MAX_CHASE_ATR) 사지
#      않는다. 갭 상승에 시장가로 따라붙으면 의도한 레벨과 크게 어긋난다.

# 다음 매수가를 이만큼(×진입시ATR) 넘게 벗어났으면 추격으로 보고 건너뛴다.
PYRAMID_MAX_CHASE_ATR = 0.5


def _pyramid_plan(inp, price: float, unit_shares: int, held_qty: int) -> Optional[dict]:
    """이 종목을 지금 1유닛 더 살지 판단한다. 안 사면 None.

    보유 유닛수는 증권사 잔고(held_qty)에서 역산한다 - 노션 '유닛수' 칸을
    믿지 않는 이유는 위 주석 1) 참고.

    기준가는 진입시 ATR로 고정한다. 매일 갱신되는 현재 ATR을 쓰면 같은
    포지션의 추가매수 레벨이 날마다 움직여, 어제 안 샀던 가격에 오늘
    사는 일이 생긴다.

    반환: {"units_held", "next_price", "qty", "reason"} 또는 None
    """
    atr = inp.entry_atr or inp.notion_atr
    if not atr or atr <= 0 or unit_shares <= 0 or price <= 0:
        return None

    units_held = round(held_qty / unit_shares)
    if units_held < 1 or units_held >= core.MAX_UNITS:
        return None                     # 미보유이거나 이미 만유닛

    # 기준가는 '추가매수 기준가'를 우선한다. 급등으로 창을 지나쳐 재기준한
    # 값이 여기 들어있을 수 있고, 마지막매수가는 그때 안 바뀐다.
    base = inp.pyramid_anchor or inp.last_buy_price or inp.buy_price
    if not base:
        return None
    step = core.PYRAMID_ATR_STEP * atr
    next_price = base + step
    if price < next_price:
        return None                     # 아직 다음 레벨에 도달 안 함

    if price > next_price + PYRAMID_MAX_CHASE_ATR * atr:
        # 10분 사이 여러 레벨을 건너뛸 만큼 급등했다. 나쁜 가격에 따라붙지
        # 않되, 기준가만 현재가 아래의 가장 가까운 레벨로 올려 다음 창을
        # 연다 - 안 그러면 창이 옛 가격에 얼어붙어 주가가 계속 올라도
        # 영영 1유닛에 갇힌다(가장 잘 가는 종목에서 가장 작은 포지션).
        skipped = int((price - base) // step)
        return {
            "action": "reanchor",
            "units_held": units_held,
            "new_anchor": base + skipped * step,
            "reason": (f"급등으로 {skipped}개 레벨 통과 - 매수 없이 기준가만 "
                       f"{base + skipped * step:,.0f}으로 올림"),
        }

    return {
        "action": "buy",
        "units_held": units_held,
        "next_price": next_price,
        "qty": unit_shares,
        "reason": (f"추가매수 {units_held + 1}유닛째 "
                   f"(기준 {next_price:,.0f} · 현재 {price:,.0f})"),
    }


def run_auto_pyramid(holdings: Optional[list] = None) -> None:
    """자동매매로 보유 중인 종목의 터틀 추가매수를 실주문으로 잇는다.

    운용="자동" 행만 대상으로 한다 - 다른 증권사에서 수동으로 들고 있는
    종목까지 KIS에서 사버리면 안 된다.

    상관군·전체 유닛 캡은 신규 진입과 같은 기준(KIS 잔고 기반)으로 본다.
    """
    if not _auto_trade_configured():
        return

    if not _within_trading_hours():
        return

    if not _is_trading_day():
        logger.info("휴장일 - 추가매수 건너뜀 (KIS 호출 생략)")
        return

    if holdings is None:
        try:
            holdings = [
                inp for _, inp in
                notion_repo.fetch_holdings(managed_by=notion_repo.MANAGED_AUTO)
            ]
        except Exception as e:              # noqa: BLE001
            logger.error("보유 종목 조회 실패 - 추가매수를 건너뜁니다: %s", e)
            return

    if not holdings:
        return

    token = get_access_token()
    try:
        balance = get_account_balance(token)
    except Exception as e:                  # noqa: BLE001
        logger.error("잔고조회 실패 - 추가매수를 건너뜁니다: %s", e)
        return

    account_size = balance["account_size"]
    cash_remaining = balance["available_cash"]
    held = {h["ticker"]: h["qty"] for h in balance.get("holdings", [])}
    if not held or account_size <= 0:
        return

    corr = get_mock_account_corr_units(account_size, balance["holdings"])
    group_units = dict(corr["groups"])
    total_units = corr["total_units"]
    sector_map = _get_sector_map()

    for inp in holdings:
        held_qty = held.get(inp.ticker, 0)
        if held_qty <= 0:
            continue


        atr = inp.entry_atr or inp.notion_atr
        if not atr:
            continue
        unit_shares = core.calc_position(atr, account_size).unit_shares

        try:
            price = float(get_current_price(token, inp.ticker))
        except Exception as e:              # noqa: BLE001
            logger.warning("[%s] 현재가 조회 실패 - 추가매수 판정 건너뜀: %s",
                           inp.ticker, e)
            continue

        plan = _pyramid_plan(inp, price, unit_shares, held_qty)
        if plan is None:
            continue

        if plan["action"] == "reanchor":
            # 주문은 내지 않는다. 기준가만 올려 다음 회차에 창이 열리게 한다.
            logger.info("[%s] %s", inp.ticker, plan["reason"])
            try:
                page_id = notion_repo.find_auto_holding_page(inp.ticker)
                if page_id:
                    notion_repo.set_pyramid_anchor(page_id, plan["new_anchor"])
            except Exception as e:      # noqa: BLE001
                logger.warning("[%s] 추가매수 기준가 갱신 실패 - 다음 회차에 "
                               "다시 시도합니다: %s", inp.ticker, e)
            continue

        # 상관군·전체 캡 - 이 종목 자체 상한은 _pyramid_plan이 이미 봤다.
        sector = inp.corr_group or sector_map.get(inp.ticker, "")
        if (group_units.get(sector, 0) + 1 > core.MAX_UNITS_GROUP
                or total_units + 1 > core.MAX_UNITS_TOTAL):
            _notify_failure(f"[KIS] 상관군 캡으로 추가매수 보류: {inp.name}")
            continue

        amount = plan["qty"] * price
        if amount > cash_remaining:
            _notify_failure(f"[KIS] 현금 부족으로 추가매수 보류: {inp.name}")
            continue

        result = place_market_buy_order(
            token, inp.ticker, plan["qty"],
            name=inp.name, ref_price=price,
            order_type=notion_repo.ORDER_ADD,
        )
        logger.info("추가매수: %s(%s) %d유닛째 %d주 -> %s",
                    inp.name, inp.ticker, plan["units_held"] + 1,
                    plan["qty"], result)

        if result.get("status") == "sent":
            _notify_failure(f"[KIS] ➕ {inp.name}({inp.ticker}) {plan['reason']}")
            group_units[sector] = group_units.get(sector, 0) + 1
            total_units += 1
            cash_remaining -= amount
            _record_holding_after_buy(
                token,
                {"ticker": inp.ticker, "name": inp.name, "market": inp.market,
                 "sector": sector, "price": price, "atr20": atr,
                 "unit_shares": plan["qty"]},
                result.get("order_no", ""),
            )


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
    """모의투자 잔고조회 API로 계좌평가액·가용현금·보유종목을 실시간 조회한다.

    반환: {"account_size": 총평가금액, "available_cash": D+2 정산금액,
           "deposit_total": 예수금총금액,
           "holdings": [{"ticker","qty","sellable"}, ...]}  (이 모의계좌 자체 보유분)

    available_cash는 예수금총금액(dnca_tot_amt)이 아니라 가수도정산금액
    (prvs_rcdl_excc_amt = D+2 정산금액)이다. 주식은 T+2 결제라 오늘·어제
    매수한 금액이 예수금에서 아직 안 빠져 있어서, dnca_tot_amt를 가용현금으로
    쓰면 이미 써버린 돈을 또 쓸 수 있다고 착각한다. 2026-08-24 실측:
    dnca_tot_amt 6,264,672원인데 D+2 정산은 -966,123원 - 723만원을 과대평가해
    자금 게이트가 통과시킨 주문마다 KIS가 "주문가능금액이 부족합니다"로
    거부했고, 그때마다 카톡 경고와 종목별 거부 스트라이크만 쌓였다.

    holdings의 sellable은 주문가능수량(ord_psbl_qty)이다. 이미 낸 매도가
    미체결로 걸려 있으면 이 값이 줄어들므로, 자동매도는 hldg_qty가 아니라
    이 값을 보고 "이미 팔았는지"를 판단한다 - 노션 장부가 아니라 증권사
    잔고가 원본이라 중복 매도를 구조적으로 막아준다.
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
        {
            "ticker": h.get("pdno", ""),
            "qty": int(float(h.get("hldg_qty") or 0)),
            "sellable": int(float(h.get("ord_psbl_qty") or 0)),
        }
        for h in data.get("output1") or []
        if int(float(h.get("hldg_qty") or 0)) > 0
    ]
    deposit_total = float(row.get("dnca_tot_amt") or 0)
    # D+2 정산금액이 실제로 쓸 수 있는 현금이다. 미결제 매수가 예수금보다
    # 많으면 음수가 나오므로(=이미 초과 투자) 0으로 깎는다.
    settled = row.get("prvs_rcdl_excc_amt")
    if settled is None:
        # 이 칸이 없는 응답은 본 적이 없다. 그래도 비면 예수금으로 물러서되
        # 과대평가 위험을 로그로 남긴다 (조용히 넘어가면 안 되는 값이다).
        logger.warning("잔고 응답에 가수도정산금액(prvs_rcdl_excc_amt)이 없습니다 "
                       "- 예수금총금액으로 대체합니다 (과대평가 가능)")
        available_cash = deposit_total
    else:
        available_cash = max(0.0, float(settled))

    logger.info("계좌: 평가액 %s원 · 가용현금(D+2) %s원 (예수금총액 %s원)",
                f"{float(row.get('tot_evlu_amt') or 0):,.0f}",
                f"{available_cash:,.0f}", f"{deposit_total:,.0f}")

    return {
        "account_size": float(row.get("tot_evlu_amt") or 0),
        "available_cash": available_cash,
        "deposit_total": deposit_total,
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
    시간 게이트 다음이 요일 게이트(_is_trading_day)다 - 휴장일에도
    이 시:분 범위는 통과하므로 따로 막아야 한다.
    """
    if not candidates:
        return

    if not _auto_trade_configured():
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

    if not _is_trading_day():
        logger.info("휴장일 - 자동매수 건너뜀 (KIS 호출 생략)")
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
        if result.get("status") == "sent":
            _record_holding_after_buy(token, c, result.get("order_no", ""))


def _record_holding_after_buy(access_token: str, c: dict, order_no: str) -> None:
    """자동매수로 산 종목을 보유종목 점검표에 편입한다.

    이 기록이 없으면 아침 배치가 손절선을 갱신하지 않아 트레일링이 멈추고,
    자동매도(run_auto_sell)의 판단 대상에서도 빠진다 - 즉 "사기만 하고 못
    파는" 상태가 된다. 자동매수 배선의 마지막 한 칸이다.

    fail-open이다: 여기서 실패해도 주문 자체를 되돌리지 않는다(이미 체결됐다).
    대신 카톡으로 알려 사람이 노션에 손으로 넣을 수 있게 한다 - 넣지 않으면
    그 종목은 자동매도 대상이 되지 않으므로 반드시 알려야 한다.

    체결가는 주문 직후 조회해 실제 평균단가를 쓴다. 시장가라 돌파 시점
    현재가(ref_price)와 어긋날 수 있는데, 손절선이 이 값에서 나오므로
    가능한 한 실제 체결가를 쓴다. 조회가 안 되면 ref_price로 폴백한다.
    """
    ticker, name = c["ticker"], c["name"]
    buy_price = float(c["price"])
    shares = int(c["unit_shares"])

    try:
        time.sleep(1)  # 체결 처리 대기
        fill = get_order_execution(access_token, order_no) if order_no else None
        if fill and fill.get("avg_price"):
            buy_price = float(fill["avg_price"])
            if fill.get("filled_qty"):
                shares = int(fill["filled_qty"])
    except Exception as e:                  # noqa: BLE001
        logger.warning("[%s] 체결가 조회 실패 - 돌파 시점 현재가로 기록합니다: %s",
                       ticker, e)

    memo = f"자동매수 편입 (주문번호 {order_no}) - 손절선은 다음 아침 배치부터 트레일링"
    try:
        # 운용="자동" 행만 찾는다 - 같은 종목을 다른 증권사에서 수동으로
        # 들고 있어도 그 행에 수량을 더하지 않는다 (계좌가 섞이면 안 된다).
        page_id = notion_repo.find_auto_holding_page(ticker)
        if page_id:
            # 이미 자동으로 보유 중 - 추가매수분으로 누적한다 (새 행 안 만듦).
            notion_repo.add_auto_holding_units(
                page_id, add_shares=shares, buy_price=buy_price,
            )
        else:
            notion_repo.create_auto_holding(
                name=name, ticker=ticker, market=c.get("market", ""),
                buy_price=buy_price, shares=shares, atr=float(c["atr20"]),
                corr_group=c.get("sector", ""), memo=memo,
            )
    except Exception as e:                  # noqa: BLE001
        msg = (f"[KIS] ⚠ {name}({ticker}) 매수는 체결됐으나 노션 편입 실패 - "
               f"손으로 넣지 않으면 자동매도 대상에서 빠집니다: {e}")
        logger.error(msg)
        _notify_failure(msg)


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
