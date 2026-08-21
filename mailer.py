"""
mailer.py – Gmail SMTP로 상세 리포트 메일 발송

daily_report.py에서는 send_report_mail() 하나만 호출한다.
나중에 차트를 붙일 때는 _build_html()에 <img src="cid:..."> 를 넣고
send_report_mail()에서 msg.add_related(...)로 이미지를 첨부하면 된다.

필요 환경변수:
    GMAIL_ADDRESS       보내는 계정 (예: me@gmail.com)
    GMAIL_APP_PASSWORD  Gmail 앱 비밀번호 16자리
    GMAIL_TO            받는 사람 (쉼표로 여러 명, 없으면 GMAIL_ADDRESS)
"""

from __future__ import annotations

import html
import logging
import os
import smtplib
from datetime import date
from email.message import EmailMessage
from typing import Optional, Sequence

from core import HoldingInput, HoldingResult, PortfolioRisk, build_stock_link

logger = logging.getLogger(__name__)

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587
SMTP_TIMEOUT = 30

# (HoldingInput, HoldingResult) 한 쌍이 표의 한 행이 된다.
ReportRow = tuple[HoldingInput, HoldingResult]

# 상단 요약표 – 한눈에 훑는 용도. 자세한 값은 종목별 카드에 있다.
COLUMNS = (
    "종목명",
    "판정",
    "현재가",
    "손익률",
    "손절선 여유%",
    "청산선 여유%",
    "R배수",
    "평가손익",
)

EMPTY = "-"                 # 노션 값이 비어 있을 때 (추측해서 채우지 않는다)
NO_NEWS = "수집 없음"        # 공시·뉴스만 별도 문구

# '운용' 칸이 이 값이면 터틀 진입이 아니다 (하이닉스 등 별도 전략 보유분).
# R배수는 터틀 손절 로직(2×ATR)을 전제로 하므로 이 종목들에는 의미가
# 없다 - 표에서 분리하고 R_EMPTY로 표시한다.
MANAGED_NON_TURTLE = "터틀외"
R_EMPTY = "—"                # R배수 계산 불가 (진입시 ATR 없음·터틀외)

# ── 색상 ────────────────────────────────────────────────────
C_BORDER = "#d5d5d5"
C_HEAD_BG = "#f2f4f7"
C_STOP_BG = "#ffe3e3"      # 손절 행 배경
C_STOP_TEXT = "#c92a2a"
C_PROFIT = "#c92a2a"       # 국내 관행: 상승 빨강
C_LOSS = "#1c7ed6"         # 하락 파랑
C_MUTED = "#868e96"
C_EXIT_BG = "#fff4e6"      # 철수신호 요약 박스
C_EXIT_LINE = "#e8590c"
C_CARD_BG = "#fafafa"      # 카드 배경
C_LABEL = "#495057"        # 카드 안 항목 이름

# 판정별 배지 색 (배경, 글자)
VERDICT_BADGE = {
    "손절": ("#ffe3e3", "#c92a2a"),
    "추세청산": ("#fff4e6", "#e8590c"),
    "기한도래": ("#fff9db", "#e67700"),
    "유지": ("#ebfbee", "#2b8a3e"),
    "조회실패": ("#f1f3f5", "#868e96"),
}


# ── 값 계산/포맷 유틸 ───────────────────────────────────────

def _fmt(value: Optional[float], digits: int = 0) -> str:
    """숫자를 천단위 구분 문자열로. 값이 없으면 '-'."""
    if value is None:
        return "-"
    return f"{value:,.{digits}f}"


def _fmt_pct(value: Optional[float], digits: int = 2) -> str:
    if value is None:
        return "-"
    return f"{value:+.{digits}f}%" if digits else f"{value:+.0f}%"


def _profit_pct(inp: HoldingInput, res: HoldingResult) -> Optional[float]:
    """매수단가 대비 손익률(%)."""
    if not inp.buy_price or not res.current_price:
        return None
    return (res.current_price - inp.buy_price) / inp.buy_price * 100


def _stop_room_pct(res: HoldingResult) -> Optional[float]:
    """현재가에서 손절선까지 남은 여유(%). 음수면 이미 손절선 아래."""
    if not res.current_price or not res.stop_loss:
        return None
    return (res.current_price - res.stop_loss) / res.current_price * 100


def _is_stop(res: HoldingResult) -> bool:
    return res.verdict == "손절"


def _is_non_turtle(inp: HoldingInput) -> bool:
    return inp.managed_by == MANAGED_NON_TURTLE


def _r_multiple(inp: HoldingInput, res: HoldingResult) -> Optional[float]:
    """R배수 = (현재가 − 매수단가) / (2 × 진입시 ATR).

    분모는 반드시 ✱진입시 ATR(inp.entry_atr)이다 - 매일 갱신되는 현재
    ATR(res.atr)을 쓰면 변동성이 커진 종목일수록 R이 실제보다 작게
    나온다. 진입시 ATR이 없으면(신규 편입 직후 등) None - 계산하지
    않고 호출부에서 '—'로 표시한다.
    """
    if not inp.entry_atr or not res.current_price:
        return None
    return (res.current_price - inp.buy_price) / (2 * inp.entry_atr)


