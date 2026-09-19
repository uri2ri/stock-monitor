"""장중 자동매수 파이프라인(scan_all.py/intraday_watch.py/kis_client.py)
회귀 테스트 공용 헬퍼. 실 네트워크(KRX/KIS/노션/카카오/네이버) 호출 없이
합성 데이터 + monkeypatch로만 검증한다.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import requests

import screener


# .env.example에 있는 키 전부. 테스트가 시작될 때 이 값들을 환경에서
# 지워, 실제 .env·CI 시크릿이 들어 있는 머신에서도 테스트가 진짜
# 자격증명을 읽지 못하게 한다.
CREDENTIAL_ENV_VARS = (
    "NOTION_TOKEN", "NOTION_DB_ID", "NOTION_ORDERS_DB_ID",
    "NOTION_TOKEN_CACHE_DB_ID", "NOTION_LEDGER_DB_ID",
    "NOTION_AUTO_TRADE_CONTROL_DB_ID", "NOTION_BREAKOUT_TRACK_DB_ID",
    "KAKAO_REST_API_KEY", "KAKAO_CLIENT_SECRET", "KAKAO_REFRESH_TOKEN",
    "GMAIL_ADDRESS", "GMAIL_APP_PASSWORD", "GMAIL_TO",
    "KIS_APP_KEY", "KIS_APP_SECRET", "KIS_ACCOUNT",
    "TOTAL_CAPITAL",
)

# requests의 모듈 수준 진입점. 코드가 쓰는 건 get/post 정도지만, 차단은
# 전 메서드에 걸어 새 경로가 생겨도 테스트에서 실 호출로 새지 않게 한다.
_REQUESTS_ENTRYPOINTS = (
    "request", "get", "post", "put", "patch", "delete", "head", "options",
)


def scrub_credential_env(monkeypatch) -> None:
    """실제 자격증명을 테스트 프로세스의 환경에서 제거한다.

    주의: kis_client·notion_repo는 import 시점에 load_dotenv()를 한 번
    부르고, 그 import는 테스트 픽스처보다 먼저 일어난다 - 즉 "`.env`
    로딩 자체를 막는" 건 픽스처로는 불가능하다. 대신 로딩된 결과를
    여기서 지운다. 자격증명은 전부 함수 호출 시점에 os.environ에서
    읽히므로(모듈 상수로 굳지 않는다) 이걸로 실제 값이 쓰일 여지가
    없어진다. 값을 읽거나 출력하지 않고 지우기만 한다.
    """
    for name in CREDENTIAL_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def block_external_http(monkeypatch) -> None:
    """테스트 중 실 HTTP 요청을 모든 메서드에서 차단한다.

    requests의 모듈 수준 함수(get/post/put/patch/delete/head/options/
    request)와 그 아래 Session.request를 전부 막는다 - Session을 직접
    만들어 쓰는 경로나 아직 mock하지 않은 메서드로 새 나가면 조용히
    네트워크를 타는 대신 AssertionError로 즉시 드러난다.

    이 프로젝트의 모듈들(kis_client/notion_repo/kakao)은 같은 requests
    모듈 객체를 공유하므로 한 번만 걸면 전부에 적용된다. 필요한 조회
    응답만 개별 테스트가 해당 메서드를 mock해서 열어준다.
    """
    def _blocked(name):
        def _raise(*args, **kwargs):
            raise AssertionError(
                f"테스트가 실 HTTP 요청을 시도했습니다: requests.{name}")
        return _raise

    for name in _REQUESTS_ENTRYPOINTS:
        monkeypatch.setattr(requests, name, _blocked(name), raising=False)

    def _blocked_session_request(self, method, url, *args, **kwargs):
        raise AssertionError(
            f"테스트가 실 HTTP 요청을 시도했습니다: Session.request({method})")

    monkeypatch.setattr(requests.sessions.Session, "request",
                        _blocked_session_request)


def mock_trading_days(monkeypatch, expected_date: date) -> None:
    """screener.trading_days()를 '직전 거래일 = expected_date' 하나로 고정한다.

    intraday_watch._expected_trading_day()는 screener.trading_days(끝날짜, 1)로
    딱 하나만 요청하고 리스트의 마지막 원소만 쓴다 - 실제 KRX 호출 없이
    (원격 환경 제약) 이 반환값만 통제하면 신선도 판정을 원하는 대로
    재현할 수 있다. 어떤 (end, count)로 불려도 같은 값을 준다 - 이
    헬퍼를 쓰는 테스트들은 달력 자체의 범위 로직이 아니라 "직전 거래일이
    이 날짜다"라는 결과만 필요로 하기 때문이다(범위·휴장 로직 자체를
    검증하는 테스트는 test_bug4에서 별도로 직접 monkeypatch한다).
    """
    monkeypatch.setattr(
        screener, "trading_days",
        lambda end, count: [expected_date.strftime("%Y%m%d")],
    )


def make_hist(n_flat: int = 20, flat_close: float = 10_000.0,
              half_spread: float = 300.0, last_close: float = 10_500.0,
              last_high: float = 10_600.0, last_low: float = 10_000.0,
              volume: float = 5_000_000.0) -> pd.DataFrame:
    """scan_row()가 기대하는 형태(날짜·티커 없이 시가/고가/저가/종가/거래량만)의
    합성 OHLCV. 마지막 한 행만 당일 고가가 그 전 20일 고가보다 높게
    잡아, high_20(당일 포함)과 high_20_prev(당일 제외)가 서로 다른
    값으로 갈리게 한다 - 이게 bug1(high20 vs high20_next)의 핵심 조건.
    """
    rows = []
    for _ in range(n_flat):
        rows.append({
            "시가": flat_close, "고가": flat_close + half_spread,
            "저가": flat_close - half_spread, "종가": flat_close,
            "거래량": volume,
        })
    rows.append({
        "시가": last_close, "고가": last_high, "저가": last_low,
        "종가": last_close, "거래량": volume,
    })
    idx = pd.bdate_range("2020-01-02", periods=len(rows))
    return pd.DataFrame(rows, index=idx)
