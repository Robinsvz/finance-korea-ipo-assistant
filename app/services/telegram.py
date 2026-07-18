from __future__ import annotations

import asyncio
import logging
import math
import time
from datetime import date, timedelta
from typing import Any, Optional

from sqlalchemy import desc, select

from app.collectors.http import ResilientHttpClient
from app.config import get_settings
from app.db import session_scope
from app.models import (
    DiligenceReview,
    FinancialMetric,
    NewsMetric,
    OutcomeEvaluation,
    SearchTrend,
    SourceSnapshot,
)
from app.repositories import (
    get_ipo_or_raise,
    latest_score,
    list_ipos_between,
    save_allocation_result,
)
from app.services.allocation import (
    build_allocation_scenario,
    derive_allocation_assumptions,
    resolve_underwriter,
)
from app.services.backtest import build_backtest_report
from app.services.clock import format_local_datetime, local_today
from app.services.formatter import (
    format_allocation,
    format_backtest,
    format_financial,
    format_ipo_summary,
    format_result,
    format_schedule,
    format_score,
    format_trend,
)
from app.services.observability import (
    finish_notification,
    latest_job_statuses,
    reserve_notification,
)

logger = logging.getLogger(__name__)

TELEGRAM_COMMANDS = (
    ("today", "오늘 공모주 일정"),
    ("week", "이번 주 공모주 일정"),
    ("ipo", "종목 기본 정보"),
    ("score", "종목 평가 점수"),
    ("why", "점수의 주요 근거"),
    ("calc", "증권사별 예상 배정"),
    ("trend", "뉴스·검색 추이"),
    ("financial", "재무 요약"),
    ("result", "상장 후 검증"),
    ("sources", "데이터 출처와 갱신 시각"),
    ("status", "수집 작업 상태"),
    ("refresh", "종목 데이터 다시 수집(관리자)"),
    ("help", "명령어 도움말"),
)


def help_text() -> str:
    lines = ["[공모주 알리미 도움말]"]
    lines.extend(f"/{command} — {description}" for command, description in TELEGRAM_COMMANDS)
    lines.extend(
        [
            "",
            "예: /calc 기업명 1000만원 한국투자증권",
            "동명 종목이나 복수 증권사는 후보를 확인한 뒤 명시해 주세요.",
        ]
    )
    return "\n".join(lines)


def send_telegram_message(
    text: str,
    chat_id: Optional[str] = None,
    *,
    event_key: Optional[str] = None,
) -> dict[str, Any]:
    settings = get_settings()
    if not settings.telegram_bot_token:
        raise ValueError("TELEGRAM_BOT_TOKEN is missing")
    recipient = chat_id or settings.telegram_chat_id
    if not recipient:
        raise ValueError("TELEGRAM_CHAT_ID is missing")
    event = None
    if event_key:
        event, created = reserve_notification("telegram", event_key, str(recipient))
        if not created and event.status in {"pending", "sent"}:
            return {"ok": True, "duplicate": True, "event_key": event_key}
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    responses = []
    try:
        # A timed-out POST may already have reached Telegram, so do not retry it blindly.
        with ResilientHttpClient(timeout=settings.http_timeout_sec, max_retries=0) as client:
            for chunk in split_telegram_message(text):
                response = client.post_json(
                    url,
                    source="Telegram",
                    json={
                        "chat_id": recipient,
                        "text": chunk,
                        "disable_web_page_preview": True,
                    },
                )
                if not response.get("ok", False):
                    raise ValueError("Telegram API rejected the message")
                responses.append(response)
    except Exception as exc:
        if event:
            finish_notification(event.id, status="failed", error=f"{type(exc).__name__}: {exc}")
        raise
    if event:
        finish_notification(event.id, status="sent", response={"chunks": len(responses)})
    return {"ok": True, "chunks": len(responses), "responses": responses}


def split_telegram_message(text: str, limit: int = 3900) -> list[str]:
    if limit <= 0:
        raise ValueError("Telegram chunk limit must be positive")
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:
            prefix, line = line[:limit], line[limit:]
            if current:
                chunks.append(current.rstrip())
                current = ""
            chunks.append(prefix.rstrip())
        if len(current) + len(line) > limit:
            chunks.append(current.rstrip())
            current = line
        else:
            current += line
    if current:
        chunks.append(current.rstrip())
    return [chunk for chunk in chunks if chunk]


def is_chat_authorized(chat_id: Any, allowed_chat_ids: tuple[str, ...]) -> bool:
    return chat_id is not None and str(chat_id) in allowed_chat_ids