def _pl_amount(inp: HoldingInput, res: HoldingResult) -> Optional[float]:
    """평가손익 = (현재가 − 매수단가) × 보유수량."""
    if not res.current_price:
        return None
    return (res.current_price - inp.buy_price) * inp.shares


def _fmt_r(value: Optional[float]) -> str:
    return f"{value:+.2f}R" if value is not None else R_EMPTY


def _fmt_signed(value: Optional[float]) -> str:
    return f"{value:+,.0f}" if value is not None else EMPTY


def _linked(name: str, ticker: str, *, color: str = "", bold: bool = False) -> str:
    """종목명. STREAMLIT_APP_URL이 설정돼 있으면 종목분석 딥링크로 감싼다.

    카톡과 core.build_stock_link()를 공유한다 (중복 구현 금지). 링크가
    없으면(빈 문자열) 기존처럼 그냥 텍스트로 표시한다. 보유종목·즐겨찾기
    섹션이 이 함수를 함께 쓴다.
    """
    escaped = html.escape(name)
    style = f"color:{color};" if color else ""
    if bold:
        style += "font-weight:bold;"

    link = build_stock_link(ticker)
    if link:
        link_style = (style or "color:inherit;") + "text-decoration:underline;"
        return f'<a href="{html.escape(link)}" style="{link_style}">{escaped}</a>'
    if style:
        return f'<span style="{style}">{escaped}</span>'
    return escaped


def _linked_name(inp: HoldingInput, *, color: str = "", bold: bool = False) -> str:
    return _linked(inp.name, inp.ticker, color=color, bold=bold)


def _or_dash(value: str) -> str:
    """노션 값이 비어 있으면 '-'. 절대 추측해서 채우지 않는다."""
    return (value or "").strip() or EMPTY


def _pyramid_line(res: HoldingResult) -> str:
    """현재 유닛 / 다음 추가매수 지점 한 줄."""
    units = f"{res.current_units:g}"
    if res.next_add_price is None:
        return f"{units}/4 · 추가매수 종료"

    gap = ""
    if res.current_price:
        pct = (res.next_add_price - res.current_price) / res.current_price * 100
        gap = f" (현재가 대비 {pct:+.1f}%)"
    return f"{units}/4 · 다음 추가 지점 {_fmt(res.next_add_price)}원{gap}"


def _sorted_rows(rows: Sequence[ReportRow]) -> list[ReportRow]:
    """조치 필요 종목을 위로. 그 안에서는 원래 순서를 유지한다."""
    return sorted(rows, key=lambda r: not r[1].is_action_needed)


def _split_turtle_rows(
    rows: Sequence[ReportRow],
) -> tuple[list[ReportRow], list[ReportRow]]:
    """(터틀 대상, 터틀외)로 나눈다. 터틀외는 '운용'='터틀외'인 종목."""
    turtle = [r for r in rows if not _is_non_turtle(r[0])]
    non_turtle = [r for r in rows if _is_non_turtle(r[0])]
    return turtle, non_turtle


def _sorted_turtle_rows(rows: Sequence[ReportRow]) -> list[ReportRow]:
    """1차: 기존 판정 그룹(조치 필요 먼저). 2차: 그룹 내 R배수 내림차순.

    전체를 R배수로 재정렬하면 안 된다 - 액션이 필요한 종목(손절·추세청산
    등)이 표 아래로 밀려나 눈에 안 띄게 된다. 그룹 순서는 그대로 두고
    그룹 안에서만 R배수로 정렬한다. R배수가 없는 종목(진입시 ATR
    미기록)은 값을 0 취급하지 않고 그 그룹의 맨 뒤로 보낸다.
    """
    def key(row: ReportRow):
        inp, res = row
        r = _r_multiple(inp, res)
        return (not res.is_action_needed, r is None, -(r or 0.0))

    return sorted(rows, key=key)


def _news_stamp(inp: HoldingInput, today: date) -> str:
    """공시·뉴스 기준일 표시. 오늘 기록이면 빈 문자열."""
    if inp.news_date is None:
        return "(확인일 없음)"
    if inp.news_date == today:
        return ""
    delta = (today - inp.news_date).days
    if delta == 1:
        return "(어제 기준)"
    if delta > 1:
        return f"({delta}일 전 기준)"
    return f"({inp.news_date.month}/{inp.news_date.day} 기준)"


