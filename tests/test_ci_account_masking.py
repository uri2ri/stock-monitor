"""Actions 로그에서 계좌번호가 자동으로 가려지는지 검증한다.

배경(2026-11-20): 공개 저장소 `uri2ri/stock-monitor`의 실행 로그에 모의계좌
번호가 남았다(`CANO: 50201912`, 실행 44685725060 - 삭제함). GitHub은 저장소
시크릿 원문은 로그에서 가리지만, `-`를 떼어 만든 CANO처럼 시크릿에서 가공된
값은 가리지 않는다. 그래서 계좌번호를 해석하는 `kis_client`가 Actions 안에서
직접 `::add-mask::`를 건다 - 이후 어떤 코드가 그 값을 찍어도 로그에는 `***`만
남는다.

여기서 검증하는 것:
  1) Actions 안에서 CANO와 원문 계좌가 `::add-mask::`로 등록된다.
  2) import 시점에 이미 걸려 있다(워크플로 단계가 import만 해도 안전).
  3) 로컬 실행·계좌 미설정에서는 아무 것도 찍지 않는다(테스트 로그 오염 없음).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import kis_client

REPO_ROOT = Path(__file__).resolve().parent.parent
ACCOUNT = "51982736-01"
CANO = "51982736"


def test_masks_both_raw_account_and_cano(monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("KIS_ACCOUNT", ACCOUNT)

    kis_client.mask_account_in_ci_logs()

    out = capsys.readouterr().out
    assert f"::add-mask::{ACCOUNT}" in out
    assert f"::add-mask::{CANO}" in out


def test_masks_account_without_dash(monkeypatch, capsys):
    # KIS_ACCOUNT가 "-" 없이 들어오는 형식도 있다(_parse_account 주석 참고).
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("KIS_ACCOUNT", "5198273601")

    kis_client.mask_account_in_ci_logs()

    assert f"::add-mask::{CANO}" in capsys.readouterr().out


def test_no_output_outside_actions(monkeypatch, capsys):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setenv("KIS_ACCOUNT", ACCOUNT)

    kis_client.mask_account_in_ci_logs()

    assert capsys.readouterr().out == ""


def test_no_output_when_account_missing(monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.delenv("KIS_ACCOUNT", raising=False)

    kis_client.mask_account_in_ci_logs()

    assert capsys.readouterr().out == ""


def test_import_in_actions_registers_mask():
    """import만으로 걸려야 한다 - 워크플로 단계가 함수를 부르지 않기 때문."""
    env = dict(os.environ)
    env.update({"GITHUB_ACTIONS": "true", "KIS_ACCOUNT": ACCOUNT})
    env.setdefault("PYTHONPATH", str(REPO_ROOT))
    proc = subprocess.run(
        [sys.executable, "-c", "import kis_client"],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert f"::add-mask::{CANO}" in proc.stdout
    assert f"::add-mask::{ACCOUNT}" in proc.stdout