def consume_refresh_cooldown(
    user_id: Any,
    last_used: dict[str, float],
    *,
    now: Optional[float] = None,
    cooldown_seconds: int = 60,
) -> int:
    current = time.monotonic() if now is None else now
    key = str(user_id)
    remaining = cooldown_seconds - (current - last_used.get(key, -float("inf")))
    if remaining > 0:
        return math.ceil(remaining)
    last_used[key] = current
    return 0


def run_bot() -> None:
    from telegram import Update
    from telegram.ext import Application, CommandHandler, ContextTypes

    settings = get_settings()
    if not settings.telegram_bot_token:
        raise ValueError("TELEGRAM_BOT_TOKEN is missing")
    if not settings.allowed_telegram_chat_ids:
        raise ValueError("TELEGRAM_ALLOWED_CHAT_IDS or TELEGRAM_CHAT_ID must be configured")
    refresh_last_used: dict[str, float] = {}

    async def authorized(update: Update) -> bool:
        chat = update.effective_chat
        if chat and is_chat_authorized(chat.id, settings.allowed_telegram_chat_ids):
            return True
        logger.warning("Rejected unauthorized Telegram chat: %s", getattr(chat, "id", "unknown"))
        if update.effective_message:
            await update.effective_message.reply_text("허용되지 않은 요청입니다.")
        return False

    async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await authorized(update):
            return
        await _reply(update, help_text())

    async def today_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await authorized(update):
            return
        today = local_today()
        await _reply(update, await asyncio.to_thread(_schedule_text, "오늘 일정", today, today))

    async def week_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await authorized(update):
            return
        today = local_today()
        await _reply(
            update,
            await asyncio.to_thread(
                _schedule_text,
                "이번 주 일정",
                today,
                today + timedelta(days=6),
            ),
        )

    async def ipo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await authorized(update):
            return
        name = _required_name(context, "/ipo 종목명")
        await _reply(update, await asyncio.to_thread(_ipo_text, name))

    async def score_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await authorized(update):
            return
        name = _required_name(context, "/score 종목명")
        await _reply(update, await asyncio.to_thread(_score_text, name))

    async def calc_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await authorized(update):
            return
        if len(context.args) < 2:
            raise ValueError("사용법: /calc 종목명 금액 [증권사]")
        amount_index = next(
            (index for index, value in enumerate(context.args) if _looks_like_amount(value)),
            len(context.args) - 1,
        )
        name = " ".join(context.args[:amount_index]).strip()
        amount = _parse_amount(context.args[amount_index])
        broker = " ".join(context.args[amount_index + 1 :]).strip() or None
        if not name:
            raise ValueError("사용법: /calc 종목명 금액 [증권사]")
        await _reply(update, await asyncio.to_thread(_allocation_text, name, amount, broker))

    async def trend_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await authorized(update):
            return
        name = _required_name(context, "/trend 종목명")
        await _reply(update, await asyncio.to_thread(_trend_text, name))

    async def financial_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await authorized(update):
            return
        name = _required_name(context, "/financial 종목명")
        await _reply(update, await asyncio.to_thread(_financial_text, name))

    async def result_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await authorized(update):
            return
        name = _required_name(context, "/result 종목명")
        await _reply(update, await asyncio.to_thread(_result_text, name))

    async def backtest_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await authorized(update):
            return
        await _reply(update, await asyncio.to_thread(_backtest_text))

    async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await authorized(update):
            return
        await _reply(update, await asyncio.to_thread(_status_text))

    async def sources_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await authorized(update):
            return
        name = _required_name(context, "/sources 종목명")
        await _reply(update, await asyncio.to_thread(_sources_text, name))

    async def why_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await authorized(update):
            return
        name = _required_name(context, "/why 종목명")
        await _reply(update, await asyncio.to_thread(_why_text, name))

    async def refresh_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await authorized(update):
            return
        user = update.effective_user
        if not user or str(user.id) not in settings.allowed_telegram_user_ids:
            raise ValueError(
                "/refresh 권한이 없습니다. 개인 user id를 관리자 목록에 등록해 주세요."
            )
        remaining = consume_refresh_cooldown(user.id, refresh_last_used)
        if remaining:
            raise ValueError(f"/refresh는 {remaining}초 후 다시 실행할 수 있습니다.")
        name = _required_name(context, "/refresh 종목명")
        await _reply(update, "수집을 시작합니다. 완료까지 시간이 걸릴 수 있습니다.")
        await _reply(update, await asyncio.to_thread(_refresh_text, name))

    async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = str(context.error)
        safe = (
            message
            if isinstance(context.error, ValueError)
            else "요청 처리 중 오류가 발생했습니다."
        )
        if isinstance(update, Update):
            await _reply(update, safe)

    async def register_commands(application: Application) -> None:
        await application.bot.set_my_commands(TELEGRAM_COMMANDS)

    app = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .post_init(register_commands)
        .build()
    )
    handlers = {
        "start": help_cmd,
        "help": help_cmd,
        "today": today_cmd,
        "week": week_cmd,
        "ipo": ipo_cmd,
        "score": score_cmd,
        "calc": calc_cmd,
        "trend": trend_cmd,
        "financial": financial_cmd,
        "result": result_cmd,
        "backtest": backtest_cmd,
        "status": status_cmd,
        "sources": sources_cmd,
        "why": why_cmd,
        "refresh": refresh_cmd,
    }
    for command, handler in handlers.items():
        app.add_handler(CommandHandler(command, handler))
    app.add_error_handler(error_handler)
    app.run_polling(drop_pending_updates=False)


