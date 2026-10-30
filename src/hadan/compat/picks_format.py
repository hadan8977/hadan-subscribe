"""Shared value parsing and labels for subscriber-facing picks output.

Quant GT values arrive as display strings ("$203.21", "+8.90%", "4.61/5",
"Buy +0.42", "Oct 29, 2026"). The email renderer and the Excel exporter both
need the same parsing, so it lives here. Every parser returns None for blank
or unparsable input instead of raising -- a weird value must never break a
subscriber notification.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from typing import Any, Dict, Iterable, List
from zoneinfo import ZoneInfo

from hadan.compat.diff import parse_analyst_signal

NY = ZoneInfo("America/New_York")
SHANGHAI = ZoneInfo("Asia/Shanghai")

BLANK_VALUES = {"", "—", "-", "–", "n/a", "N/A", "None"}

FIELD_LABELS = {
    "analyst_signal": "Analyst Consensus",
    "gt_score": "GT Score",
    "held_since": "Entry Date",
    "buy_or_entry_price": "Entry Price",
    "current_price": "Price",
    "return": "Return",
    "sector": "Sector",
    "company": "Company",
    "next_earnings": "Next Earnings",
    "market_cap": "Market Cap",
    "revenue_ttm": "Revenue (TTM)",
    "revenue_growth_yoy": "Revenue Growth",
    "pe_ttm": "P/E (TTM)",
    "rating": "Rating",
    "source_kind": "Source",
    "watch_reason": "Why Selected",
    "signal_price": "Signal Price",
}


def is_blank(value: Any) -> bool:
    return value is None or str(value).strip() in BLANK_VALUES


def field_label(name: str) -> str:
    return FIELD_LABELS.get(name) or str(name).replace("_", " ").title()


def section_title(section_key: str, section: Dict[str, Any] | None = None) -> str:
    if section_key == "monthly":
        return "Portfolio"
    if (section or {}).get("kind") == "watchlist":
        return "Weekly Watchlist"
    return "Weekly Picks"


def display_date(pick_date: Any) -> str:
    """ "Updated on Oct 2, 2026" -> "Oct 2, 2026"; "Updated October 1" -> "October 1"."""
    text = str(pick_date or "").strip()
    text = re.sub(
        r"^(last\s+)?updated\s*(on\s+)?", "", text, flags=re.IGNORECASE
    ).strip()
    return text or "Unknown"


def parse_money(value: Any) -> float | None:
    if is_blank(value):
        return None
    text = str(value).replace(",", "").strip()
    match = re.fullmatch(
        r"\$?\s*([-+]?\d+(?:\.\d+)?)\s*([KMBT])?", text, flags=re.IGNORECASE
    )
    if not match:
        return None
    number = float(match.group(1))
    scale = {"K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}.get(
        (match.group(2) or "").upper(), 1.0
    )
    return number * scale


def parse_pct(value: Any) -> float | None:
    """ "+8.90%" -> 0.089 (a fraction, ready for an Excel percent format)."""
    if is_blank(value):
        return None
    match = re.fullmatch(
        r"([-+]?\d+(?:\.\d+)?)\s*%", str(value).replace(",", "").strip()
    )
    return float(match.group(1)) / 100 if match else None


def parse_gt_score(value: Any) -> float | None:
    if is_blank(value):
        return None
    match = re.match(r"\s*(\d+(?:\.\d+)?)\s*(?:/\s*5)?\s*$", str(value))
    return float(match.group(1)) if match else None


def parse_signal(value: Any) -> tuple[str, float | None]:
    if is_blank(value):
        return "", None
    return parse_analyst_signal(value)


def parse_date(value: Any) -> date | None:
    if is_blank(value):
        return None
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%b %d, %Y", "%B %d, %Y", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def short_date(value: Any) -> str:
    """Drop the year: "Oct 29, 2026" -> "Oct 29". Unparsable text is returned unchanged."""
    text = str(value or "").strip()
    return re.sub(r",?\s+20\d\d$", "", text)


def format_gt(value: Any) -> str:
    score = parse_gt_score(value)
    return f"{score:.2f}" if score is not None else str(value or "")


def fetched_at_utc(data: Dict[str, Any]) -> datetime | None:
    """`fetched_at` is written as a naive server-local (UTC) ISO timestamp."""
    raw = str(data.get("fetched_at") or "").strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (
        dt.replace(tzinfo=timezone.utc)
        if dt.tzinfo is None
        else dt.astimezone(timezone.utc)
    )


def format_fetched(data: Dict[str, Any]) -> str:
    """ "Oct 6, 2026 · 08:30 ET / 20:30 Beijing"; most subscribers are in China."""
    dt = fetched_at_utc(data)
    if dt is None:
        return ""
    ny = dt.astimezone(NY)
    bj = dt.astimezone(SHANGHAI)
    return f"{ny:%b} {ny.day}, {ny:%Y} · {ny:%H:%M} ET / {bj:%H:%M} Beijing"


def since_signal(row: Dict[str, Any], as_of: datetime | None = None) -> float | None:
    """Watchlist move from this week's signal price to the current price (fraction).

    The watchlist is usually published at the weekend with a signal time of
    Monday's open; before that moment there is no move to report, so return
    None when `as_of` is earlier than the row's `signal_at`.
    """
    signal_at = row.get("signal_at")
    if as_of is not None and signal_at:
        try:
            if as_of < datetime.fromisoformat(str(signal_at).replace("Z", "+00:00")):
                return None
        except ValueError:
            pass
    start, now = (
        parse_money(row.get("signal_price")),
        parse_money(row.get("current_price")),
    )
    if not start or now is None:
        return None
    return now / start - 1


def rows_by_symbol(rows: Iterable[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for row in rows or []:
        key = row.get("symbol") or row.get("company")
        if key:
            out[str(key)] = row
    return out


def stock_count(n: int) -> str:
    return f"{n} stock" if n == 1 else f"{n} stocks"


def compact_symbols(symbols: List[str], limit: int = 3, sign: str = "") -> str:
    shown = [f"{sign}{s}" for s in symbols[:limit]]
    if len(symbols) > limit:
        shown.append(f"+{len(symbols) - limit} more")
    return " ".join(shown)
