from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional
from uuid import uuid4

from app.config import DEFAULT_EMAIL_RECIPIENT, get_settings
from app.db import init_db, migrate_db, session_scope
from app.models import UserParticipation
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
from app.services.backtest import build_operational_backtest
from app.services.clock import local_today
from app.services.email import EmailContent, send_email_message
from app.services.formatter import (
    format_allocation,
    format_financial,
    format_health,
    format_ipo_summary,
    format_operational_backtest,
    format_result,
    format_schedule,
    format_score,
    format_trend,
)
from app.services.jobs import (
    backfill_historical_outcomes,
    build_daily_email_content,
    build_monthly_email_content,
    build_subscription_d_minus_one_email_batch,
    build_weekly_email_content,
    collect_dart_for_ipo,
    collect_financials_for_ipo,
    collect_kind_schedule,
    collect_market_condition,
    collect_news_trend_for_ipo,
    collect_prices_for_ipo,
    evaluate_outcome_for_ipo,
    healthcheck,
    latest_financial_metric,
    latest_news_metric,
    latest_outcome,
    latest_search_trend,
    mark_diligence_review,
    record_allocation_actual,
    refresh_statuses,
    resolve_ticker_for_ipo,
    review_diligence,
    score_all_active,
    score_one,
    seed_sample_data,
    send_daily_email_summary,
    send_daily_summary,
    send_monthly_email_snapshot,
    send_weekly_email_summary,
    subscription_email_event_key,
)
from app.services.observability import latest_job_statuses
from app.services.outcome import latest_actionable_score
from app.services.participation import (
    list_participation_summaries,
    record_exit,
    record_participation,
    summarize_participation,
    summarize_participation_portfolio,
)
from app.services.scheduler import run_scheduler
from app.services.scoring_v3 import resolve_scoring_identity
from app.services.telegram import run_bot, send_telegram_message
from app.services.validation import (
    assess_data_readiness,
    build_validation_dataset,
    write_validation_csv,
)


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return
    args.func(args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Korea IPO Assistant automation")
    sub = parser.add_subparsers(dest="command")

    _cmd(sub, "healthcheck", cmd_healthcheck)
    _cmd(sub, "init-db", cmd_init_db)
    _cmd(sub, "seed-sample", cmd_seed_sample)
    collect_schedule = _cmd(sub, "collect-schedule", cmd_collect_schedule)
    collect_schedule.add_argument("--from-date", dest="from_date")
    collect_schedule.add_argument("--to-date", dest="to_date")

    collect_dart = _cmd(sub, "collect-dart", cmd_collect_dart)
    collect_dart.add_argument("name")
    collect_financials = _cmd(sub, "collect-financials", cmd_collect_financials)
    collect_financials.add_argument("name")
    collect_financials.add_argument("--years", type=int, default=3)
    collect_news = _cmd(sub, "collect-news-trend", cmd_collect_news_trend)
    collect_news.add_argument("name")
    collect_prices = _cmd(sub, "collect-prices", cmd_collect_prices)
    collect_prices.add_argument("name")
    resolve_ticker = _cmd(sub, "resolve-ticker", cmd_resolve_ticker)
    resolve_ticker.add_argument("name")
    backfill_history = _cmd(sub, "backfill-history", cmd_backfill_history)
    backfill_history.add_argument("--from-date", required=True, dest="from_date")
    backfill_history.add_argument("--to-date", dest="to_date")
    _cmd(sub, "collect-market", cmd_collect_market)
    _cmd(sub, "refresh-statuses", cmd_refresh_statuses)

    score = _cmd(sub, "score", cmd_score)
    score.add_argument("name", nargs="?")
    score.add_argument("--all", action="store_true")
    review = _cmd(sub, "review-diligence", cmd_review_diligence)
    review.add_argument("name", nargs="?")
    review.add_argument("--min-news-articles", type=int, default=1)
    review.add_argument("--max-age-days", type=int, default=14)
    mark_review = _cmd(sub, "mark-reviewed", cmd_mark_reviewed)
    mark_review.add_argument("name")
    mark_review.add_argument("review_type", choices=["disclosure", "articles"])
    mark_review.add_argument("--by", required=True, dest="reviewed_by")
    mark_review.add_argument("--note", default="")
    mark_review.add_argument("--evidence", action="append", default=[])
    allocation_actual = _cmd(sub, "record-allocation-actual", cmd_record_allocation_actual)
    allocation_actual.add_argument("name")
    allocation_actual.add_argument("broker")
    allocation_actual.add_argument("applicants", type=int)
    allocation_actual.add_argument("competition", type=float)
    allocation_actual.add_argument("--source", default="manual")
    participation = _cmd(sub, "record-participation", cmd_record_participation)
    participation.add_argument("name")
    participation.add_argument("broker")
    participation.add_argument("--cash")
    participation.add_argument("--applied-shares", type=int, required=True)
    participation.add_argument("--cash-committed", required=True)
    participation.add_argument("--allocated-shares", type=int)
    participation.add_argument("--fee", type=int, default=0)
    participation.add_argument(
        "--action",
        default="RESEARCH_ONLY",
        choices=["CANDIDATE", "RESEARCH_ONLY", "VALUE_SKIP", "DATA_REQUIRED", "BLOCKED"],
    )
    participation.add_argument("--decision-at")
    participation.add_argument("--note", default="")
    exit_record = _cmd(sub, "record-exit", cmd_record_exit)
    exit_record.add_argument("participation_id", type=int)
    exit_record.add_argument("--shares", type=int, required=True)
    exit_record.add_argument("--price", required=True)
    exit_record.add_argument("--cost", type=int, default=0)
    exit_record.add_argument("--strategy", default="MANUAL")
    exit_record.add_argument("--sold-at")
    exit_record.add_argument("--note", default="")
    participation_summary = _cmd(sub, "participation-summary", cmd_participation_summary)
    participation_summary.add_argument("name", nargs="?")

    today = _cmd(sub, "today", cmd_today)
    today.add_argument("--days", type=int, default=0)
    _cmd(sub, "week", cmd_week)
    ipo = _cmd(sub, "ipo", cmd_ipo)
    ipo.add_argument("name")
    calc = _cmd(sub, "calc", cmd_calc)
    calc.add_argument("name")
    calc.add_argument("amount")
    calc.add_argument("--broker")
    trend = _cmd(sub, "trend", cmd_trend)
    trend.add_argument("name")
    financial = _cmd(sub, "financial", cmd_financial)
    financial.add_argument("name")
    result = _cmd(sub, "result", cmd_result)
    result.add_argument("name")
    evaluate = _cmd(sub, "evaluate", cmd_evaluate)
    evaluate.add_argument("name")
    _cmd(sub, "backtest", cmd_backtest)
    validation = _cmd(sub, "validation-report", cmd_validation_report)
    validation.add_argument("--model-version")
    validation.add_argument("--rules-hash")
    validation.add_argument("--minimum-samples", type=int, default=30)
    validation.add_argument("--csv", dest="csv_path")
    _cmd(sub, "status", cmd_status)
    _cmd(sub, "send-daily-summary", cmd_send_daily_summary)
    _cmd(sub, "send-daily-email", cmd_send_daily_email)
    _cmd(sub, "send-weekly-email", cmd_send_weekly_email)
    preview_email = _cmd(sub, "preview-email", cmd_preview_email)
    preview_email.add_argument("--weekly", action="store_true")
    preview_email.add_argument("--html", action="store_true")
    preview_monthly = _cmd(sub, "preview-monthly-email", cmd_preview_monthly_email)
    preview_monthly.add_argument("--month", help="calendar month in YYYY-MM format")
    preview_monthly.add_argument("--html", action="store_true")
    preview_monthly.add_argument("--part", type=int)
    preview_d1 = _cmd(sub, "preview-d1-emails", cmd_preview_d1_emails)
    preview_d1.add_argument("--send-date", help="KST scheduler date in YYYY-MM-DD")
    preview_d1.add_argument("--ipo", help="show one ready IPO by exact company name")
    preview_d1.add_argument("--html", action="store_true")
    preview_d1.add_argument("--manifest-only", action="store_true")
    send_monthly = _cmd(sub, "send-monthly-email", cmd_send_monthly_email)
    send_monthly.add_argument("--month", help="calendar month in YYYY-MM format")
    send_monthly.add_argument("--canary", action="store_true")
    send_test_email = _cmd(sub, "send-test-email", cmd_send_test_email)
    send_test_email.add_argument("--strict", action="store_true")
    send_test = _cmd(sub, "send-test-telegram", cmd_send_test_telegram)
    send_test.add_argument("--strict", action="store_true")
    _cmd(sub, "run-bot", cmd_run_bot)
    _cmd(sub, "run-scheduler", cmd_run_scheduler)
    return parser


def _cmd(sub, name: str, func):
    command = sub.add_parser(name)
    command.set_defaults(func=func)
    return command


def cmd_healthcheck(args) -> None:
    status = healthcheck()
    print(format_health(status))
    if not status.database_ok or not status.schema_current:
        raise SystemExit(1)


def cmd_init_db(args) -> None:
    migrate_db()
    print("database migrated")


def cmd_seed_sample(args) -> None:
    print_json(seed_sample_data())


def cmd_collect_schedule(args) -> None:
    print_json(collect_kind_schedule(_parse_date(args.from_date), _parse_date(args.to_date)))


def cmd_collect_dart(args) -> None:
    print_json(collect_dart_for_ipo(args.name))


def cmd_collect_financials(args) -> None:
    print_json(collect_financials_for_ipo(args.name, years=args.years))


def cmd_collect_news_trend(args) -> None:
    print_json(collect_news_trend_for_ipo(args.name))


def cmd_collect_prices(args) -> None:
    print_json(collect_prices_for_ipo(args.name))


def cmd_resolve_ticker(args) -> None:
    print_json(resolve_ticker_for_ipo(args.name))


def cmd_backfill_history(args) -> None:
    start = _parse_date(args.from_date)
    end = _parse_date(args.to_date) or local_today()
    print_json(backfill_historical_outcomes(start, end))


def cmd_collect_market(args) -> None:
    print_json(collect_market_condition())


def cmd_refresh_statuses(args) -> None:
    print_json(refresh_statuses())


def cmd_score(args) -> None:
    if args.all or not args.name:
        print_json(score_all_active())
    else:
        score_one(args.name)
        with session_scope() as session:
            ipo = _find_or_exit(session, args.name)
            print(format_score(ipo, latest_score(session, ipo.id)))


def cmd_review_diligence(args) -> None:
    print_json(
        review_diligence(
            name=args.name,
            min_news_articles=args.min_news_articles,
            max_age_days=args.max_age_days,
        )
    )


def cmd_mark_reviewed(args) -> None:
    print_json(
        mark_diligence_review(
            args.name,
            args.review_type,
            args.reviewed_by,
            note=args.note,
            evidence_refs=args.evidence,
        )
    )


def cmd_record_allocation_actual(args) -> None:
    print_json(
        record_allocation_actual(
            args.name,
            args.broker,
            args.applicants,
            args.competition,
            source=args.source,
        )
    )


def cmd_record_participation(args) -> None:
    init_db()
    user_cash = _parse_amount(args.cash) if args.cash else get_settings().default_user_cash
    cash_committed = _parse_amount(args.cash_committed)
    with session_scope() as session:
        ipo = _find_or_exit(session, args.name)
        model_version, rules_hash = resolve_scoring_identity()
        score = latest_actionable_score(
            session,
            ipo.id,
            ipo.subscription_start_date,
            ipo.actual_listing_date or ipo.listing_date,
            model_version=model_version,
            rules_hash=rules_hash,
        )
        row = record_participation(
            session,
            ipo,
            broker_name=args.broker,
            decision_action=args.action,
            user_cash=user_cash,
            applied_shares=args.applied_shares,
            cash_committed=cash_committed,
            allocated_shares=args.allocated_shares,
            subscription_fee=args.fee,
            score=score,
            decision_at=_parse_datetime(args.decision_at),
            note=args.note,
        )
        print_json(summarize_participation(row).to_dict())


def cmd_record_exit(args) -> None:
    init_db()
    with session_scope() as session:
        participation = session.get(UserParticipation, args.participation_id)
        if participation is None:
            raise SystemExit(f"Participation not found: {args.participation_id}")
        record_exit(
            session,
            participation,
            sold_shares=args.shares,
            sell_price=_parse_amount(args.price),
            strategy=args.strategy,
            selling_cost=args.cost,
            sold_at=_parse_datetime(args.sold_at),
            note=args.note,
        )
        print_json(summarize_participation(participation).to_dict())


def cmd_participation_summary(args) -> None:
    init_db()
    with session_scope() as session:
        ipo_id = _find_or_exit(session, args.name).id if args.name else None
        rows = list_participation_summaries(session, ipo_id=ipo_id)
        print_json(
            {
                "portfolio": summarize_participation_portfolio(rows).to_dict(),
                "participations": [row.to_dict() for row in rows],
            }
        )


def cmd_today(args) -> None:
    init_db()
    target = local_today() + timedelta(days=args.days)
    with session_scope() as session:
        ipos = list_ipos_between(session, target, target)
        print(format_schedule("오늘 일정", ipos, target, target))


def cmd_week(args) -> None:
    init_db()
    today = local_today()
    with session_scope() as session:
        end = today + timedelta(days=6)
        ipos = list_ipos_between(session, today, end)
        print(format_schedule("이번 주 일정", ipos, today, end))


def cmd_ipo(args) -> None:
    init_db()
    with session_scope() as session:
        ipo = _find_or_exit(session, args.name)
        print(format_ipo_summary(ipo))


def cmd_calc(args) -> None:
    init_db()
    amount = _parse_amount(args.amount)
    with session_scope() as session:
        ipo = _find_or_exit(session, args.name)
        try:
            underwriter = resolve_underwriter(ipo, args.broker)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        assumptions = derive_allocation_assumptions(session, underwriter)
        result = build_allocation_scenario(ipo, underwriter, amount, assumptions=assumptions)
        save_allocation_result(session, result)
        print(format_allocation(ipo, result))


def cmd_trend(args) -> None:
    init_db()
    with session_scope() as session:
        ipo = _find_or_exit(session, args.name)
        print(
            format_trend(
                ipo, latest_news_metric(session, ipo.id), latest_search_trend(session, ipo.id)
            )
        )


def cmd_financial(args) -> None:
    init_db()
    with session_scope() as session:
        ipo = _find_or_exit(session, args.name)
        print(format_financial(ipo, latest_financial_metric(session, ipo.id)))


def cmd_result(args) -> None:
    init_db()
    with session_scope() as session:
        ipo = _find_or_exit(session, args.name)
        print(format_result(ipo, latest_outcome(session, ipo.id)))


def cmd_evaluate(args) -> None:
    print_json(evaluate_outcome_for_ipo(args.name))


def cmd_backtest(args) -> None:
    init_db()
    with session_scope() as session:
        print(format_operational_backtest(build_operational_backtest(session)))


def cmd_validation_report(args) -> None:
    init_db()
    try:
        model_version, rules_hash = resolve_scoring_identity(
            args.model_version,
            args.rules_hash,
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    with session_scope() as session:
        readiness = assess_data_readiness(
            session,
            model_version=model_version,
            rules_hash=rules_hash,
            minimum_samples=args.minimum_samples,
        )
        rows = build_validation_dataset(
            session,
            model_version=model_version,
            rules_hash=rules_hash,
        )
    output = None
    if args.csv_path:
        output = write_validation_csv(rows, Path(args.csv_path).expanduser().resolve())
    print_json(
        {
            "readiness": readiness.to_dict(),
            "validation_rows": len(rows),
            "csv": str(output) if output else None,
        }
    )


def cmd_status(args) -> None:
    print_json({"jobs": latest_job_statuses()})


def cmd_send_daily_summary(args) -> None:
    print_json(send_daily_summary())


def cmd_send_daily_email(args) -> None:
    print_json(send_daily_email_summary())


def cmd_send_weekly_email(args) -> None:
    print_json(send_weekly_email_summary())


def cmd_preview_email(args) -> None:
    content = (build_weekly_email_content() if args.weekly else build_daily_email_content())[0]
    print(content.html if args.html else content.text)


def cmd_preview_monthly_email(args) -> None:
    bundle = build_monthly_email_content(_parse_month(args.month))
    if args.part is not None:
        if args.part < 1 or args.part > len(bundle.parts):
            raise SystemExit(f"part must be between 1 and {len(bundle.parts)}")
        parts = (bundle.parts[args.part - 1],)
    else:
        parts = bundle.parts
    print("\n\n".join(part.html if args.html else part.text for part in parts))


def cmd_preview_d1_emails(args) -> None:
    """Render the production D-1 batch without SMTP or notification-ledger writes."""
    send_date = _parse_date(args.send_date) or local_today()
    batch = build_subscription_d_minus_one_email_batch(send_date)
    ready = batch.ready
    if args.ipo:
        ready = tuple(item for item in ready if item.corp_name.casefold() == args.ipo.casefold())
        if not ready:
            raise SystemExit(f"Ready D-1 IPO not found: {args.ipo}")
    reports = [
        {
            "ipo_id": candidate.ipo_id,
            "corp_name": candidate.corp_name,
            "subject": candidate.bundle.parts[0].subject,
            "event_key": subscription_email_event_key(candidate),
            "universe_hash": candidate.bundle.universe_hash,
            "evidence_hash": candidate.bundle.evidence_hash,
            "html_bytes": len(candidate.bundle.parts[0].html.encode("utf-8")),
            "evidence_warnings": list(candidate.evidence_warnings),
        }
        for candidate in ready
    ]
    print_json(
        {
            "send_date": batch.run_date.isoformat(),
            "subscription_date": batch.subscription_date.isoformat(),
            "recipient": DEFAULT_EMAIL_RECIPIENT,
            "candidate_count": batch.total_candidates,
            "ready_count": len(batch.ready),
            "selected_count": len(ready),
            "blocked_count": len(batch.blocked),
            "blocked": list(batch.blocked),
            "reports": reports,
            "external_send": False,
        }
    )
    if args.manifest_only:
        return
    for index, candidate in enumerate(ready, 1):
        content = candidate.bundle.parts[0]
        print(f"\n===== D-1 REPORT {index}/{len(ready)} · {content.subject} =====\n")
        print(content.html if args.html else content.text)


def cmd_send_monthly_email(args) -> None:
    print_json(
        send_monthly_email_snapshot(
            _parse_month(args.month),
            trigger="canary" if args.canary else "manual",
        )
    )


def cmd_send_test_email(args) -> None:
    content = EmailContent(
        subject="[Korea IPO] SMTP 연결 테스트",
        text="Korea IPO Assistant SMTP 연결 테스트입니다.",
        html="<p>Korea IPO Assistant SMTP 연결 테스트입니다.</p>",
    )
    try:
        print_json(send_email_message(content, event_key=f"smtp-connection-test:{uuid4().hex}"))
    except Exception as exc:
        if args.strict:
            raise
        print_json({"sent": False, "reason": str(exc)})


def cmd_send_test_telegram(args) -> None:
    try:
        result = send_telegram_message("[IPO Bot] 테스트 메시지")
        print_json(result)
    except Exception as exc:
        if args.strict:
            raise
        print_json({"sent": False, "reason": str(exc)})


def cmd_run_bot(args) -> None:
    init_db()
    run_bot()


def cmd_run_scheduler(args) -> None:
    run_scheduler()


def _find_or_exit(session, name: str):
    try:
        return get_ipo_or_raise(session, name)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc


def _parse_date(value):
    if not value:
        return None
    return date.fromisoformat(value)


def _parse_month(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    try:
        parsed = datetime.strptime(value, "%Y-%m").date()
    except ValueError as exc:
        raise SystemExit(f"Invalid month: {value}; expected YYYY-MM") from exc
    return parsed.replace(day=1)


def _parse_amount(text_value: str) -> int:
    normalized = text_value.replace(",", "").strip().lower()
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
        raise SystemExit(f"Invalid amount: {text_value}") from exc
    if not 0 < amount <= 10_000_000_000_000:
        raise SystemExit("Amount must be greater than 0 and at most 10 trillion KRW")
    return amount


def _parse_datetime(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SystemExit(f"Invalid ISO-8601 datetime: {raw}") from exc
    if parsed.tzinfo is None:
        raise SystemExit("Datetime must include an explicit UTC offset or Z")
    return parsed


def print_json(payload) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
