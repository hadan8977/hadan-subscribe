from __future__ import annotations

import json
import os
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .db import enqueue

NY = ZoneInfo("America/New_York")
TRADING = [
    (8, 20, "official_mail"),
    (8, 30, "picks"),
    (8, 40, "daily_admin_status"),
    (8, 45, "health_site"),
    (8, 50, "weekly_digest"),
    (9, 0, "picks"),
    (17, 0, "picks"),
    (17, 15, "health_site"),
    (17, 30, "official_mail"),
]
HOLIDAY = [
    (8, 20, "official_mail"),
    (8, 30, "picks"),
    (8, 40, "daily_admin_status"),
    (8, 45, "health_site"),
    (8, 50, "weekly_digest"),
]


def nth(year, month, weekday, n):
    first = date(year, month, 1)
    return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))


def observed(d):
    return d + timedelta(days=1 if d.weekday() == 6 else -1 if d.weekday() == 5 else 0)


def easter(year):
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    return date(year, (h + l - 7 * m + 114) // 31, (h + l - 7 * m + 114) % 31 + 1)


def is_trading_day(day):
    y = day.year
    newyear = date(y, 1, 1)
    holidays = {
        newyear if newyear.weekday() != 6 else newyear + timedelta(days=1),
        nth(y, 1, 0, 3),
        nth(y, 2, 0, 3),
        easter(y) - timedelta(days=2),
        nth(y, 9, 0, 1),
        nth(y, 11, 3, 4),
        observed(date(y, 7, 4)),
        observed(date(y, 12, 25)),
    }
    last = date(y, 5, 31)
    holidays.add(last - timedelta(days=last.weekday()))
    if y >= 2022:
        holidays.add(observed(date(y, 6, 19)))
    special = {"2012-10-29", "2012-10-30", "2018-12-05", "2025-01-09"}
    extra = {
        x.strip()
        for x in os.getenv("MARKET_EXTRA_HOLIDAYS", "").split(",")
        if x.strip()
    }
    return (
        day.weekday() < 5
        and day not in holidays
        and day.isoformat() not in special | extra
    )


def schedule_for_date(day):
    custom = os.getenv("HADAN_SCHEDULE", "")
    if custom:
        values = []
        for entry in custom.split(","):
            hour, minute, kind = entry.strip().split(":")
            if (
                not 0 <= int(hour) < 24
                or not 0 <= int(minute) < 60
                or kind not in {x[2] for x in TRADING} | {"health"}
            ):
                raise ValueError("Invalid HADAN_SCHEDULE")
            values.append((int(hour), int(minute), kind))
        return sorted(set(values))
    values = list(TRADING if is_trading_day(day) else HOLIDAY)
    priority = {
        "picks": 0,
        "health_site": 1,
        "health": 2,
        "daily_admin_status": 3,
        "weekly_digest": 4,
        "official_mail": 5,
    }
    return sorted(set(values), key=lambda x: (x[0], x[1], priority[x[2]]))


def tick(service, now=None):
    now = now or datetime.now(timezone.utc)
    stamp = now.timestamp()
    local = now.astimezone(NY)
    source_enabled = os.getenv("QUANTGT_ENABLED", "0") == "1"
    mail_enabled = os.getenv("OFFICIAL_MAIL_ENABLED", "0") == "1"
    with service.db.transaction() as db:
        poll_seconds = int(os.getenv("OFFICIAL_MAIL_POLL_SECONDS", "0"))
        if mail_enabled and poll_seconds:
            if not 60 <= poll_seconds <= 86400:
                raise ValueError("OFFICIAL_MAIL_POLL_SECONDS must be 0 or between 60 and 86400")
            enqueue(db, "source:official_mail", {}, "inbox-poll:" + str(int(stamp // poll_seconds)), queue="source")
        prior = db.execute(
            "SELECT value FROM meta WHERE key='scheduler_cursor'"
        ).fetchone()
        start = max(float(prior[0]) if prior else stamp - 60, stamp - 86400)
        latest = {}
        for day in (local.date() - timedelta(days=1), local.date()):
            for h, m, kind in schedule_for_date(day):
                due = datetime(day.year, day.month, day.day, h, m, tzinfo=NY)
                if start < due.timestamp() <= stamp:
                    latest[kind] = due
        for kind, due in latest.items():
            if kind == "official_mail" and not mail_enabled:
                continue
            if kind == "health" and not (source_enabled or mail_enabled):
                continue
            if (
                kind not in ("official_mail", "daily_admin_status", "health")
                and not source_enabled
            ):
                continue
            job_kind = (
                "admin_summary" if kind == "daily_admin_status" else "source:" + kind
            )
            enqueue(
                db,
                job_kind,
                {},
                "schedule:" + kind + ":" + due.isoformat(),
                queue="main" if kind == "daily_admin_status" else "source",
            )
        db.execute(
            "INSERT INTO meta VALUES('scheduler_cursor',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(stamp),),
        )
        db.execute(
            "INSERT INTO heartbeats VALUES('scheduler',?,'{}') ON CONFLICT(name) DO UPDATE SET updated_at=excluded.updated_at",
            (stamp,),
        )
        # Expiry notices and provider truth reconciliation do not re-enable content preferences.
        daykey = local.date().isoformat()
        if service.settings.ops_ai:
            utc_day = now.astimezone(timezone.utc).date().isoformat()
            for incident in db.execute(
                "SELECT id FROM incidents WHERE state='open' AND kind NOT IN ('diagnose','propose_parser') ORDER BY updated_at LIMIT 100"
            ).fetchall():
                enqueue(
                    db,
                    "diagnose",
                    {"incident_id": incident["id"]},
                    "diagnose:" + incident["id"] + ":" + utc_day,
                    queue="ops",
                )
        for sub in db.execute(
            "SELECT * FROM subscriptions WHERE expires_at>? AND expires_at<?",
            (stamp, stamp + 7 * 86400),
        ).fetchall():
            service.account_notice(
                db,
                sub["user_id"],
                sub["product_id"],
                "expiring",
                "Quant · 订阅即将到期",
                "一项订阅将在 7 天内到期。请在订阅页面检查权益及续费设置。",
                "expiry:" + sub["id"] + ":" + str(sub["expires_at"]),
            )
        for sub in db.execute(
            "SELECT * FROM subscriptions WHERE status='active' AND expires_at<=? AND expires_at>?",
            (stamp, stamp - 86400),
        ).fetchall():
            service.account_notice(
                db,
                sub["user_id"],
                sub["product_id"],
                "expired",
                "Quant · 订阅已到期",
                "订阅已到期，私有内容访问和内容交付已停止。续费不会自动重新开启你关闭过的邮件或 Agent 接收。",
                "expired:" + sub["id"] + ":" + str(sub["expires_at"]),
            )
        if service.settings.stripe_key:
            for sub in db.execute(
                "SELECT billing_id FROM subscriptions WHERE billing_id IS NOT NULL"
            ).fetchall():
                enqueue(
                    db,
                    "billing_reconcile",
                    {"billing_id": sub["billing_id"], "event_id": "periodic:" + daykey},
                    "reconcile:" + sub["billing_id"] + ":" + daykey,
                )
        # Reconcile in-flight money even when accepting NEW checkout is disabled.
        if ((service.settings.xorpay_aid and service.settings.xorpay_secret) or
            (service.settings.xiaodou_app_id and service.settings.xiaodou_private_key_file)):
            for order in db.execute(
                "SELECT id FROM purchase_orders WHERE provider IN ('xorpay','xiaodou') AND status IN ('creating','pending','verifying','expired') AND created_at<? AND created_at>?",
                (stamp - 120, stamp - 7 * 86400),
            ).fetchall():
                enqueue(db, "purchase_reconcile", {"order_id": order["id"]},
                        "purchase-reconcile:" + order["id"] + ":" + str(int(stamp // 3600)))
        db.execute("DELETE FROM sessions WHERE expires_at<?", (stamp,))
        db.execute("DELETE FROM otp WHERE expires_at<?", (stamp - 86400,))
        db.execute("DELETE FROM pairings WHERE expires_at<?", (stamp - 86400,))
        db.execute("DELETE FROM rate_limits WHERE start<?", (stamp - 86400,))
        # Payloads may contain encrypted OTPs; remove completed ephemeral work promptly.
        db.execute(
            "DELETE FROM jobs WHERE kind='login_mail' AND state='complete' AND created_at<?",
            (stamp - 3600,),
        )