def _schedule_text(title: str, start: date, end: date) -> str:
    with session_scope() as session:
        rows = list_ipos_between(session, start, end)
        for ipo in rows:
            _ = ipo.underwriters
        return format_schedule(title, rows, start, end)


def _ipo_text(name: str) -> str:
    with session_scope() as session:
        ipo = _find_or_raise(session, name)
        return format_ipo_summary(ipo)


def _score_text(name: str) -> str:
    with session_scope() as session:
        ipo = _find_or_raise(session, name)
        return format_score(ipo, latest_score(session, ipo.id))


def _allocation_text(name: str, amount: int, broker: Optional[str]) -> str:
    with session_scope() as session:
        ipo = _find_or_raise(session, name)
        underwriter = resolve_underwriter(ipo, broker)
        assumptions = derive_allocation_assumptions(session, underwriter)
        result = build_allocation_scenario(ipo, underwriter, amount, assumptions=assumptions)
        save_allocation_result(session, result)
        return format_allocation(ipo, result)


def _trend_text(name: str) -> str:
    with session_scope() as session:
        ipo = _find_or_raise(session, name)
        news = (
            session.execute(
                select(NewsMetric)
                .where(NewsMetric.ipo_id == ipo.id)
                .order_by(desc(NewsMetric.updated_at))
            )
            .scalars()
            .first()
        )
        trend = (
            session.execute(
                select(SearchTrend)
                .where(SearchTrend.ipo_id == ipo.id)
                .order_by(desc(SearchTrend.updated_at))
            )
            .scalars()
            .first()
        )
        return format_trend(ipo, news, trend)


def _financial_text(name: str) -> str:
    with session_scope() as session:
        ipo = _find_or_raise(session, name)
        metric = (
            session.execute(
                select(FinancialMetric)
                .where(FinancialMetric.ipo_id == ipo.id)
                .order_by(desc(FinancialMetric.fiscal_year), desc(FinancialMetric.updated_at))
            )
            .scalars()
            .first()
        )
        return format_financial(ipo, metric)


def _result_text(name: str) -> str:
    with session_scope() as session:
        ipo = _find_or_raise(session, name)
        evaluation = (
            session.execute(
                select(OutcomeEvaluation)
                .where(OutcomeEvaluation.ipo_id == ipo.id)
                .order_by(desc(OutcomeEvaluation.evaluated_at))
            )
            .scalars()
            .first()
        )
        return format_result(ipo, evaluation)


def _backtest_text() -> str:
    with session_scope() as session:
        return format_backtest(build_backtest_report(session))


def _status_text() -> str:
    rows = latest_job_statuses()
    if not rows:
        return "[수집 상태]\n잡 실행 이력이 없습니다."
    lines = ["[수집 상태]"]
    for row in rows:
        finished = format_local_datetime(row["finished_at"]) if row["finished_at"] else "실행 중"
        lines.append(
            f"- {row['job_name']}: {row['status']} / {finished} / "
            f"성공 {row['success_count']} · 부분 {row['partial_count']} · "
            f"건너뜀 {row['skipped_count']} · 실패 {row['failure_count']}"
        )
        if row["error_summary"]:
            lines.append(f"  오류: {row['error_summary']}")
    return "\n".join(lines)


