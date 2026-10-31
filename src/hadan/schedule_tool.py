"""Agent-facing view of the configured source scan schedule.

The scheduler is defined in America/New_York local time. Returned times use
New York time with the correct DST offset. A scan is retained while any of its
three checks is still due, even when the scan start itself is already in the
past; this prevents a client that refreshes the schedule at 08:35 from losing
the 08:40/08:50/09:00 checks of the 08:30 scan.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .schedule import schedule_for_date

CONTENT_KINDS = {"official_mail", "picks", "weekly_digest"}
CHECK_DELAY_SECONDS = 600
CHECK_INTERVAL_SECONDS = 600
CHECKS_PER_SCAN = 3
NY = ZoneInfo("America/New_York")


def _checks(kind: str, at: datetime, now: datetime) -> list[dict]:
    checks = []
    for index in range(CHECKS_PER_SCAN):
        check_at = at + timedelta(
            seconds=CHECK_DELAY_SECONDS + index * CHECK_INTERVAL_SECONDS
        )
        # A check exactly at generated_at is due, not lost. Clients must
        # deduplicate by check_id if they fetch the schedule repeatedly.
        if check_at < now:
            continue
        checks.append(
            {
                "check_id": f"{kind}:{at:%Y%m%dT%H%M}:{index + 1}",
                "check_at_new_york": check_at.isoformat(),
                "state": "due" if check_at <= now else "upcoming",
            }
        )
    return checks


def upcoming_scans(limit: int = 12, *, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    today = now.astimezone(NY).date()
    scans = []
    # Start one day early so a late-night custom scan can contribute checks
    # that fall after midnight. Ten days cover holidays and long weekends.
    for offset in range(-1, 10):
        day = today + timedelta(days=offset)
        for hour, minute, kind in schedule_for_date(day):
            if kind not in CONTENT_KINDS:
                continue
            at = datetime(day.year, day.month, day.day, hour, minute, tzinfo=NY)
            checks = _checks(kind, at, now)
            if not checks:
                continue
            scans.append(
                {
                    "kind": kind,
                    "scan_at_new_york": at.isoformat(),
                    "poll_after_new_york": checks[0]["check_at_new_york"],
                    "checks_new_york": [
                        check["check_at_new_york"] for check in checks
                    ],
                    "checks": checks,
                    "remaining_checks": len(checks),
                    "check_window_start_new_york": checks[0]["check_at_new_york"],
                    "check_window_end_new_york": checks[-1]["check_at_new_york"],
                }
            )
    scans.sort(key=lambda item: item["poll_after_new_york"])
    scans = scans[: max(1, min(int(limit), 50))]
    return {
        "generated_at_new_york": now.astimezone(NY).isoformat(),
        "timezone": "America/New_York",
        "timezone_note": "All scan and check times are New York local time, including the DST offset.",
        "polling_policy": {
            "mode": "scan_windows_only",
            "checks_per_scan": CHECKS_PER_SCAN,
            "first_check_delay_seconds": CHECK_DELAY_SECONDS,
            "check_interval_within_window_seconds": CHECK_INTERVAL_SECONDS,
            "max_checks_per_scan": CHECKS_PER_SCAN,
            "do_not_poll_between_scans": True,
            "deduplicate_by": "checks[].check_id",
            "run_at_or_after_check": True,
        },
        "scans": scans,
        "note": (
            "Run list_events at a returned check time or shortly after it, at most once "
            "per check_id. Re-fetching this schedule does not re-run checks; persist the "
            "check_id you already executed. Webhook delivery and email are independent "
            "and may arrive outside these scheduled checks. Do not run a 24/7 every-10-minute "
            "loop, and do not poll between the returned checks. If the scheduler can only "
            "repeat at a fixed interval, gate each run by the returned check times and skip "
            "runs outside them. No pending events means no notification."
        ),
    }
