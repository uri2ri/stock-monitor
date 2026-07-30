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

COLUMNS = (
    "종목명",
    "종목코드",
    "매수단가",
    "현재가",
    "손익률",
    "ATR",
    "손절선",
    "진입후 최고가",
    "손절선까지 여유%",
    "판정",
    "판정 메모",
)

# 이 문구만 적혀 있으면 공시·뉴스 블록에서 생략한다 (공백 제거 후 비교).
NO_NEWS_PHRASES = frozenset({
    "특이사항없음",
    "특이사항무",
    "해당없음",
    "없음",
})

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


def _has_news(inp: HoldingInput) -> bool:
    """리포트에 실을 공시·뉴스가 있는지.

    비어 있거나 "특이사항 없음" 류의 상투적인 문구뿐이면 False.
    """
    memo = (inp.news_memo or "").strip()
    if not memo:
        return False
    normalized = memo.rstrip(" .。!?~-·").replace(" ", "")
    if not normalized:      # "-", "." 같은 자리표시 문자뿐인 경우
        return False
    return normalized not in NO_NEWS_PHRASES


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


def _row_html(inp: HoldingInput, res: HoldingResult) -> str:
    stop = _is_stop(res)
    bg = f"background-color:{C_STOP_BG};" if stop else ""

    pl = _profit_pct(inp, res)
    room = _stop_room_pct(res)

    pl_color = ""
    if pl is not None:
        pl_color = C_PROFIT if pl >= 0 else C_LOSS

    room_color = C_STOP_TEXT if (room is not None and room <= 0) else ""

    verdict = html.escape(res.verdict or "-")
    if stop:
        verdict = f"🔴 {verdict}"

    memo = html.escape(res.verdict_memo or "")
    if res.error:
        memo = html.escape(f"조회 실패: {res.error}")

    cells = [
        _td(html.escape(inp.name), align="left",
            color=C_STOP_TEXT if stop else "", bold=stop),
        _td(html.escape(inp.ticker), align="center"),
        _td(_fmt(inp.buy_price)),
        _td(_fmt(res.current_price) if res.current_price else "-"),
        _td(_fmt_pct(pl), color=pl_color),
        _td(_fmt(res.atr, 2) if res.atr else "-"),
        _td(_fmt(res.stop_loss) if res.stop_loss else "-"),
        _td(_fmt(res.trailing_high) if res.trailing_high else "-"),
        _td(_fmt_pct(room), color=room_color),
        _td(verdict, align="center",
            color=C_STOP_TEXT if stop else "", bold=stop),
        _td(memo or "-", align="left",
            color=C_MUTED if not memo else "", nowrap=False),
    ]
    return f'<tr style="{bg}">' + "".join(cells) + "</tr>"


def _build_html(
    rows: Sequence[ReportRow],
    risk: PortfolioRisk,
    has_errors: bool,
    today: date,
) -> str:
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

    # 공시·뉴스 – 내용이 있는 종목만
    news_blocks = []
    for inp, res in rows:
        if not _has_news(inp):
            continue
        stamp = _news_stamp(inp, today)
        stamp_html = (
            f' <span style="color:{C_STOP_TEXT};font-size:12px;">'
            f'{html.escape(stamp)}</span>'
            if stamp
            else ""
        )
        mark = "⛔ " if inp.exit_signal else ""
        border = C_EXIT_LINE if inp.exit_signal else C_BORDER
        body = html.escape(inp.news_memo).replace("\n", "<br>")
        news_blocks.append(f"""
  <div style="border-left:3px solid {border};padding:2px 0 2px 10px;
  margin:0 0 12px;">
    <div style="font-weight:bold;">{mark}{html.escape(inp.name)}
      <span style="color:{C_MUTED};font-weight:normal;font-size:12px;">
        ({html.escape(inp.ticker)})</span>{stamp_html}
    </div>
    <div style="margin-top:2px;">{body}</div>
  </div>""")

    news_section = (
        f"""
  <h3 style="margin:20px 0 10px;">📰 공시·뉴스</h3>{"".join(news_blocks)}"""
        if news_blocks
        else ""
    )

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
{news_section}

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

    for inp, res in rows:
        mark = "[손절] " if _is_stop(res) else ""
        lines.append(f"{mark}{res.name} ({inp.ticker}) - {res.verdict or '-'}")
        lines.append(
            f"  매수단가 {_fmt(inp.buy_price)} / "
            f"현재가 {_fmt(res.current_price) if res.current_price else '-'} / "
            f"손익률 {_fmt_pct(_profit_pct(inp, res))}"
        )
        lines.append(
            f"  ATR {_fmt(res.atr, 2) if res.atr else '-'} / "
            f"손절선 {_fmt(res.stop_loss) if res.stop_loss else '-'} / "
            f"진입후 최고가 "
            f"{_fmt(res.trailing_high) if res.trailing_high else '-'} / "
            f"여유 {_fmt_pct(_stop_room_pct(res))}"
        )
        memo = f"조회 실패: {res.error}" if res.error else res.verdict_memo
        if memo:
            lines.append(f"  메모: {memo}")
        lines.append("")

    news_rows = [(inp, res) for inp, res in rows if _has_news(inp)]
    if news_rows:
        lines.append("📰 공시·뉴스")
        lines.append("")
        for inp, res in news_rows:
            mark = "⛔ " if inp.exit_signal else ""
            stamp = _news_stamp(inp, today)
            head = f"{mark}{inp.name} ({inp.ticker})"
            lines.append(f"{head} {stamp}".rstrip())
            for memo_line in inp.news_memo.splitlines():
                lines.append(f"  {memo_line}")
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