def build_subject(rows: Sequence[ReportRow], today: Optional[date] = None) -> str:
    """제목: [주식점검] 7/29 조치 3건 / [주식점검] 7/29 이상 없음"""
    today = today or date.today()
    action_count = sum(1 for _, res in rows if res.is_action_needed)
    tail = f"조치 {action_count}건" if action_count else "이상 없음"
    return f"[주식점검] {today.month}/{today.day} {tail}"


# ── HTML 본문 ───────────────────────────────────────────────

def _td(content: str, *, align: str = "right", color: str = "",
        bold: bool = False, nowrap: bool = True) -> str:
    style = (
        f"padding:6px 10px;border:1px solid {C_BORDER};"
        f"text-align:{align};"
    )
    if nowrap:
        style += "white-space:nowrap;"
    if color:
        style += f"color:{color};"
    if bold:
        style += "font-weight:bold;"
    return f'<td style="{style}">{content}</td>'


def _badge_html(res: HoldingResult) -> str:
    """판정 배지."""
    bg, fg = VERDICT_BADGE.get(res.verdict, (C_HEAD_BG, C_LABEL))
    return (
        f'<span style="display:inline-block;padding:2px 10px;border-radius:10px;'
        f'background-color:{bg};color:{fg};font-weight:bold;font-size:13px;'
        f'white-space:nowrap;">{html.escape(res.verdict or EMPTY)}</span>'
    )


def _row_html(inp: HoldingInput, res: HoldingResult) -> str:
    """요약표 한 행."""
    stop = _is_stop(res)
    bg = f"background-color:{C_STOP_BG};" if stop else ""

    pl = _profit_pct(inp, res)
    room = _stop_room_pct(res)
    exit_room = res.dist_to_exit_pct if res.current_price else None

    pl_color = ""
    if pl is not None:
        pl_color = C_PROFIT if pl >= 0 else C_LOSS

    # 터틀외 종목은 진입시 ATR이 있어도 R배수를 내지 않는다 - 터틀
    # 손절 로직(2×ATR) 자체가 적용되지 않는 종목이라 값 자체가 무의미하다.
    r_mult = None if _is_non_turtle(inp) else _r_multiple(inp, res)
    r_color = "" if r_mult is None else (C_PROFIT if r_mult >= 0 else C_LOSS)
    pl_amount = _pl_amount(inp, res)
    pl_amount_color = (
        "" if pl_amount is None else (C_PROFIT if pl_amount >= 0 else C_LOSS)
    )

    cells = [
        _td(_linked_name(inp, color=C_STOP_TEXT if stop else "", bold=stop),
            align="left"),
        _td(_badge_html(res), align="center"),
        _td(_fmt(res.current_price) if res.current_price else EMPTY),
        _td(_fmt_pct(pl), color=pl_color),
        _td(_fmt_pct(room),
            color=C_STOP_TEXT if (room is not None and room <= 0) else ""),
        _td(_fmt_pct(exit_room),
            color=C_STOP_TEXT if (exit_room is not None and exit_room <= 0) else ""),
        _td(_fmt_r(r_mult), color=r_color),
        _td(_fmt_signed(pl_amount), color=pl_amount_color),
    ]
    return f'<tr style="{bg}">' + "".join(cells) + "</tr>"


def _card_row(label: str, value: str, *, color: str = "",
              nowrap: bool = False) -> str:
    """카드 안의 한 줄 (항목 이름 + 값)."""
    style = "padding:3px 0;vertical-align:top;"
    val_style = style + (f"color:{color};" if color else "")
    if nowrap:
        val_style += "white-space:nowrap;"
    return (
        f'<tr>'
        f'<td style="{style}width:96px;color:{C_LABEL};white-space:nowrap;">'
        f"{html.escape(label)}</td>"
        f'<td style="{val_style}">{value}</td>'
        f"</tr>"
    )


