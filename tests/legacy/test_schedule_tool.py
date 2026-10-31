"""Agent schedule windows must not disappear after the scan start passes."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from hadan import schedule_tool

NY = ZoneInfo("America/New_York")


def fixed_schedule(day):
    if day == date(2026, 10, 30):
        return [(8, 30, "picks"), (23, 50, "official_mail")]
    return []


@pytest.fixture(autouse=True)
def only_fixed_scans(monkeypatch):
    monkeypatch.setattr(schedule_tool, "schedule_for_date", fixed_schedule)


@pytest.mark.parametrize(
    "hour,minute,expected",
    [
        (8, 29, ["08:40", "08:50", "09:00"]),
        (8, 35, ["08:40", "08:50", "09:00"]),
        (8, 45, ["08:50", "09:00"]),
        (9, 0, ["09:00"]),
        (9, 1, []),
    ],
)
def test_refreshing_after_scan_start_keeps_future_checks(hour, minute, expected):
    now = datetime(2026, 10, 30, hour, minute, tzinfo=NY)
    result = schedule_tool.upcoming_scans(now=now)
    picks = [scan for scan in result["scans"] if scan["kind"] == "picks"]
    if not expected:
        assert not picks
        return
    scan = picks[0]
    assert scan["remaining_checks"] == len(expected)
    assert [value[11:16] for value in scan["checks_new_york"]] == expected
    assert scan["poll_after_new_york"] == scan["checks_new_york"][0]
    if hour == 9 and minute == 0:
        assert scan["checks"][0]["state"] == "due"
        assert scan["checks"][0]["check_id"] == "picks:20261030T0830:3"


def test_late_night_scan_keeps_checks_after_midnight():
    now = datetime(2026, 10, 30, 23, 55, tzinfo=NY)
    result = schedule_tool.upcoming_scans(now=now)
    mail = [scan for scan in result["scans"] if scan["kind"] == "official_mail"]
    assert mail
    assert [value[11:16] for value in mail[0]["checks_new_york"]] == [
        "00:00",
        "00:10",
        "00:20",
    ]
    assert mail[0]["checks_new_york"][0].startswith("2026-10-31")


def test_repeated_schedule_reads_return_stable_check_ids():
    now = datetime(2026, 10, 30, 8, 35, tzinfo=NY)
    first = schedule_tool.upcoming_scans(now=now)
    second = schedule_tool.upcoming_scans(now=now)
    assert first["scans"] == second["scans"]
    assert first["polling_policy"]["deduplicate_by"] == "checks[].check_id"
    assert "nothing new" not in first["note"].lower()
    assert "webhook delivery and email are independent" in first["note"].lower()
