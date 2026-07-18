from __future__ import annotations

import argparse
import json
from datetime import date, timedelta

from app.db import init_db, migrate_db, session_scope
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
from app.services.clock import local_today
from app.services.formatter import (
    format_allocation,
    format_backtest,
    format_financial,
    format_health,
    format_ipo_summary,
    format_result,
    format_schedule,
    format_score,
    format_trend,
)
from app.services.jobs import (
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
    review_diligence,
    score_all_active,
    score_one,
    seed_sample_data,
    send_daily_summary,
)
from app.services.observability import latest_job_statuses
from app.services.scheduler import run_scheduler
from app.services.telegram import run_bot, send_telegram_message


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return
    args.func(args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Korea IPO Telegram automation")
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
    _cmd(sub, "status", cmd_status)
    _cmd(sub, "send-daily-summary", cmd_send_daily_summary)
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
        ipos = list_ipos_between(session, today, today + timedelta(days=7))
        print(format_schedule("이번 주 일정", ipos, today, today + timedelta(days=7)))


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
        print(format_backtest(build_backtest_report(session)))


def cmd_status(args) -> None:
    print_json({"jobs": latest_job_statuses()})


def cmd_send_daily_summary(args) -> None:
    print_json(send_daily_summary())


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


def print_json(payload) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