def _card_html(inp: HoldingInput, res: HoldingResult, today: date) -> str:
    """종목 카드 한 장. 노션 값이 비면 '-'로 두고 추측하지 않는다."""
    stop = _is_stop(res)
    border = C_STOP_TEXT if stop else C_BORDER

    pl = _profit_pct(inp, res)
    pl_color = "" if pl is None else (C_PROFIT if pl >= 0 else C_LOSS)
    room = _stop_room_pct(res)
    exit_room = res.dist_to_exit_pct if res.current_price else None

    # 공시·뉴스: 노션 값 그대로. 비어 있을 때만 '수집 없음'.
    memo = (inp.news_memo or "").strip()
    if memo:
        news = html.escape(memo).replace("\n", "<br>")
        stamp = _news_stamp(inp, today)
        if stamp:
            news += (
                f' <span style="color:{C_STOP_TEXT};font-size:12px;">'
                f"{html.escape(stamp)}</span>"
            )
    else:
        news = f'<span style="color:{C_MUTED};">{NO_NEWS}</span>'

    exit_mark = (
        f'<span style="color:{C_EXIT_LINE};font-weight:bold;">⛔ 있음</span>'
        if inp.exit_signal
        else f'<span style="color:{C_MUTED};">없음</span>'
    )

    body = "".join([
        _card_row("현재가 / 매수단가",
                  f"{_fmt(res.current_price) if res.current_price else EMPTY}원"
                  f" / {_fmt(inp.buy_price)}원", nowrap=True),
        _card_row("손익률", _fmt_pct(pl), color=pl_color, nowrap=True),
        _card_row("ATR", f"{_fmt(res.atr, 2) if res.atr else EMPTY}원",
                  nowrap=True),
        _card_row("손절선",
                  f"{_fmt(res.stop_loss) if res.stop_loss else EMPTY}원"
                  f" (여유 {_fmt_pct(room)})", nowrap=True),
        _card_row("청산선",
                  f"{_fmt(res.exit_level) if res.exit_level else EMPTY}원"
                  f" (여유 {_fmt_pct(exit_room)})", nowrap=True),
        _card_row("추가매수", html.escape(_pyramid_line(res)), nowrap=True),
        _card_row("산 이유", html.escape(_or_dash(inp.buy_reason))),
        _card_row("강세론", html.escape(_or_dash(inp.bull_case))),
        _card_row("약세론", html.escape(_or_dash(inp.bear_case))),
        _card_row("공시·뉴스", news),
        _card_row("철수신호", exit_mark),
        _card_row("다음 확인 이벤트", html.escape(_or_dash(inp.next_event))),
    ])

    memo_line = ""
    note = res.verdict_memo or (f"조회 실패: {res.error}" if res.error else "")
    if note:
        memo_line = (
            f'<div style="margin-top:6px;color:{C_MUTED};font-size:12px;">'
            f"{html.escape(note)}</div>"
        )

    return f"""
  <div style="border:1px solid {border};border-left:4px solid {border};
  border-radius:6px;background-color:{C_CARD_BG};padding:12px 14px;
  margin:0 0 12px;">
    <div style="margin-bottom:8px;">
      <span style="font-size:16px;">{_linked_name(inp, bold=True)}</span>
      <span style="color:{C_MUTED};font-size:12px;">
        ({html.escape(inp.ticker)})</span>
      &nbsp;{_badge_html(res)}
    </div>
    <table style="border-collapse:collapse;font-size:13px;width:100%;">
      {body}
    </table>{memo_line}
  </div>"""


def _build_stop_updates_html(stop_updates: Sequence[ReportRow]) -> str:
    """손절선 갱신 필요 표. 대상이 없으면 섹션 자체를 생략한다.

    core.evaluate_holding()이 (마지막 알린 손절선 대비 0.5×진입시ATR
    이상 상승)일 때만 stop_update_alert를 세우므로, 여기 들어오는 종목은
    inp.last_alerted_stop이 항상 값을 갖는다.
    """
    if not stop_updates:
        return ""

    head = "".join(
        f'<th style="padding:8px 10px;border:1px solid {C_BORDER};'
        f'background-color:{C_HEAD_BG};text-align:center;white-space:nowrap;">'
        f"{html.escape(c)}</th>"
        for c in ("종목명(코드)", "기존", "신규", "차이(×ATR)")
    )

    def _row(inp: HoldingInput, res: HoldingResult) -> str:
        name_cell = (
            f'{_linked_name(inp)} '
            f'<span style="color:{C_MUTED};font-size:12px;">'
            f'({html.escape(inp.ticker)})</span>'
        )
        old_stop = inp.last_alerted_stop or 0.0
        atr_mult = (
            (res.stop_loss - old_stop) / res.entry_atr if res.entry_atr else None
        )
        cells = [
            _td(name_cell, align="left"),
            _td(f"{_fmt(old_stop)}원"),
            _td(f"{_fmt(res.stop_loss)}원", bold=True),
            _td(f"{atr_mult:+.2f}" if atr_mult is not None else EMPTY),
        ]
        return "<tr>" + "".join(cells) + "</tr>"

    body = "".join(_row(inp, res) for inp, res in stop_updates)

    return f"""
  <h3 style="margin:20px 0 10px;">🔺 손절선 갱신 필요 ({len(stop_updates)}건)</h3>
  <table style="border-collapse:collapse;border:1px solid {C_BORDER};font-size:13px;">
    <thead><tr>{head}</tr></thead>
    <tbody>{body}</tbody>
  </table>"""


