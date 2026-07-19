from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Callable

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import get_settings
from app.services.jobs import (
    collect_dart_for_active,
    collect_financials_for_active,
    collect_kind_listings_for_current_month,
    collect_kind_listings_for_subscription_d_minus_one,
    collect_kind_schedule,
    collect_kind_schedule_for_subscription_d_minus_one,
    collect_market_condition,
    collect_news_for_active,
    collect_prices_due,
    collect_public_dart_for_current_month,
    collect_public_dart_for_subscription_d_minus_one,
    evaluate_outcomes_due,
    reconcile_current_month_evidence,
    reconcile_subscription_d_minus_one_evidence,
    refresh_statuses,
    score_all_active,
    send_daily_summary,
    send_monthly_backtest,
    send_subscription_d_minus_one_emails,
    send_weekly_summary,
)
from app.services.observability import mask_secrets, run_tracked_job

logger = logging.getLogger(__name__)


def job_definitions() -> list[tuple[str, Callable, dict]]:
    return [
        ("collect_kind_schedule", collect_kind_schedule, {"hour": 7, "minute": 0}),
        (
            "collect_kind_listings_for_current_month",
            collect_kind_listings_for_current_month,
            {"hour": 7, "minute": 5},
        ),
        ("refresh_statuses", refresh_statuses, {"hour": 7, "minute": 10}),
        (
            "collect_public_dart_for_current_month",
            collect_public_dart_for_current_month,
            {"hour": 7, "minute": 15},
        ),
        ("collect_dart_for_active", collect_dart_for_active, {"hour": 7, "minute": 35}),
        ("collect_financials_for_active", collect_financials_for_active, {"hour": 7, "minute": 40}),
        ("collect_news_for_active", collect_news_for_active, {"hour": 8, "minute": 0}),
        (
            "reconcile_current_month_evidence",
            reconcile_current_month_evidence,
            {"hour": 8, "minute": 10},
        ),
        ("collect_market_condition", collect_market_condition, {"hour": 8, "minute": 15}),
        ("score_all_active", score_all_active, {"hour": 8, "minute": 20}),
        ("send_daily_summary", send_daily_summary, {"hour": 8, "minute": 30}),
        (
            "send_weekly_summary",
            send_weekly_summary,
            {"day_of_week": "mon", "hour": 8, "minute": 35},
        ),
        (
            "collect_kind_schedule_for_subscription_d_minus_one",
            collect_kind_schedule_for_subscription_d_minus_one,
            {"hour": 17, "minute": 10},
        ),
        (
            "collect_kind_listings_for_subscription_d_minus_one",
            collect_kind_listings_for_subscription_d_minus_one,
            {"hour": 17, "minute": 20},
        ),
        (
            "collect_public_dart_for_subscription_d_minus_one",
            collect_public_dart_for_subscription_d_minus_one,
            {"hour": 17, "minute": 30},
        ),
        (
            "reconcile_subscription_d_minus_one_evidence",
            reconcile_subscription_d_minus_one_evidence,
            {"hour": 18, "minute": 10},
        ),
        (
            "send_subscription_d_minus_one_emails",
            send_subscription_d_minus_one_emails,
            {"hour": 18, "minute": 30},
        ),
        ("collect_prices_due", collect_prices_due, {"hour": 16, "minute": 30}),
        ("evaluate_outcomes_due", evaluate_outcomes_due, {"hour": 17, "minute": 0}),
        ("send_monthly_backtest", send_monthly_backtest, {"day": 1, "hour": 9, "minute": 0}),
    ]


def run_scheduler() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    scheduler = BlockingScheduler(
        timezone=settings.timezone,
        job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 3600},
    )
    jobs = job_definitions()
    registered_names = {name for name, _fn, _trigger_kwargs in jobs}
    logger.info(
        "Email scheduler configured: fixed_recipients=%d d1_registered=%s smtp_delivery=%s",
        len(settings.email_recipients),
        "send_subscription_d_minus_one_emails" in registered_names,
        "enabled" if settings.email_configured else "waiting",
    )
    for name, fn, trigger_kwargs in jobs:
        scheduler.add_job(
            _job(name, fn),
            CronTrigger(timezone=settings.timezone, **trigger_kwargs),
            id=name,
            replace_existing=True,
        )
    logger.info("Scheduler started in %s with %d jobs", settings.timezone, len(jobs))
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scheduler shutting down")


def _job(name: str, fn: Callable):
    def wrapped():
        try:
            result = run_tracked_job(name, fn)
            logger.info("%s completed: %s", name, result)
            if isinstance(result, dict) and _failure_count(result):
                _send_operational_alert(
                    name,
                    f"부분 실패 {_failure_count(result)}건 / 결과: {result}",
                    status="partial",
                )
        except Exception as exc:
            logger.exception("%s failed", name)
            _send_operational_alert(name, f"{type(exc).__name__}: {exc}", status="failed")

    return wrapped


def _failure_count(result: dict) -> int:
    failed = result.get("failed", result.get("failures", 0))
    partial = result.get("partial", 0)
    return (failed if isinstance(failed, int) else 0) + (partial if isinstance(partial, int) else 0)


def _send_operational_alert(name: str, detail: str, *, status: str) -> None:
    settings = get_settings()
    if not settings.telegram_configured:
        return
    bucket = datetime.now(timezone.utc).strftime("%Y%m%d%H")
    try:
        from app.services.telegram import send_telegram_message

        send_telegram_message(
            f"[IPO 알리미 운영 경보]\n잡: {name}\n상태: {status}\n{mask_secrets(detail)[:1500]}",
            event_key=f"job-alert:{name}:{status}:{bucket}",
        )
    except Exception:
        logger.exception("Failed to send operational alert for %s", name)
