"""체결은 확인됐는데 일지가 안 맞으면 일지를 체결 증거에 맞춘다.

배경: 탑코미디어(134580) 매도(주문번호 0000006905, 2026-10-01, 1162주)가
`[KIS] 매도 기록 복구 보류: 옛 포지션 일지/주문 증거 불일치`로 매일 경고를
냈다. 원인은 일지가 **수기로** 작성돼 있었던 것:

  - 청산 메모에 주문번호가 없음(코드가 쓰면 "자동매도 (주문번호 ...)" 형식)
  - ✱ 청산가가 주문가 2,955 ("청산가는 주문가 기준(실제 체결가 미확인)")

이 분기에 도달했다는 것 자체가 **체결은 확인됐다**는 뜻이다 - 그 앞에서
수량 일치와 유효 평균가를 이미 통과했기 때문이다. 안 맞는 건 일지뿐이고,
일지는 기록일 뿐 주문이 아니라 고쳐도 매매 판단에 영향이 없다. 반대로
틀린 채 두면 R배수·승률 통계가 계속 틀어진다.

2건 이상이면 어느 쪽이 이 주문의 것인지 정할 수 없으므로 기존처럼 보류한다.
"""

from __future__ import annotations

from datetime import date
from unittest import mock

import pytest

import kis_client
import notion_repo
from helpers import block_external_http, scrub_credential_env

ORDER = {
    'page_id': 'order-page', 'holding_page_id': 'holding-page',
    'account_type': kis_client.ACCOUNT_TYPE, 'ticker': '134580',
    'order_no': '0000006905', 'qty': 1162, 'order_day': '2026-10-01',
    'reason': '추세청산 (아침 배치 판정)', 'price': 2955,
}
FILL = {'filled_qty': 1162, 'avg_price': 2948.5}
DAY = date(2026, 10, 1)


@pytest.fixture
def env(monkeypatch):
    scrub_credential_env(monkeypatch)
    block_external_http(monkeypatch)
    monkeypatch.setattr(notion_repo, "holding_identity",
                        lambda pid: {'ticker': '134580', 'status': '청산'})
    monkeypatch.setattr(kis_client, "get_order_execution",
                        lambda *a, **k: dict(FILL))
    monkeypatch.setattr(notion_repo, "ledger_matches_sell",
                        lambda *a, **k: False)      # 늘 불일치 상황
    calls: dict = {"created": [], "updated": [], "orders": []}
    monkeypatch.setattr(notion_repo, "create_ledger_record",
                        lambda **kw: calls["created"].append(kw) or "ledger-new")
    monkeypatch.setattr(notion_repo, "update_ledger_record",
                        lambda pid, **kw: calls["updated"].append((pid, kw)))
    monkeypatch.setattr(notion_repo, "update_order_record",
                        lambda pid, **kw: calls["orders"].append((pid, kw)))
    monkeypatch.setattr(notion_repo, "fetch_holding_for_ledger", lambda pid: {
        'name': '탑코미디어', 'ticker': '134580', 'market': 'KOSDAQ',
        'entry_price': 2282.5, 'entry_atr': 168.0, 'units': 2.0,
        'entry_date': date(2026, 8, 21), 'signal_date': None,
    })
    return calls


# ── 일지 없음 → 생성 ────────────────────────────────────────

def test_creates_ledger_when_missing(env, monkeypatch):
    monkeypatch.setattr(notion_repo, "fetch_ledger_rows_for_holding",
                        lambda pid, **kw: [])

    assert kis_client._recover_closed_sell("tok", dict(ORDER)) is True

    assert len(env["created"]) == 1
    row = env["created"][0]
    assert row["shares"] == 1162
    assert row["exit_price"] == 2948.5          # 체결가
    assert row["exit_date"] == DAY
    assert row["holding_page_id"] == "holding-page"
    assert "주문번호 0000006905" in row["memo"]
    # 신호 발생일이 없으면 체결일로 - 지연 0
    assert row["signal_date"] == DAY
    assert row["entry_date"] == date(2026, 8, 21)
    assert env["updated"] == []
    # 주문 상태는 성공으로 갱신된다
    assert env["orders"][0][1]["status"] == "성공"


def test_creation_held_when_entry_price_unknown(env, monkeypatch):
    """진입가를 모르면 만들지 않는다 - 빈 값으로 통계를 오염시키지 않는다."""
    monkeypatch.setattr(notion_repo, "fetch_ledger_rows_for_holding",
                        lambda pid, **kw: [])
    monkeypatch.setattr(notion_repo, "fetch_holding_for_ledger", lambda pid: {
        'name': '탑코미디어', 'ticker': '134580', 'market': 'KOSDAQ',
        'entry_price': None, 'entry_atr': None, 'units': None,
        'entry_date': None, 'signal_date': None,
    })

    with pytest.raises(ValueError, match="진입가 확인 불가"):
        kis_client._recover_closed_sell("tok", dict(ORDER))

    assert env["created"] == []
    assert env["orders"] == []                  # 주문 상태도 안 건드린다


