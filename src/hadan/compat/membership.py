"""Membership billing-window arithmetic.

Billing windows are anchored to the 9th of every month at 00:00:00 in
America/New_York: ``[9th 00:00, next 9th 00:00)``. This is equivalent to the
plain-language framing used when this was specified: "9th 00:00 through the
8th 24:00".

Design notes (read before touching this file, it is the foundation the whole
membership system sits on):

- The anchor day (``ANCHOR_DAY = 9``) exists in every month of the year, so
  month arithmetic on anchor-aligned datetimes never has to clamp a day that
  does not exist in the target month (unlike, say, a day-31 anchor bumping
  into February). ``extend_expiry`` still clamps when it operates on a
  manually-edited expiry that is not anchor-aligned (see
  ``_add_calendar_months``); that path is defensive, not something anchor
  arithmetic itself relies on.
- Every public function requires timezone-aware input and internally
  normalizes to ``MEMBERSHIP_TZ``. Naive datetimes are rejected outright
  because silently assuming a timezone is exactly the kind of ambiguity that
  produces off-by-one-day member cutoffs.
- All comparisons in this module (``is_active``, and the ``<`` checks inside
  ``next_anchor``/``window_of``) are safe across DST transitions even though
  Python's aware-datetime comparison takes a "compare naive wall-clock
  tuples" shortcut when both operands share the identical ``tzinfo`` object
  (which they always do here, since ``zoneinfo.ZoneInfo`` caches by key and
  every datetime in this module is built with the same ``MEMBERSHIP_TZ``
  instance). That shortcut only produces a wrong chronological verdict when
  two datetimes share the exact same wall-clock hour/minute/second but
  differ in ``fold`` (the repeated hour during a "fall back" transition).
  America/New_York's DST transitions always happen between 01:00 and 03:00
  local time, and every datetime this module produces is pinned to
  00:00:00, so that ambiguous window is never hit. See
  ``tests/test_membership.py::DstTests`` for tests that pin this down.
"""

from __future__ import annotations

import calendar
from datetime import datetime
from zoneinfo import ZoneInfo

ANCHOR_DAY = 9
MEMBERSHIP_TZ = ZoneInfo("America/New_York")


def _require_aware(dt: datetime, *, param: str = "dt") -> datetime:
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        raise ValueError(f"{param} must be timezone-aware")
    return dt


def _to_ny(dt: datetime, *, param: str = "dt") -> datetime:
    return _require_aware(dt, param=param).astimezone(MEMBERSHIP_TZ)


def _require_months(months: int) -> int:
    if not isinstance(months, int) or isinstance(months, bool) or months < 1:
        raise ValueError(f"months must be a positive integer, got {months!r}")
    return months


def _add_months(year: int, month: int, delta: int) -> tuple[int, int]:
    """Add ``delta`` calendar months to (year, month). Returns (year, month), month in 1..12."""
    zero_based = (year * 12 + (month - 1)) + delta
    return zero_based // 12, zero_based % 12 + 1


def _add_calendar_months(dt: datetime, months: int) -> datetime:
    """Add ``months`` calendar months to ``dt``, clamping the day if the
    target month is shorter (e.g. Jan 31 + 1 month -> Feb 28).

    Anchor dates always fall on day 9, which exists in every month, so this
    clamp is never exercised by anchor-based arithmetic; it only matters for
    manually-set expiry dates (``set_expiry``) that are not anchor-aligned.
    """
    year, month = _add_months(dt.year, dt.month, months)
    last_day = calendar.monthrange(year, month)[1]
    day = min(dt.day, last_day)
    return dt.replace(year=year, month=month, day=day)


def anchor_at(year: int, month: int) -> datetime:
    """The billing anchor for ``year``-``month``: ``ANCHOR_DAY`` at 00:00:00 NY."""
    return datetime(year, month, ANCHOR_DAY, 0, 0, 0, tzinfo=MEMBERSHIP_TZ)


def next_anchor(dt: datetime) -> datetime:
    """The smallest anchor strictly greater than ``dt``.

    If ``dt`` itself lands exactly on an anchor, that instant belongs to the
    window that is *starting* right there, so the next anchor is one month
    later (not the same instant).
    """
    dt_ny = _to_ny(dt)
    this_month_anchor = anchor_at(dt_ny.year, dt_ny.month)
    if dt_ny < this_month_anchor:
        return this_month_anchor
    year, month = _add_months(dt_ny.year, dt_ny.month, 1)
    return anchor_at(year, month)


def window_of(dt: datetime) -> tuple[datetime, datetime]:
    """The half-open billing window ``[start, end)`` that contains ``dt``."""
    end = next_anchor(dt)
    start_year, start_month = _add_months(end.year, end.month, -1)
    start = anchor_at(start_year, start_month)
    return start, end


def expiry_for_new_member(joined_at: datetime, months: int) -> datetime:
    """Expiry for a brand-new member joining at ``joined_at`` and buying ``months`` months.

    ``= next_anchor(joined_at) + (months - 1) months``

    The first "billing month" always ends at the very next anchor, regardless
    of how much of the current partial window is left -- there is no
    proration. Joining one hour before an anchor and buying one month yields
    an expiry one hour later; that is the literal, intended rule (see
    ``tests/test_membership.py::ExpiryForNewMemberTests``), not a bug.
    """
    months = _require_months(months)
    first_anchor = next_anchor(joined_at)
    year, month = _add_months(first_anchor.year, first_anchor.month, months - 1)
    return anchor_at(year, month)


def extend_expiry(
    current_expiry: datetime | None, months: int, now: datetime
) -> datetime:
    """Expiry after adding ``months`` months of paid membership on top of ``current_expiry``.

    - Currently active (``current_expiry`` is in the future relative to
      ``now``): the new months stack on top of the existing expiry, counted
      from ``current_expiry`` itself -- not from today.
    - Expired, or no expiry on record at all (``current_expiry is None``):
      behaves exactly like a brand new membership starting ``now``.
    """
    months = _require_months(months)
    if current_expiry is not None and is_active(current_expiry, now):
        return _add_calendar_months(
            _to_ny(current_expiry, param="current_expiry"), months
        )
    return expiry_for_new_member(now, months)


def is_active(expires_at: datetime | None, now: datetime) -> bool:
    """A member is active if they have no expiry on record (legacy/never-expires) or haven't reached it yet."""
    if expires_at is None:
        return True
    return _to_ny(now, param="now") < _to_ny(expires_at, param="expires_at")
