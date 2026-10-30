"""Agent-facing view of the source scan schedule.

The scheduler is defined in America/New_York local time. Converting it here
keeps DST, weekends and holiday schedules correct for agents that poll from a
different timezone. Agents should check only after the listed scans, not run a
24/7 interval loop.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .schedule import schedule_for_date

CONTENT_KINDS = {"official_mail", "picks", "weekly_digest"}
CHECK_DELAY_SECONDS = 600
CHECK_INTERVAL_SECONDS = 600
CHECKS_PER_SCAN = 3


def _checks(at: datetime, cn: ZoneInfo) -> dict:
    checks = [
        at + timedelta(seconds=CHECK_DELAY_SECONDS + index * CHECK_INTERVAL_SECONDS)
        for index in range(CHECKS_PER_SCAN)
    ]
    return {
        "checks_utc": [item.astimezone(timezone.utc).isoformat() for item in checks],
        "checks_asia_shanghai": [item.astimezone(cn).isoformat() for item in checks],
        "check_window_start_utc": checks[0].astimezone(timezone.utc).isoformat(),
        "check_window_end_utc": checks[-1].astimezone(timezone.utc).isoformat(),
        "check_window_start_asia_shanghai": checks[0].astimezone(cn).isoformat(),
        "check_window_end_asia_shanghai": checks[-1].astimezone(cn).isoformat(),
    }


def upcoming_scans(limit: int = 12) -> dict:
    now = datetime.now(timezone.utc)
    ny = ZoneInfo("America/New_York")
    cn = ZoneInfo("Asia/Shanghai")
    today = now.astimezone(ny).date()
    scans = []
    for offset in range(0, 10):
        day = today + timedelta(days=offset)
        for hour, minute, kind in schedule_for_date(day):
            if kind not in CONTENT_KINDS:
                continue
            at = datetime(day.year, day.month, day.day, hour, minute, tzinfo=ny)
            if at <= now:
                continue
            timing = _checks(at, cn)
            scans.append(
                {
                    "kind": kind,
                    "scan_at_utc": at.astimezone(timezone.utc).isoformat(),
                    "scan_at_asia_shanghai": at.astimezone(cn).isoformat(),
                    "poll_after_utc": timing["checks_utc"][0],
                    "poll_after_asia_shanghai": timing["checks_asia_shanghai"][0],
                    **timing,
                }
            )
        if len(scans) >= limit:
            break
    scans = sorted(scans, key=lambda item: item["scan_at_utc"])[:limit]
    return {
        "generated_at_utc": now.isoformat(),
        "timezone": "America/New_York",
        "timezone_note": "Scan times follow the NYSE calendar and are converted to UTC and Asia/Shanghai.",
        "polling_policy": {
            "mode": "scan_windows_only",
            "checks_per_scan": CHECKS_PER_SCAN,
            "first_check_delay_seconds": CHECK_DELAY_SECONDS,
            "repeat_interval_seconds": CHECK_INTERVAL_SECONDS,
            "max_checks_per_scan": CHECKS_PER_SCAN,
            "do_not_poll_between_scans": True,
        },
        "scans": scans,
        "note": (
            "Run list_events only at the listed checks for each scan, at most three checks. "
            "Each check is 10 minutes after the scan, then 10 and 20 minutes later. "
            "Do not run a 24/7 every-10-minute loop and do not poll between scans; "
            "there is nothing new to fetch outside the scan windows. "
            "If the scheduler can only repeat at a fixed interval, gate each run by "
            "check_window_start_utc and check_window_end_utc and skip runs outside those windows. "
            "If it cannot gate by time, use MCP Events or email instead of a continuous loop. "
            "No pending events means no notification."
        ),
    }