# ── 수기 일지 1건 → 덮어쓰되 원문 보존 ──────────────────────

def test_overwrites_single_manual_ledger_and_keeps_old_memo(env, monkeypatch):
    manual = ("자동매매(모의). 9/30 종가 2,850 ≤ 손절선 2,902 이탈 → 10/1 "
              "자동매도. 청산가는 주문가 기준(실제 체결가 미확인)")
    monkeypatch.setattr(notion_repo, "fetch_ledger_rows_for_holding",
                        lambda pid, **kw: [{
                            'page_id': 'ledger-1', 'memo': manual,
                            'shares': 1162.0, 'exit_price': 2955.0,
                            'exit_date': DAY}])

    assert kis_client._recover_closed_sell("tok", dict(ORDER)) is True

    assert env["created"] == []
    assert len(env["updated"]) == 1
    pid, kw = env["updated"][0]
    assert pid == "ledger-1"
    assert kw["shares"] == 1162
    assert kw["exit_price"] == 2948.5           # 주문가 2955 → 체결가로
    assert kw["exit_date"] == DAY
    assert "주문번호 0000006905" in kw["memo"]   # 대조 조건을 만족하게 된다
    # 기존 수기 메모를 지우지 않는다 - 왜 그렇게 적었는지가 단서다.
    assert "이전 수기 기록:" in kw["memo"]
    assert manual in kw["memo"]
    assert env["orders"][0][1]["status"] == "성공"


def test_empty_old_memo_adds_no_prefix(env, monkeypatch):
    monkeypatch.setattr(notion_repo, "fetch_ledger_rows_for_holding",
                        lambda pid, **kw: [{
                            'page_id': 'ledger-1', 'memo': '',
                            'shares': None, 'exit_price': None,
                            'exit_date': None}])

    kis_client._recover_closed_sell("tok", dict(ORDER))

    _, kw = env["updated"][0]
    assert "이전 수기 기록" not in kw["memo"]


# ── 2건 이상 → 보류 ────────────────────────────────────────

def test_holds_when_multiple_ledgers(env, monkeypatch):
    monkeypatch.setattr(notion_repo, "fetch_ledger_rows_for_holding",
                        lambda pid, **kw: [
                            {'page_id': 'l1', 'memo': 'a', 'shares': 1162.0,
                             'exit_price': 2955.0, 'exit_date': DAY},
                            {'page_id': 'l2', 'memo': 'b', 'shares': 1162.0,
                             'exit_price': 2900.0, 'exit_date': DAY},
                        ])

    with pytest.raises(ValueError, match="2건"):
        kis_client._recover_closed_sell("tok", dict(ORDER))

    assert env["created"] == [] and env["updated"] == []
    assert env["orders"] == []                  # 주문 상태도 안 건드린다


# ── 앞 단계 보호막은 그대로 ─────────────────────────────────

def test_unconfirmed_fill_still_raises_before_touching_ledger(env, monkeypatch):
    """체결이 확인 안 되면 일지를 손대기 전에 멈춘다 - 이 변경의 전제."""
    monkeypatch.setattr(kis_client, "get_order_execution", lambda *a, **k: None)
    rows = mock.Mock(side_effect=AssertionError("일지까지 갔다"))
    monkeypatch.setattr(notion_repo, "fetch_ledger_rows_for_holding", rows)

    with pytest.raises(ValueError, match="매도 체결 미확인"):
        kis_client._recover_closed_sell("tok", dict(ORDER))

    rows.assert_not_called()
    assert env["orders"] == []


def test_quantity_mismatch_still_raises(env, monkeypatch):
    monkeypatch.setattr(kis_client, "get_order_execution",
                        lambda *a, **k: {'filled_qty': 1000, 'avg_price': 2948.5})
    rows = mock.Mock(side_effect=AssertionError("일지까지 갔다"))
    monkeypatch.setattr(notion_repo, "fetch_ledger_rows_for_holding", rows)

    with pytest.raises(ValueError, match="매도 체결 미확인"):
        kis_client._recover_closed_sell("tok", dict(ORDER))
    rows.assert_not_called()


def test_matching_ledger_skips_repair(env, monkeypatch):
    """이미 맞으면 아무것도 고치지 않는다 - 기존 동작 그대로."""
    monkeypatch.setattr(notion_repo, "ledger_matches_sell", lambda *a, **k: True)
    rows = mock.Mock(side_effect=AssertionError("고치려 들었다"))
    monkeypatch.setattr(notion_repo, "fetch_ledger_rows_for_holding", rows)

    assert kis_client._recover_closed_sell("tok", dict(ORDER)) is True

    rows.assert_not_called()
    assert env["orders"][0][1]["status"] == "성공"
