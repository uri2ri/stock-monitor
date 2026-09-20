"""KRX 로그인이 실패해도 pykrx import가 프로세스를 죽이면 안 된다.

2026-09-18 야간 전종목 스캔(run 35367705949)이 `import core` 한 줄에서
`requests.exceptions.JSONDecodeError`로 죽었다. pykrx가 import 시점에
KRX 로그인을 하는데, KRX가 JSON이 아닌 응답(점검 페이지 등)을 주면
`login_krx()`의 `resp.json()`이 그대로 터지기 때문이다. 스캔이 한 줄도
못 돌아 scan_latest.csv가 하루 낡았고, 다음 거래일 장중 감시가 종일
"야간 스캔 기준일이 오래됐습니다"로 신규 진입을 보류했다.

여기서 검증하는 것은 `krx_session._load()`의 **분기 규칙**이다:
로그인이 터지면 자격증명을 빼고 한 번 더 import하고, 그 사이에만
환경변수를 지우며, 로그인과 무관한 실패는 삼키지 않는다.

검증하지 **않는** 것: 실제 pykrx가 정말 저 지점에서 터지는지, 익명
세션이 실제 KRX 조회에 충분한지. 앞은 운영 로그의 트레이스백이 근거이고,
뒤는 같은 실행의 즐겨찾기 배치가 자격증명 없이 12종목을 조회한 사실이
근거다 - 둘 다 이 테스트가 만드는 증거가 아니다(네트워크를 타야 한다).
"""

from __future__ import annotations

import pytest

import krx_session


class _FakeStock:
    """pykrx.stock 자리에 들어갈 표식. 동작은 필요 없다."""


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    # 실제 sys.modules의 pykrx를 건드리지 않는다 - 다른 테스트가 이미
    # 올려둔 모듈을 이 테스트가 지워버리면 안 된다.
    purged = []
    monkeypatch.setattr(krx_session, "_purge_pykrx_modules",
                        lambda: purged.append(True))
    # 모듈 전역 플래그는 import 시점 값이라 테스트마다 초기화한다.
    monkeypatch.setattr(krx_session, "anonymous_fallback", False)
    return purged


def _with_credentials(monkeypatch):
    monkeypatch.setenv("KRX_ID", "krx-id-fake")
    monkeypatch.setenv("KRX_PW", "krx-pw-fake")


def test_login_failure_falls_back_to_anonymous_import(monkeypatch, _isolate):
    """로그인이 터지면 자격증명을 빼고 다시 import해서 살아남는다."""
    _with_credentials(monkeypatch)
    seen_env = []
    stock = _FakeStock()

    import os

    def importer():
        seen_env.append((os.environ.get("KRX_ID"), os.environ.get("KRX_PW")))
        if len(seen_env) == 1:
            # 운영에서 실제로 난 예외와 같은 종류(JSON 파싱 실패).
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return stock

    assert krx_session._load(importer) is stock
    # 1회차는 자격증명을 본 채, 2회차는 지워진 채 호출돼야 한다.
    assert seen_env == [("krx-id-fake", "krx-pw-fake"), (None, None)]
    assert krx_session.anonymous_fallback is True
    # 반쪽짜리 pykrx가 sys.modules에 남으면 재import가 로그인을 안 탄다.
    assert _isolate == [True]


def test_credentials_are_restored_after_fallback(monkeypatch):
    """익명 재시도는 그 import 동안만 환경을 비운다 - 영구 변경 금지."""
    import os

    _with_credentials(monkeypatch)
    monkeypatch.setattr(krx_session, "_purge_pykrx_modules", lambda: None)

    calls = []

    def importer():
        calls.append(1)
        if len(calls) == 1:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return _FakeStock()

    krx_session._load(importer)

    assert os.environ.get("KRX_ID") == "krx-id-fake"
    assert os.environ.get("KRX_PW") == "krx-pw-fake"


def test_failure_without_credentials_is_not_swallowed(monkeypatch, _isolate):
    """자격증명이 없으면 로그인 단계를 탈 일이 없다 = 다른 원인이다.

    익명으로 다시 해봐야 같은 곳에서 똑같이 터진다. 원인을 "로그인 실패"로
    바꿔치기하지 말고 그대로 올려야 한다(예: pykrx 미설치).
    """
    monkeypatch.delenv("KRX_ID", raising=False)
    monkeypatch.delenv("KRX_PW", raising=False)
    calls = []

    def importer():
        calls.append(1)
        raise ModuleNotFoundError("No module named 'pykrx'")

    with pytest.raises(ModuleNotFoundError):
        krx_session._load(importer)

    assert calls == [1]                     # 재시도하지 않았다
    assert krx_session.anonymous_fallback is False
    assert _isolate == []                   # sys.modules를 건드리지 않았다


def test_partial_credentials_are_not_treated_as_login_failure(monkeypatch, _isolate):
    """ID만 있고 PW가 없으면 pykrx는 로그인을 시도하지 않는다."""
    monkeypatch.setenv("KRX_ID", "krx-id-fake")
    monkeypatch.delenv("KRX_PW", raising=False)

    def importer():
        raise ValueError("Expecting value: line 1 column 1 (char 0)")

    with pytest.raises(ValueError):
        krx_session._load(importer)
    assert krx_session.anonymous_fallback is False


def test_successful_import_does_not_retry_or_touch_env(monkeypatch, _isolate):
    """정상 경로에서는 아무것도 달라지지 않는다."""
    _with_credentials(monkeypatch)
    stock = _FakeStock()
    calls = []

    def importer():
        calls.append(1)
        return stock

    assert krx_session._load(importer) is stock
    assert calls == [1]
    assert krx_session.anonymous_fallback is False
    assert _isolate == []


def test_anonymous_retry_failure_propagates(monkeypatch, _isolate):
    """익명 재시도까지 실패하면 그 예외를 올린다 - 조용히 넘기지 않는다."""
    _with_credentials(monkeypatch)
    import os

    def importer():
        raise RuntimeError(f"boom (KRX_ID={os.environ.get('KRX_ID')})")

    with pytest.raises(RuntimeError, match="KRX_ID=None"):
        krx_session._load(importer)

    # 실패해도 환경은 원래대로 돌려놓는다.
    assert os.environ.get("KRX_ID") == "krx-id-fake"
