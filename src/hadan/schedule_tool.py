"""Agent-facing view of the source scan schedule.

The scheduler is defined in America/New_York local time. All returned times
use New York time with the correct DST offset. Agents should check only after
the listed scans, not run a 24/7 interval loop.
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


def _checks(at: datetime) -> dict:
    checks = [
        at + timedelta(seconds=CHECK_DELAY_SECONDS + index * CHECK_INTERVAL_SECONDS)
        for index in range(CHECKS_PER_SCAN)
    ]
    return {
        "checks_new_york": [item.isoformat() for item in checks],
        "check_window_start_new_york": checks[0].isoformat(),
        "check_window_end_new_york": checks[-1].isoformat(),
    }


def upcoming_scans(limit: int = 12) -> dict:
    now = datetime.now(timezone.utc)
    today = now.astimezone(NY).date()
    scans = []
    for offset in range(0, 10):
        day = today + timedelta(days=offset)
        for hour, minute, kind in schedule_for_date(day):
            if kind not in CONTENT_KINDS:
                continue
            at = datetime(day.year, day.month, day.day, hour, minute, tzinfo=NY)
            if at <= now:
                continue
            timing = _checks(at)
            scans.append(
                {
                    "kind": kind,
                    "scan_at_new_york": at.isoformat(),
                    "poll_after_new_york": timing["checks_new_york"][0],
                    **timing,
                }
            )
        if len(scans) >= limit:
            break
    scans = scans[:limit]
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
        },
        "scans": scans,
        "note": (
            "Run list_events only at the listed checks for each scan, at most three checks. "
            "Each check is 10 minutes after the scan, then 10 and 20 minutes later. "
            "On trading days there are no scheduled source scans between 09:30 and 16:00 New York time. "
            "Do not run a 24/7 every-10-minute loop and do not poll between scans; "
            "there is nothing new to fetch outside the scan windows. "
            "If the scheduler can only repeat at a fixed interval, gate each run by "
            "check_window_start_new_york and check_window_end_new_york and skip runs outside those windows. "
            "If it cannot gate by time, use MCP Events or email instead of a continuous loop. "
            "No pending events means no notification."
        ),
    }