def _build_favorites_html(favorites: Sequence[dict]) -> str:
    """즐겨찾기 표. 비어 있으면 빈 문자열을 돌려줘 섹션 자체를 생략한다.

    favorites의 각 dict는 notion_repo.fetch_favorites()가 주는 item과
    같은 키를 쓴다 (ticker/name/category/current_price/gap_atr/atr_pct/
    verdict). 계산 전(야간 배치가 아직 안 돈) 종목은 해당 칸이 None이라
    '-'로 표시된다.
    """
    if not favorites:
        return ""

    head = "".join(
        f'<th style="padding:8px 10px;border:1px solid {C_BORDER};'
        f'background-color:{C_HEAD_BG};text-align:center;white-space:nowrap;">'
        f"{html.escape(c)}</th>"
        for c in ("종목명(코드)", "구분", "현재가", "갭(×ATR)", "ATR%", "판정")
    )

    def _row(item: dict) -> str:
        name_cell = (
            f'{_linked(item["name"], item["ticker"])} '
            f'<span style="color:{C_MUTED};font-size:12px;">'
            f'({html.escape(item["ticker"])})</span>'
        )
        gap = item.get("gap_atr")
        atr_pct = item.get("atr_pct")
        cells = [
            _td(name_cell, align="left"),
            _td(html.escape(item.get("category") or EMPTY), align="center"),
            _td(_fmt(item.get("current_price"))
                if item.get("current_price") else EMPTY),
            _td(f"{gap:+.2f}" if gap is not None else EMPTY),
            _td(f"{atr_pct:.1f}%" if atr_pct is not None else EMPTY),
            _td(html.escape(item.get("verdict") or EMPTY), align="center"),
        ]
        return "<tr>" + "".join(cells) + "</tr>"

    body = "".join(_row(item) for item in favorites)

    return f"""
  <h3 style="margin:20px 0 10px;">■ 즐겨찾기</h3>
  <table style="border-collapse:collapse;border:1px solid {C_BORDER};font-size:13px;">
    <thead><tr>{head}</tr></thead>
    <tbody>{body}</tbody>
  </table>"""


def _summary_row_html(turtle_rows: Sequence[ReportRow]) -> str:
    """터틀 대상 종목 합계 행 – 평가손익 합계, 수익/손실 종목 수."""
    pl_values = [
        v for inp, res in turtle_rows
        if (v := _pl_amount(inp, res)) is not None
    ]
    total_pl = sum(pl_values)
    wins = sum(1 for v in pl_values if v > 0)
    losses = sum(1 for v in pl_values if v < 0)
    pl_color = C_PROFIT if total_pl >= 0 else C_LOSS

    return f"""<tr style="background-color:{C_HEAD_BG};font-weight:bold;">
      <td colspan="7" style="padding:6px 10px;border:1px solid {C_BORDER};
      text-align:right;">합계 (터틀 대상 {len(turtle_rows)}종목)</td>
      <td style="padding:6px 10px;border:1px solid {C_BORDER};
      text-align:right;color:{pl_color};white-space:nowrap;">
        {_fmt_signed(total_pl)}원 ({wins}승 {losses}패)</td>
    </tr>"""


def _divider_row_html() -> str:
    return (
        f'<tr><td colspan="8" style="padding:0;border:none;'
        f'border-top:2px solid {C_MUTED};"></td></tr>'
    )


