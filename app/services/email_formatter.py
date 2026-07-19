# ruff: noqa: E501
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from html import escape
from typing import Iterable, Optional
from zoneinfo import ZoneInfo

from app.services.email import EmailContent

DISCLAIMER = (
    "참고용 자동 분류이며 투자 추천이나 수익 보장이 아닙니다. "
    "최종 판단과 청약은 사용자가 직접 수행해야 합니다."
)
INSUFFICIENT_VALIDATION = "검증 표본 부족 — 확률과 기대수익을 제공하지 않습니다."
MISSING_RISK = "위험 정보 미확인 — 무위험을 의미하지 않습니다."
KST = ZoneInfo("Asia/Seoul")
WEEKDAYS = ("월", "화", "수", "목", "금", "토", "일")
DECISION_COLORS = {
    "청약 후보": ("#E8F5EE", "#11653A"),
    "검증 대기": ("#FFF4D6", "#805500"),
    "가치 기준 제외": ("#F1F3F4", "#4A5568"),
    "데이터 부족": ("#FFF4D6", "#805500"),
    "중대 위험 차단": ("#FDECEC", "#A32020"),
}


@dataclass(frozen=True)
class DecisionEmailItem:
    corp_name: str
    decision: str
    next_action: str = "공모 일정과 공시 원문을 직접 확인하세요."
    total_score: Optional[float] = None
    confidence_score: Optional[float] = None
    reasons: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()
    subscription_start_date: Optional[date] = None
    subscription_end_date: Optional[date] = None
    refund_date: Optional[date] = None
    listing_date: Optional[date] = None
    offer_price: Optional[int] = None
    broker_name: Optional[str] = None
    expected_shares: Optional[float] = None
    break_even_return_pct: Optional[float] = None
    expected_net_profit: Optional[float] = None
    positive_probability: Optional[float] = None
    validation_sample_count: int = 0
    validation_summary: Optional[str] = None


@dataclass(frozen=True)
class ScheduleEmailItem:
    corp_name: str
    event_label: str
    event_date: date


def build_decision_email(
    title: str,
    period_label: str,
    items: Iterable[DecisionEmailItem],
    generated_at: datetime,
    validation_note: str,
    *,
    schedule_items: Iterable[ScheduleEmailItem] = (),
) -> EmailContent:
    rows = list(items)
    schedules = sorted(
        schedule_items,
        key=lambda item: (item.event_date, item.corp_name, item.event_label),
    )
    generated_label = _generated_label(generated_at)
    subject = _subject(title, len(rows), len(schedules))
    summary = _summary(len(rows), len(schedules))
    preheader = _preheader(len(rows), len(schedules))
    text = _build_text(
        title,
        period_label,
        rows,
        schedules,
        generated_label,
        validation_note,
        summary,
    )
    html = _build_html(
        title,
        period_label,
        rows,
        schedules,
        generated_label,
        validation_note,
        summary,
        preheader,
    )
    return EmailContent(subject=subject, text=text, html=html)


def _subject(title: str, decision_count: int, schedule_count: int) -> str:
    if decision_count and schedule_count:
        suffix = f"판단 {decision_count} · 일정 {schedule_count}"
    elif decision_count:
        suffix = f"판단 {decision_count}"
    elif schedule_count:
        suffix = f"일정 {schedule_count}"
    else:
        suffix = "새 일정 없음"
    return f"{title} · {suffix}"


def _summary(decision_count: int, schedule_count: int) -> str:
    if decision_count:
        return f"확인할 청약 판단 {decision_count}건 · 주요 일정 {schedule_count}건"
    if schedule_count:
        return f"청약 판단은 없고 주요 일정 {schedule_count}건이 있습니다."
    return "새로 확인할 IPO 일정이 없습니다."


def _preheader(decision_count: int, schedule_count: int) -> str:
    if decision_count:
        return f"청약 판단 {decision_count}건과 주요 일정 {schedule_count}건을 확인하세요."
    if schedule_count:
        return f"청약 판단 대상은 없고 주요 일정 {schedule_count}건이 있습니다."
    return "새로 확인할 IPO 일정이 없습니다."


def _build_text(
    title: str,
    period_label: str,
    items: list[DecisionEmailItem],
    schedules: list[ScheduleEmailItem],
    generated_label: str,
    validation_note: str,
    summary: str,
) -> str:
    lines = [title, summary, f"기간: {period_label}", f"업데이트: {generated_label}", ""]
    if schedules:
        lines.extend(["주요 일정", *(_text_schedule(item) for item in schedules), ""])
    if items:
        lines.append("청약 판단")
        for item in items:
            lines.extend(_text_item(item))
    lines.extend([f"검증 상태: {validation_note}", "", DISCLAIMER])
    return "\n".join(lines)


def _text_schedule(item: ScheduleEmailItem) -> str:
    return f"- {_date(item.event_date)} · {item.event_label} · {item.corp_name}"


