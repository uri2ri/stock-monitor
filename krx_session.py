"""pykrx import 시 KRX 로그인 실패가 프로세스를 죽이지 않게 감싼다.

pykrx는 `pykrx.website.comm.webio`를 import하는 시점에 모듈 최상위에서
`build_krx_session()`을 호출해 KRX에 로그인한다. 그런데 그 안의
`login_krx()`는 응답을 검증 없이 `resp.json()`으로 파싱해서, KRX가 JSON이
아닌 것(점검 안내 페이지·게이트웨이 오류 HTML 등)을 돌려주면
JSONDecodeError가 그대로 import 밖으로 올라온다. 그러면 `import core`
한 줄에서 프로세스가 죽는다 - 이쪽 코드가 한 줄도 실행되기 전에.

2026-09-18 야간 전종목 스캔([run 35367705949](
https://github.com/uri2ri/stock-monitor/actions/runs/35367705949))이 정확히
이렇게 끝났다:

    File ".../stock-monitor/scan_all.py", line 35, in <module>
        import core
    File ".../stock-monitor/core.py", line 24, in <module>
        from pykrx import stock as krx
    ...
    File ".../pykrx/website/comm/webio.py", line 12, in <module>
        _session = build_krx_session()
    File ".../pykrx/website/comm/auth.py", line 153, in login_krx
        data = resp.json()
    requests.exceptions.JSONDecodeError: Expecting value: line 1 column 1 (char 0)

스캔이 한 줄도 돌지 못해 `data/scan_latest.csv`가 09-17 기준으로 남았고,
다음 거래일 장중 감시가 "야간 스캔 기준일이 오래됐습니다"로 신규 진입을
종일 보류했다(억제 창 1시간마다 카톡 반복).

로그인은 **선택 사항**이다 - 같은 실행의 즐겨찾기 배치는 KRX_ID/KRX_PW
없이(익명 세션) 12종목 시세를 정상 조회하고 끝났다. 그래서 로그인 단계에서
터지면 자격증명을 빼고 다시 import해 익명 세션으로 내려간다.

"조용히 익명으로 내려가면 데이터가 비어도 모르는 것 아닌가"에 대한 답:
익명 세션으로도 시세를 못 받으면 `scan_all`이 `KrxUnavailable`("요청 구간에
시세가 한 건도 없습니다" / "계산에 성공한 종목이 없습니다")로 실패한다 -
빈 값이나 옛 기준선으로 CSV를 덮어쓰는 경로는 없다. 즉 이 대체는 "성공할
수도 있는 경로를 한 번 더 시도"하는 것이지 검증을 건너뛰는 게 아니다.
그래도 어느 쪽으로 떨어졌는지는 `anonymous_fallback`과 경고 로그로 남긴다.

사용법: `from pykrx import stock as krx` 대신
        `from krx_session import stock as krx`.
pykrx를 직접 import하는 경로가 하나라도 남아 있고 그쪽이 먼저 실행되면
이 보호를 우회하므로, 최상위 import는 전부 이 모듈을 지나게 한다.
"""

from __future__ import annotations

import logging
import os
import sys

logger = logging.getLogger(__name__)

# 로그인 없이(익명 세션으로) pykrx를 올렸으면 True. 첫 import가 바로
# 성공했으면 False. 진단·로그용이며 계산에는 쓰지 않는다.
anonymous_fallback = False

_CREDENTIAL_ENV = ("KRX_ID", "KRX_PW")


def _default_importer():
    from pykrx import stock
    return stock


def _purge_pykrx_modules() -> None:
    """실패한 import가 sys.modules에 남긴 pykrx 흔적을 지운다.

    지우지 않으면 재import가 반쪽짜리 모듈을 그대로 돌려줘 로그인 단계를
    다시 타지 않는다(= 익명 대체가 아무 일도 하지 못한다).
    """
    for name in [n for n in sys.modules if n == "pykrx" or n.startswith("pykrx.")]:
        del sys.modules[name]


def _load(importer=None):
    """pykrx.stock 모듈을 돌려준다. 로그인 실패면 익명으로 한 번 더 시도한다.

    importer는 테스트 주입용이다 - 실제 import를 대신하는 호출가능 객체.
    """
    global anonymous_fallback
    importer = importer or _default_importer

    try:
        return importer()
    except Exception as e:                  # noqa: BLE001
        if not all(os.environ.get(k) for k in _CREDENTIAL_ENV):
            # 자격증명이 없으면 pykrx는 로그인 단계를 타지 않는다
            # (build_krx_session이 바로 None을 돌려준다). 그러니 이 실패는
            # 로그인과 무관한 다른 이유이고, 익명으로 다시 해봐야 같은
            # 곳에서 똑같이 터진다. 원인을 바꿔치기하지 말고 그대로 올린다.
            raise
        logger.warning(
            "pykrx import 중 KRX 로그인이 실패했습니다 (%s: %s) - "
            "자격증명 없이 익명 세션으로 다시 시도합니다.",
            type(e).__name__, e,
        )

    _purge_pykrx_modules()
    # build_krx_session()의 기본 인자가 모듈 import 시점의 os.getenv() 값이라,
    # 환경변수를 지운 뒤 다시 import해야 로그인 단계를 건너뛴다.
    saved = {k: os.environ.pop(k) for k in _CREDENTIAL_ENV if k in os.environ}
    try:
        stock = importer()
    finally:
        # 이 프로세스의 다른 코드가 KRX 자격증명을 볼 수 있어야 한다 -
        # import 한 번 때문에 환경을 영구히 바꾸지 않는다.
        os.environ.update(saved)

    anonymous_fallback = True
    logger.warning(
        "KRX 익명 세션으로 진행합니다 - 로그인이 필요한 조회는 실패할 수 있습니다. "
        "시세를 한 건도 못 받으면 스캔이 KrxUnavailable로 실패하므로, "
        "낡은 기준선이 그대로 쓰이는 일은 없습니다.",
    )
    return stock


stock = _load()