def _build_html(
    rows: Sequence[ReportRow],
    risk: PortfolioRisk,
    has_errors: bool,
    today: date,
    favorites: Sequence[dict] = (),
    stop_updates: Sequence[ReportRow] = (),
) -> str:
    rows = _sorted_rows(rows)      # 조치 필요 종목이 위로

    head_cells = "".join(
        f'<th style="padding:8px 10px;border:1px solid {C_BORDER};'
        f'background-color:{C_HEAD_BG};text-align:center;'
        f'white-space:nowrap;">{html.escape(c)}</th>'
        for c in COLUMNS
    )

    # 요약표는 터틀 대상만 정렬·합계 대상으로 삼는다. 터틀외(하이닉스 등)는
    # R배수가 의미 없어 표 맨 아래 구분선 밑에 그대로(원래 순서) 붙인다.
    turtle_rows, non_turtle_rows = _split_turtle_rows(rows)
    turtle_rows = _sorted_turtle_rows(turtle_rows)

    body_parts = [_row_html(inp, res) for inp, res in turtle_rows]
    if turtle_rows:
        body_parts.append(_summary_row_html(turtle_rows))
    if non_turtle_rows:
        body_parts.append(_divider_row_html())
        body_parts.extend(_row_html(inp, res) for inp, res in non_turtle_rows)
    body_rows = "".join(body_parts)

    actions = [res for _, res in rows if res.is_action_needed]
    if actions:
        summary = f"조치 필요 {len(actions)}건: " + ", ".join(
            f"{res.name}({res.verdict})" for res in actions
        )
        summary_color = C_STOP_TEXT
    else:
        summary = "조치 필요 종목 없음 (전 종목 유지)"
        summary_color = "#2b8a3e"

    # 철수신호 – 판정과 별개로 상단에 모아 보여준다
    exits = [(inp, res) for inp, res in rows if inp.exit_signal]
    if exits:
        items = "".join(
            f'<li style="margin:3px 0;">'
            f'<b>{_linked_name(inp)}</b> '
            f'<span style="color:{C_MUTED};">({html.escape(inp.ticker)})</span>'
            f' – 판정 {html.escape(res.verdict or "-")}'
            f'</li>'
            for inp, res in exits
        )
        exit_box = f"""
  <div style="border:2px solid {C_EXIT_LINE};background-color:{C_EXIT_BG};
  border-radius:6px;padding:10px 14px;margin:0 0 16px;">
    <div style="font-weight:bold;color:{C_EXIT_LINE};margin-bottom:4px;">
      ⛔ 철수신호 {len(exits)}건 – 판정과 별개로 검토 필요
    </div>
    <ul style="margin:0;padding-left:20px;">{items}</ul>
  </div>"""
    else:
        exit_box = ""

    stop_updates_section = _build_stop_updates_html(stop_updates)

    # 종목별 카드 – 표에 안 담기는 노션 값(산 이유·강세론·약세론·공시·뉴스 등)
    cards = "".join(_card_html(inp, res, today) for inp, res in rows)
    card_section = f"""
  <h3 style="margin:20px 0 10px;">📋 종목별 상세</h3>{cards}"""

    favorites_section = _build_favorites_html(favorites)

    risk_color = C_STOP_TEXT if risk.risk_warning else "#212529"
    warn = " ⚠️ 6% 초과" if risk.risk_warning else ""

    # 손절 대기는 리스크 %와 별개 항목이다. 손절선을 이미 깬 종목은
    # 리스크 합계에서 0으로 잡히므로 건수로 따로 알려야 한다.
    stop_pending_color = C_STOP_TEXT if risk.stop_pending else "#212529"
    stop_pending_note = (
        '<span style="font-weight:normal;color:#868e96;"> '
        "– 손절선을 이미 깬 종목이라 리스크 합계에는 0으로 잡힘</span>"
        if risk.stop_pending
        else ""
    )

    if risk.group_risk_pct:
        group_rows = "".join(
            f"""    <tr>
      <td style="padding:6px 12px 6px 0;color:{C_MUTED};">
        상관군 · {html.escape(g)}</td>
      <td style="padding:6px 0;font-weight:bold;">
        {pct:.2f}% ({_fmt(risk.group_risk_amount.get(g, 0.0))}원)
      </td>
    </tr>
"""
            for g, pct in risk.group_risk_pct.items()
        )
    else:
        group_rows = (
            f'    <tr><td style="padding:6px 12px 6px 0;color:{C_MUTED};">'
            f"상관군</td>"
            f'<td style="padding:6px 0;color:{C_MUTED};">지정 없음</td></tr>'
        )

    error_note = (
        f'<p style="color:{C_STOP_TEXT};margin:8px 0 0;">'
        "⚠️ 일부 종목의 시세 조회 또는 노션 갱신에 실패했습니다. "
        "로그를 확인하세요.</p>"
        if has_errors
        else ""
    )

    return f"""\
<div style="font-family:-apple-system,'Apple SD Gothic Neo','Malgun Gothic',
sans-serif;font-size:14px;color:#212529;line-height:1.5;">
  <h2 style="margin:0 0 4px;">📊 포트폴리오 점검 리포트</h2>
  <p style="margin:0 0 12px;color:{C_MUTED};">{today.isoformat()} 기준</p>

  <p style="margin:0 0 12px;color:{summary_color};font-weight:bold;">
    {html.escape(summary)}
  </p>
{exit_box}
{stop_updates_section}
  <table style="border-collapse:collapse;border:1px solid {C_BORDER};
  font-size:13px;">
    <thead><tr>{head_cells}</tr></thead>
    <tbody>{body_rows}</tbody>
  </table>
{card_section}
{favorites_section}

  <table style="border-collapse:collapse;margin-top:16px;font-size:14px;">
    <tr>
      <td style="padding:6px 12px 6px 0;color:{C_MUTED};">전체 리스크</td>
      <td style="padding:6px 0;font-weight:bold;color:{risk_color};">
        {risk.total_risk_pct:.2f}% ({_fmt(risk.total_risk_amount)}원){warn}
      </td>
    </tr>
    <tr>
      <td style="padding:6px 12px 6px 0;color:{C_MUTED};">손절 대기</td>
      <td style="padding:6px 0;font-weight:bold;color:{stop_pending_color};">
        {risk.stop_pending}건{stop_pending_note}
      </td>
    </tr>
{group_rows}
  </table>
  {error_note}
</div>"""


# ── 텍스트 대체본 ───────────────────────────────────────────