def _text_item(item: DecisionEmailItem) -> list[str]:
    reasons = item.reasons or ("확인된 긍정 근거가 없습니다.",)
    risks = item.risks or (MISSING_RISK,)
    lines = [
        f"[{item.decision}] {item.corp_name}",
        f"오늘 할 일: {item.next_action}",
        f"점수: {_score(item.total_score)}",
        f"데이터 완성도: {_confidence(item.confidence_score)}",
        f"공모가: {_money(item.offer_price)}",
    ]
    schedule = _decision_schedule(item)
    if schedule:
        lines.append(f"일정: {schedule}")
    allocation = _allocation(item)
    if allocation:
        lines.append(f"배정 추정: {allocation}")
    value = _validation_value(item)
    if value:
        lines.append(f"검증 가치: {value}")
    lines.extend(["근거:", *(f"- {reason}" for reason in reasons)])
    lines.extend(["위험:", *(f"- {risk}" for risk in risks)])
    lines.extend(
        [
            f"검증: {item.validation_summary or INSUFFICIENT_VALIDATION}",
            "",
        ]
    )
    return lines


def _build_html(
    title: str,
    period_label: str,
    items: list[DecisionEmailItem],
    schedules: list[ScheduleEmailItem],
    generated_label: str,
    validation_note: str,
    summary: str,
    preheader: str,
) -> str:
    schedule_section = _html_schedule_section(schedules)
    decision_cards = "".join(_html_item(item) for item in items)
    decision_section = (
        f'<tr><td style="padding:24px 28px 8px;color:#152238;font-size:18px;'
        f'font-weight:700">청약 판단</td></tr>{decision_cards}'
        if decision_cards
        else ""
    )
    return f"""<!doctype html>
<html lang="ko">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#EEF2F7;font-family:Arial,'Apple SD Gothic Neo',sans-serif;color:#152238;line-height:1.55">
  <div style="display:none;max-height:0;overflow:hidden;opacity:0;color:transparent">{escape(preheader)}</div>
  <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" bgcolor="#EEF2F7">
    <tr><td align="center" style="padding:24px 12px">
      <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="max-width:680px;background:#FFFFFF;border-collapse:separate">
        <tr><td bgcolor="#102A43" style="padding:30px 28px;color:#FFFFFF">
          <div style="font-size:12px;letter-spacing:1.4px;font-weight:700;color:#9FD5FF">KOREA IPO ASSISTANT</div>
          <div style="margin-top:7px;font-size:26px;line-height:1.3;font-weight:800">{escape(title)}</div>
          <div style="margin-top:9px;font-size:14px;color:#D9E8F5">{escape(period_label)} · {escape(generated_label)}</div>
        </td></tr>
        <tr><td style="padding:24px 28px 8px">
          <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" bgcolor="#EAF4FF" style="border-left:4px solid #1677C8">
            <tr><td style="padding:18px 20px;color:#0B4A75;font-size:18px;font-weight:800">{escape(summary)}</td></tr>
          </table>
        </td></tr>
        {schedule_section}
        {decision_section}
        <tr><td style="padding:24px 28px 8px">
          <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" bgcolor="#F7F9FC">
            <tr><td style="padding:15px 17px;color:#526477;font-size:13px"><strong>검증 상태</strong><br>{escape(validation_note)}</td></tr>
          </table>
        </td></tr>
        <tr><td style="padding:18px 28px 30px;color:#6B7785;font-size:12px">{escape(DISCLAIMER)}</td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""


def _html_schedule_section(items: list[ScheduleEmailItem]) -> str:
    if not items:
        return ""
    rows = "".join(_html_schedule_item(item) for item in items)
    return f"""
        <tr><td style="padding:24px 28px 8px;color:#152238;font-size:18px;font-weight:700">주요 일정</td></tr>
        <tr><td style="padding:0 28px">
          <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="border:1px solid #D9E2EC">
            {rows}
          </table>
        </td></tr>"""


def _html_schedule_item(item: ScheduleEmailItem) -> str:
    return f"""<tr>
      <td width="128" style="padding:12px 14px;border-bottom:1px solid #E8EDF2;color:#526477;font-size:13px;white-space:nowrap">{escape(_date(item.event_date))}</td>
      <td width="92" style="padding:12px 8px;border-bottom:1px solid #E8EDF2;color:#1677C8;font-size:13px;font-weight:700;white-space:nowrap">{escape(item.event_label)}</td>
      <td style="padding:12px 14px;border-bottom:1px solid #E8EDF2;color:#152238;font-size:14px;font-weight:700">{escape(item.corp_name)}</td>
    </tr>"""


def _html_item(item: DecisionEmailItem) -> str:
    reasons = item.reasons or ("확인된 긍정 근거가 없습니다.",)
    risks = item.risks or (MISSING_RISK,)
    reason_rows = "".join(_html_bullet(reason, "#1677C8") for reason in reasons)
    risk_rows = "".join(_html_bullet(risk, "#C2413B") for risk in risks)
    background, foreground = DECISION_COLORS.get(item.decision, ("#EAF4FF", "#0B4A75"))
    schedule = _decision_schedule(item)
    schedule_row = _html_detail("일정", schedule) if schedule else ""
    allocation = _allocation(item)
    allocation_row = _html_detail("배정 추정", allocation) if allocation else ""
    value = _validation_value(item)
    value_row = _html_detail("검증 가치", value) if value else ""
    return f"""
        <tr><td style="padding:8px 28px">
          <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="border:1px solid #D9E2EC">
            <tr><td style="padding:20px 20px 8px">
              <span style="display:inline-block;padding:4px 9px;background:{background};color:{foreground};font-size:12px;font-weight:800">{escape(item.decision)}</span>
              <div style="margin-top:8px;font-size:21px;line-height:1.35;font-weight:800;color:#152238">{escape(item.corp_name)}</div>
            </td></tr>
            <tr><td style="padding:8px 20px">
              <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" bgcolor="#F0F7FF">
                <tr><td style="padding:13px 15px;color:#0B4A75;font-size:14px"><strong>오늘 할 일</strong><br>{escape(item.next_action)}</td></tr>
              </table>
            </td></tr>
            <tr><td style="padding:12px 20px 6px">
              <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0">
                <tr>
                  {_html_metric("점수", _score(item.total_score))}
                  {_html_metric("데이터 완성도", _confidence(item.confidence_score))}
                  {_html_metric("공모가", _money(item.offer_price))}
                </tr>
              </table>
            </td></tr>
            {schedule_row}{allocation_row}{value_row}
            <tr><td style="padding:13px 20px 4px;color:#34495E;font-size:14px"><strong>판단 근거</strong>{reason_rows}</td></tr>
            <tr><td style="padding:8px 20px 4px;color:#34495E;font-size:14px"><strong>위험 확인</strong>{risk_rows}</td></tr>
            <tr><td style="padding:12px 20px 20px;color:#6B7785;font-size:12px">검증: {escape(item.validation_summary or INSUFFICIENT_VALIDATION)}</td></tr>
          </table>
        </td></tr>"""


def _html_metric(label: str, value: str) -> str:
    return (
        '<td width="33.33%" valign="top" style="padding:10px 8px;border:1px solid #E8EDF2;'
        f'text-align:center"><div style="color:#6B7785;font-size:11px">{escape(label)}</div>'
        f'<div style="margin-top:3px;color:#152238;font-size:14px;font-weight:800">{escape(value)}</div></td>'
    )


def _html_detail(label: str, value: str) -> str:
    return (
        '<tr><td style="padding:4px 20px;color:#526477;font-size:13px">'
        f'<strong style="color:#34495E">{escape(label)}</strong> · {escape(value)}</td></tr>'
    )


def _html_bullet(value: str, color: str) -> str:
    return (
        '<div style="margin-top:5px">'
        f'<span style="color:{color};font-weight:800">•</span> {escape(value)}</div>'
    )


def _generated_label(value: datetime) -> str:
    localized = value.astimezone(KST) if value.tzinfo else value.replace(tzinfo=KST)
    return (
        f"{localized.month}월 {localized.day}일({WEEKDAYS[localized.weekday()]}) "
        f"{localized:%H:%M} KST"
    )


def _decision_schedule(item: DecisionEmailItem) -> str:
    parts = []
    subscription = _date_range(item.subscription_start_date, item.subscription_end_date)
    if subscription:
        parts.append(f"청약 {subscription}")
    if item.refund_date:
        parts.append(f"환불 {_date(item.refund_date)}")
    if item.listing_date:
        parts.append(f"상장 {_date(item.listing_date)}")
    return " · ".join(parts)


def _allocation(item: DecisionEmailItem) -> str:
    parts = []
    if item.broker_name:
        parts.append(item.broker_name)
    if item.expected_shares is not None:
        parts.append(f"예상 {_shares(item.expected_shares)}")
    if item.break_even_return_pct is not None:
        parts.append(f"손익분기 {_percent(item.break_even_return_pct)}")
    return " · ".join(parts)


def _validation_value(item: DecisionEmailItem) -> str:
    parts = []
    if item.positive_probability is not None:
        parts.append(f"D1 양수확률 {_probability(item.positive_probability)}")
    if item.expected_net_profit is not None:
        parts.append(f"중앙값 기준 순이익 {_money(item.expected_net_profit)}")
    if item.validation_sample_count > 0:
        parts.append(f"표본 n={item.validation_sample_count}")
    return " · ".join(parts)


def _score(value: Optional[float]) -> str:
    return "정보 없음" if value is None else f"{value:.1f} / 100"


def _confidence(value: Optional[float]) -> str:
    return "정보 없음" if value is None else f"{value:.1f}%"


def _date(value: date) -> str:
    return f"{value:%Y.%m.%d}({WEEKDAYS[value.weekday()]})"


def _date_range(start: Optional[date], end: Optional[date]) -> str:
    if start and end and start != end:
        return f"{_date(start)}–{_date(end)}"
    value = start or end
    return _date(value) if value else ""


def _money(value: Optional[float]) -> str:
    return "정보 없음" if value is None else f"{value:,.0f}원"


def _shares(value: float) -> str:
    return f"{value:,.2f}주"


def _percent(value: float) -> str:
    return f"{value:.2f}%"


def _probability(value: float) -> str:
    return f"{value:.1%}"
