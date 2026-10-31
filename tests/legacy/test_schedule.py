"""Durable scheduling: NYSE fixtures, DST, restart idempotency and source health."""

from datetime import date, datetime, timedelta, timezone
import time

import pytest

from hadan import schedule
from hadan.health import check


@pytest.mark.parametrize(
    "day,expected",
    [
        ("2026-05-26", True),
        ("2026-05-23", False),
        ("2026-05-25", False),
        ("2026-04-03", False),
        ("2026-07-03", False),
        ("2026-11-26", False),
        ("2026-12-25", False),
        ("2026-06-19", False),
        ("2025-01-09", False),
        ("2021-12-31", True),
        ("2023-01-02", False),
    ],
)
def test_nyse_calendar_known_closures(day, expected, monkeypatch):
    monkeypatch.delenv("MARKET_EXTRA_HOLIDAYS", raising=False)
    assert schedule.is_trading_day(date.fromisoformat(day)) is expected


def test_extra_closure_is_configurable(monkeypatch):
    monkeypatch.setenv("MARKET_EXTRA_HOLIDAYS", "2026-05-26")
    assert not schedule.is_trading_day(date(2026, 5, 26))


def test_month_end_has_no_extra_intraday_mail(monkeypatch):
    monkeypatch.delenv("HADAN_SCHEDULE", raising=False)
    plan = schedule.schedule_for_date(date(2026, 6, 30))
    assert (8, 30, "picks") in plan and (8, 40, "daily_admin_status") in plan
    assert not any(9 * 60 + 30 <= h * 60 + m < 16 * 60 for h, m, _ in plan)
    assert len(plan) == len(set(plan))


def test_trading_and_closed_day_scan_windows(monkeypatch):
    monkeypatch.delenv("HADAN_SCHEDULE", raising=False)
    trading = schedule.schedule_for_date(date(2026, 6, 30))
    closed = schedule.schedule_for_date(date(2026, 7, 3))
    assert (8, 30, "picks") in trading and (17, 0, "picks") in trading
    assert (8, 40, "daily_admin_status") in trading
    assert (8, 50, "weekly_digest") in trading
    assert not any(9 * 60 + 30 <= h * 60 + m < 16 * 60 for h, m, _ in trading)
    assert (8, 30, "picks") in closed and (17, 0, "picks") not in closed
    assert (8, 40, "daily_admin_status") in closed
    assert (8, 50, "weekly_digest") in closed


def test_custom_schedule_replaces_dynamic_plan(monkeypatch):
    monkeypatch.setenv("HADAN_SCHEDULE", "12:00:official_mail")
    assert schedule.schedule_for_date(date(2026, 6, 30)) == [(12, 0, "official_mail")]
    monkeypatch.setenv("HADAN_SCHEDULE", "25:00:picks")
    with pytest.raises(ValueError):
        schedule.schedule_for_date(date(2026, 6, 30))


@pytest.mark.parametrize(
    "instant",
    [
        "2026-03-06T13:30:00+00:00",  # 08:30 EST before spring change
        "2026-03-09T12:30:00+00:00",  # 08:30 EDT after spring change
        "2026-10-30T12:30:00+00:00",  # 08:30 EDT before autumn change
        "2026-11-02T13:30:00+00:00",  # 08:30 EST after autumn change
    ],
)
def test_dst_slot_uses_new_york_clock_and_survives_repeat_tick(
    source_service, monkeypatch, instant
):
    monkeypatch.setenv("QUANTGT_ENABLED", "1")
    monkeypatch.setenv("HADAN_SCHEDULE", "08:30:picks")
    due = datetime.fromisoformat(instant)
    source_service.db.execute(
        "INSERT INTO meta VALUES('scheduler_cursor',?)", (str(due.timestamp() - 1),)
    )
    schedule.tick(source_service, due)
    schedule.tick(source_service, due + timedelta(seconds=30))
    jobs = source_service.db.all("SELECT * FROM jobs WHERE kind='source:picks'")
    assert len(jobs) == 1
    assert "T08:30:00" in jobs[0]["dedupe"]


def test_restart_catchup_coalesces_each_kind_instead_of_blasting_old_slots(
    source_service, monkeypatch
):
    monkeypatch.setenv("QUANTGT_ENABLED", "1")
    monkeypatch.setenv("HADAN_SCHEDULE", "08:30:picks,09:00:picks,17:00:picks")
    now = datetime(2026, 10, 26, 22, tzinfo=timezone.utc)
    source_service.db.execute(
        "INSERT INTO meta VALUES('scheduler_cursor',?)",
        (str(now.timestamp() - 3 * 86400),),
    )
    schedule.tick(source_service, now)
    jobs = source_service.db.all("SELECT * FROM jobs WHERE kind='source:picks'")
    assert len(jobs) == 1
    assert "T17:00:00" in jobs[0]["dedupe"]


def test_missing_source_worker_is_unhealthy_when_sources_enabled(
    source_service, monkeypatch
):
    monkeypatch.setenv("QUANTGT_ENABLED", "1")
    for name in ("scheduler", "worker:main"):
        source_service.db.execute(
            "INSERT INTO heartbeats VALUES(?,?,'{}')", (name, time.time())
        )
    status = check(source_service)
    assert not status["healthy"], (
        "Main-worker liveness is insufficient if the source worker is dead"
    )
    assert any("source" in text.lower() for text in status["issues"])


def test_actual_expiry_notice_is_idempotent_and_does_not_restore_content_consent(
    source_service,
):
    from hadan.cli import grant

    now = datetime.now(timezone.utc)
    grant(
        source_service,
        "expired@example.test",
        "quantgt",
        (now - timedelta(minutes=1)).isoformat(),
    )
    schedule.tick(source_service, now)
    schedule.tick(source_service, now + timedelta(seconds=30))
    notices = source_service.db.all("SELECT * FROM jobs WHERE dedupe LIKE 'expired:%'")
    assert len(notices) == 1
    sub = source_service.db.one("SELECT * FROM subscriptions")
    assert sub["email_enabled"] == 0
    assert not source_service.entitled(sub["user_id"], "quantgt")


def test_payment_success_notice_is_once_per_invoice_and_preserves_opt_out(
    source_service, monkeypatch
):
    from hadan.billing import Billing
    from hadan.cli import grant

    grant(source_service, "paid@example.test", "quantgt")
    sub = source_service.db.one("SELECT * FROM subscriptions")
    source_service.db.execute(
        "UPDATE products SET paid=1,price_id='price-fixture' WHERE id='quantgt'"
    )
    remote = {
        "metadata": {"user_id": sub["user_id"], "product_id": "quantgt"},
        "items": {"data": [{"price": {"id": "price-fixture"}}]},
        "current_period_end": time.time() + 30 * 86400,
        "latest_invoice": {
            "id": "invoice-fixture",
            "status": "paid",
            "amount_paid": 1200,
            "currency": "usd",
        },
        "status": "active",
        "cancel_at_period_end": False,
    }
    billing = Billing(source_service)
    monkeypatch.setattr(billing, "api", lambda *_, **__: remote)
    billing.reconcile("subscription-fixture", "event-one")
    billing.reconcile("subscription-fixture", "event-two")
    assert (
        len(
            source_service.db.all(
                "SELECT * FROM jobs WHERE dedupe='payment-notice:invoice-fixture'"
            )
        )
        == 1
    )
    assert (
        source_service.db.one("SELECT email_enabled FROM subscriptions")[
            "email_enabled"
        ]
        == 0
    )