def _build_favorites_text(favorites: Sequence[dict]) -> list[str]:
    """즐겨찾기 섹션 줄 목록. 비어 있으면 빈 리스트 (섹션 생략)."""
    if not favorites:
        return []

    lines = ["[즐겨찾기]"]
    for item in favorites:
        price = item.get("current_price")
        gap = item.get("gap_atr")
        atr_pct = item.get("atr_pct")
        lines.append(
            f"  {item.get('verdict') or EMPTY:<5} {item['name']} "
            f"({item['ticker']}) · {item.get('category') or EMPTY} "
            f"{_fmt(price) if price else EMPTY} "
            f"({f'{gap:+.2f}N' if gap is not None else EMPTY}) "
            f"ATR% {f'{atr_pct:.1f}%' if atr_pct is not None else EMPTY}"
        )
    lines.append("")
    return lines


def _build_stop_updates_text(stop_updates: Sequence[ReportRow]) -> list[str]:
    """손절선 갱신 필요 섹션 줄 목록. 비어 있으면 빈 리스트 (섹션 생략)."""
    if not stop_updates:
        return []

    lines = [f"🔺 손절선 갱신 필요 {len(stop_updates)}건"]
    for inp, res in stop_updates:
        old_stop = inp.last_alerted_stop or 0.0
        atr_mult = (
            (res.stop_loss - old_stop) / res.entry_atr if res.entry_atr else None
        )
        lines.append(
            f"  - {inp.name} ({inp.ticker}) "
            f"{_fmt(old_stop)} → {_fmt(res.stop_loss)}원 "
            f"({f'{atr_mult:+.2f}×ATR' if atr_mult is not None else EMPTY})"
        )
    lines.append("")
    return lines


def _build_text(
    rows: Sequence[ReportRow],
    risk: PortfolioRisk,
    has_errors: bool,
    today: date,
    favorites: Sequence[dict] = (),
    stop_updates: Sequence[ReportRow] = (),
) -> str:
    rows = _sorted_rows(rows)      # 조치 필요 종목이 위로

    lines = [
        f"📊 포트폴리오 점검 리포트 ({today.isoformat()} 기준)",
        "",
    ]

    exits = [(inp, res) for inp, res in rows if inp.exit_signal]
    if exits:
        lines.append(f"⛔ 철수신호 {len(exits)}건 – 판정과 별개로 검토 필요")
        for inp, res in exits:
            lines.append(f"  - {inp.name} ({inp.ticker}) – 판정 {res.verdict or '-'}")
        lines.append("")

    lines.extend(_build_stop_updates_text(stop_updates))

    # 요약 – 터틀 대상만 R배수 정렬·합계 대상. 터틀외는 구분선 아래 별도.
    turtle_rows, non_turtle_rows = _split_turtle_rows(rows)
    turtle_rows = _sorted_turtle_rows(turtle_rows)

    def _summary_line(inp: HoldingInput, res: HoldingResult) -> str:
        exit_room = res.dist_to_exit_pct if res.current_price else None
        r_mult = None if _is_non_turtle(inp) else _r_multiple(inp, res)
        return (
            f"  {res.verdict or EMPTY:<5} {inp.name} "
            f"{_fmt(res.current_price) if res.current_price else EMPTY} "
            f"({_fmt_pct(_profit_pct(inp, res))}) "
            f"손절여유 {_fmt_pct(_stop_room_pct(res))} / "
            f"청산여유 {_fmt_pct(exit_room)} / "
            f"{_fmt_r(r_mult)} / {_fmt_signed(_pl_amount(inp, res))}원"
        )

    lines.append("[요약]")
    for inp, res in turtle_rows:
        lines.append(_summary_line(inp, res))

    if turtle_rows:
        pl_values = [
            v for inp, res in turtle_rows
            if (v := _pl_amount(inp, res)) is not None
        ]
        total_pl = sum(pl_values)
        wins = sum(1 for v in pl_values if v > 0)
        losses = sum(1 for v in pl_values if v < 0)
        lines.append(
            f"  합계 (터틀 대상 {len(turtle_rows)}종목): "
            f"{_fmt_signed(total_pl)}원 ({wins}승 {losses}패)"
        )

    if non_turtle_rows:
        lines.append("  ─── 터틀외 ───")
        for inp, res in non_turtle_rows:
            lines.append(_summary_line(inp, res))
    lines.append("")

    # 종목별 상세
    lines.append("[종목별 상세]")
    lines.append("")
    for inp, res in rows:
        exit_room = res.dist_to_exit_pct if res.current_price else None
        lines.append(f"■ {inp.name} ({inp.ticker}) — {res.verdict or EMPTY}")
        lines.append(
            f"  현재가 {_fmt(res.current_price) if res.current_price else EMPTY}"
            f" / 매수단가 {_fmt(inp.buy_price)}"
            f" / 손익률 {_fmt_pct(_profit_pct(inp, res))}"
        )
        lines.append(
            f"  ATR {_fmt(res.atr, 2) if res.atr else EMPTY}"
            f" / 손절선 {_fmt(res.stop_loss) if res.stop_loss else EMPTY}"
            f" (여유 {_fmt_pct(_stop_room_pct(res))})"
        )
        lines.append(
            f"  청산선 {_fmt(res.exit_level) if res.exit_level else EMPTY}"
            f" (여유 {_fmt_pct(exit_room)})"
        )
        lines.append(f"  추가매수: {_pyramid_line(res)}")
        lines.append(f"  산 이유: {_or_dash(inp.buy_reason)}")
        lines.append(f"  강세론: {_or_dash(inp.bull_case)}")
        lines.append(f"  약세론: {_or_dash(inp.bear_case)}")

        memo = (inp.news_memo or "").strip()
        if memo:
            stamp = _news_stamp(inp, today)
            lines.append(f"  공시·뉴스: {stamp}".rstrip())
            for memo_line in memo.splitlines():
                lines.append(f"    {memo_line}")
        else:
            lines.append(f"  공시·뉴스: {NO_NEWS}")

        lines.append(f"  철수신호: {'있음' if inp.exit_signal else '없음'}")
        lines.append(f"  다음 확인 이벤트: {_or_dash(inp.next_event)}")

        note = res.verdict_memo or (f"조회 실패: {res.error}" if res.error else "")
        if note:
            lines.append(f"  메모: {note}")
        lines.append("")

    lines.extend(_build_favorites_text(favorites))

    warn = " (6% 초과)" if risk.risk_warning else ""
    lines.append(
        f"전체 리스크: {risk.total_risk_pct:.2f}% "
        f"({_fmt(risk.total_risk_amount)}원){warn}"
    )
    lines.append(
        f"손절 대기: {risk.stop_pending}건"
        + (
            " (손절선을 이미 깬 종목이라 리스크 합계에는 0으로 잡힘)"
            if risk.stop_pending
            else ""
        )
    )
    if risk.group_risk_pct:
        for g, pct in risk.group_risk_pct.items():
            lines.append(
                f"상관군 · {g}: {pct:.2f}% "
                f"({_fmt(risk.group_risk_amount.get(g, 0.0))}원)"
            )
    else:
        lines.append("상관군: 지정 없음")

    if has_errors:
        lines.append("")
        lines.append("⚠️ 일부 종목의 시세 조회 또는 노션 갱신에 실패했습니다.")

    return "\n".join(lines)


