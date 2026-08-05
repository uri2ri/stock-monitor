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

from core import HoldingInput, HoldingResult, PortfolioRisk

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
)

EMPTY = "-"                 # 노션 값이 비어 있을 때 (추측해서 채우지 않는다)
NO_NEWS = "수집 없음"        # 공시·뉴스만 별도 문구

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

    cells = [
        _td(html.escape(inp.name), align="left",
            color=C_STOP_TEXT if stop else "", bold=stop),
        _td(_badge_html(res), align="center"),
        _td(_fmt(res.current_price) if res.current_price else EMPTY),
        _td(_fmt_pct(pl), color=pl_color),
        _td(_fmt_pct(room),
            color=C_STOP_TEXT if (room is not None and room <= 0) else ""),
        _td(_fmt_pct(exit_room),
            color=C_STOP_TEXT if (exit_room is not None and exit_room <= 0) else ""),
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
      <span style="font-size:16px;font-weight:bold;">
        {html.escape(inp.name)}</span>
      <span style="color:{C_MUTED};font-size:12px;">
        ({html.escape(inp.ticker)})</span>
      &nbsp;{_badge_html(res)}
    </div>
    <table style="border-collapse:collapse;font-size:13px;width:100%;">
      {body}
    </table>{memo_line}
  </div>"""


def _build_html(
    rows: Sequence[ReportRow],
    risk: PortfolioRisk,
    has_errors: bool,
    today: date,
) -> str:
    rows = _sorted_rows(rows)      # 조치 필요 종목이 위로

    head_cells = "".join(
        f'<th style="padding:8px 10px;border:1px solid {C_BORDER};'
        f'background-color:{C_HEAD_BG};text-align:center;'
        f'white-space:nowrap;">{html.escape(c)}</th>'
        for c in COLUMNS
    )
    body_rows = "".join(_row_html(inp, res) for inp, res in rows)

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
            f'<b>{html.escape(inp.name)}</b> '
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

    # 종목별 카드 – 표에 안 담기는 노션 값(산 이유·강세론·약세론·공시·뉴스 등)
    cards = "".join(_card_html(inp, res, today) for inp, res in rows)
    card_section = f"""
  <h3 style="margin:20px 0 10px;">📋 종목별 상세</h3>{cards}"""

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
  <table style="border-collapse:collapse;border:1px solid {C_BORDER};
  font-size:13px;">
    <thead><tr>{head_cells}</tr></thead>
    <tbody>{body_rows}</tbody>
  </table>
{card_section}

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

def _build_text(
    rows: Sequence[ReportRow],
    risk: PortfolioRisk,
    has_errors: bool,
    today: date,
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

    # 요약
    lines.append("[요약]")
    for inp, res in rows:
        exit_room = res.dist_to_exit_pct if res.current_price else None
        lines.append(
            f"  {res.verdict or EMPTY:<5} {inp.name} "
            f"{_fmt(res.current_price) if res.current_price else EMPTY} "
            f"({_fmt_pct(_profit_pct(inp, res))}) "
            f"손절여유 {_fmt_pct(_stop_room_pct(res))} / "
            f"청산여유 {_fmt_pct(exit_room)}"
        )
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
) -> bool:
    """상세 리포트를 Gmail SMTP로 발송한다.

    Args:
        rows: (HoldingInput, HoldingResult) 쌍의 목록 – 표의 각 행
        risk: 포트폴리오 리스크 요약
        has_errors: 조회/갱신 실패가 있었는지
        today: 기준일 (기본값 오늘)

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

    msg.set_content(_build_text(rows, risk, has_errors, today))
    msg.add_alternative(
        _build_html(rows, risk, has_errors, today), subtype="html"
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