def _sources_text(name: str) -> str:
    with session_scope() as session:
        ipo = _find_or_raise(session, name)
        snapshots = list(
            session.execute(
                select(SourceSnapshot)
                .where(SourceSnapshot.ipo_id == ipo.id)
                .order_by(desc(SourceSnapshot.fetched_at))
                .limit(10)
            ).scalars()
        )
        score = latest_score(session, ipo.id)
        reviews = list(
            session.execute(
                select(DiligenceReview)
                .where(DiligenceReview.ipo_id == ipo.id)
                .order_by(DiligenceReview.review_type)
            ).scalars()
        )
        lines = [f"[데이터 출처] {ipo.corp_name}"]
        for snapshot in snapshots:
            lines.append(
                f"- {snapshot.source}: {snapshot.source_key} / "
                f"{format_local_datetime(snapshot.fetched_at)}"
            )
        if score:
            lines.append(f"- score: {score.model_version} / rules {score.rules_hash or '-'}")
        for review in reviews:
            lines.append(
                f"- review/{review.review_type}: {review.review_status} / "
                f"{format_local_datetime(review.reviewed_at)}"
            )
        if len(lines) == 1:
            lines.append("저장된 출처가 없습니다.")
        return "\n".join(lines)


def _why_text(name: str) -> str:
    with session_scope() as session:
        ipo = _find_or_raise(session, name)
        score = latest_score(session, ipo.id)
        if score is None:
            return f"[점수 근거] {ipo.corp_name}\n점수 스냅샷이 없습니다."
        component_scores = [
            ("기관 수요예측", score.demand_score),
            ("재무 품질", score.financial_score),
            ("밸류에이션", score.valuation_score),
            ("공모 구조", score.structure_score),
            ("뉴스·검색", score.news_trend_score),
            ("예상 배정", score.allocation_score),
            ("시장 환경", score.market_condition_score),
        ]
        strongest = sorted(component_scores, key=lambda item: item[1] or 0, reverse=True)[:3]
        lines = [
            f"[점수 근거] {ipo.corp_name}",
            f"종합 {score.total_score:.1f}점 · {score.recommendation_band}",
            f"데이터 완성도 {score.confidence_score:.1f}%",
            "",
            "상위 기여 요인:",
        ]
        lines.extend(f"- {label}: {value or 0:.1f}점" for label, value in strongest)
        lines.append(f"- 리스크 차감: {score.risk_penalty_score or 0:.1f}점")
        missing = score.missing_features_json or []
        lines.append("누락 데이터: " + (", ".join(missing) if missing else "없음"))
        lines.append("상세 산식은 모델 버전과 입력 지문으로 보존됩니다.")
        return "\n".join(lines)


def _refresh_text(name: str) -> str:
    from app.services.jobs import (
        collect_dart_for_ipo,
        collect_financials_for_ipo,
        collect_news_trend_for_ipo,
        score_one,
    )

    operations = (
        ("공시", collect_dart_for_ipo),
        ("재무", collect_financials_for_ipo),
        ("뉴스·검색", collect_news_trend_for_ipo),
        ("점수", score_one),
    )
    lines = ["[수집 결과]"]
    for label, operation in operations:
        try:
            result = operation(name)
            if result.get("skipped"):
                state = f"건너뜀 · {result.get('reason', '-')}"
            elif result.get("errors"):
                state = f"부분 성공 · {'; '.join(result['errors'])}"
            elif result.get("updated") is False:
                state = f"미갱신 · {result.get('reason', '-')}"
            else:
                state = "완료"
        except Exception as exc:
            state = f"실패 · {type(exc).__name__}: {exc}"
        lines.append(f"- {label}: {state}")
    return "\n".join(lines)


async def _reply(update: Any, text: str) -> None:
    if update.effective_message:
        for chunk in split_telegram_message(text):
            await update.effective_message.reply_text(chunk, disable_web_page_preview=True)


def _required_name(context: Any, usage: str) -> str:
    value = " ".join(context.args).strip()
    if not value:
        raise ValueError(f"사용법: {usage}")
    return value


def _find_or_raise(session: Any, name: str):
    try:
        return get_ipo_or_raise(session, name)
    except ValueError as exc:
        message = str(exc).replace("IPO not found", "IPO를 찾지 못했습니다")
        message = message.replace("IPO name is ambiguous", "같은 이름의 IPO가 여러 건입니다")
        raise ValueError(message) from exc


def _looks_like_amount(value: str) -> bool:
    normalized = value.replace(",", "").lower()
    try:
        float(normalized.removesuffix("만원").removesuffix("억"))
    except ValueError:
        return False
    return True


def _parse_amount(text: str) -> int:
    normalized = text.replace(",", "").strip().lower()
    multiplier = 1
    if normalized.endswith("만원"):
        multiplier = 10_000
        normalized = normalized[:-2]
    elif normalized.endswith("억"):
        multiplier = 100_000_000
        normalized = normalized[:-1]
    try:
        amount = int(float(normalized) * multiplier)
    except ValueError as exc:
        raise ValueError(f"금액 형식이 올바르지 않습니다: {text}") from exc
    if not 0 < amount <= 10_000_000_000_000:
        raise ValueError("금액은 0원 초과 10조원 이하여야 합니다.")
    return amount