# ── 발송 ────────────────────────────────────────────────────

def send_report_mail(
    rows: Sequence[ReportRow],
    risk: PortfolioRisk,
    has_errors: bool = False,
    today: Optional[date] = None,
    favorites: Sequence[dict] = (),
    stop_updates: Sequence[ReportRow] = (),
) -> bool:
    """상세 리포트를 Gmail SMTP로 발송한다.

    Args:
        rows: (HoldingInput, HoldingResult) 쌍의 목록 – 표의 각 행
        risk: 포트폴리오 리스크 요약
        has_errors: 조회/갱신 실패가 있었는지
        today: 기준일 (기본값 오늘)
        favorites: 노션 즐겨찾기 DB 조회 결과 (notion_repo.fetch_favorites()의
            item dict 목록, page_id는 뺀 것). 비어 있으면 즐겨찾기 섹션 생략.
            보유종목 섹션 로직에는 영향 없다.
        stop_updates: 손절선 갱신 알림 대상 (HoldingInput, HoldingResult) 쌍.
            rows의 부분집합이며, res.stop_update_alert가 True인 것들이다.
            비어 있으면 "손절선 갱신 필요" 섹션을 생략한다.

    Returns:
        True  – 발송 완료
        False – 환경변수 미설정으로 건너뜀

    Raises:
        smtplib.SMTPException 등 – 발송 실패 시 그대로 전파
        (호출부에서 try/except로 감싸 전체 실행을 막지 않도록 한다)
    """
    today = today or date.today()

    sender = os.environ.get("GMAIL_ADDRESS", "").strip()
    password = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
    if not sender or not password:
        logger.warning(
            "GMAIL_ADDRESS / GMAIL_APP_PASSWORD 미설정 – 메일 발송을 건너뜁니다."
        )
        return False

    to_raw = os.environ.get("GMAIL_TO", "").strip() or sender
    recipients = [addr.strip() for addr in to_raw.split(",") if addr.strip()]

    msg = EmailMessage()
    msg["Subject"] = build_subject(rows, today)
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)

    msg.set_content(
        _build_text(rows, risk, has_errors, today, favorites, stop_updates)
    )
    msg.add_alternative(
        _build_html(rows, risk, has_errors, today, favorites, stop_updates),
        subtype="html",
    )
    # 차트 추가 시:
    #   msg.get_payload()[1].add_related(png_bytes, "image", "png", cid="chart")
    # 후 _build_html()에 <img src="cid:chart"> 삽입

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=SMTP_TIMEOUT) as smtp:
        smtp.starttls()
        smtp.login(sender, password)
        smtp.send_message(msg, from_addr=sender, to_addrs=recipients)

    logger.info("메일 발송 완료 (수신: %s)", ", ".join(recipients))
    return True
