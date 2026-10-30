from __future__ import annotations

import json
import re
from typing import Any, Dict, List


DYNAMIC_NOISE_FIELDS = {
    "return",
    "current_price",
    "chart_return",
    "market_cap",
    "pe_ttm",
    "buy_or_entry_price",
    "revenue_ttm",
    "revenue_growth_yoy",
    "next_earnings",
    "momentum",
    "relative_strength",
    # Scrape-metadata flags: they flip when Quant GT omits/restores a field,
    # which is data availability, not a pick change subscribers care about.
    "analyst_signal_unavailable",
    "next_earnings_unavailable",
    "gt_score_source",
    # Which page layout a row was parsed from (table vs watchlist card); flipped
    # on every weekly row on 2026-07-02 and produced a meaningless alert.
    "source_kind",
    # Watchlist context added 2026-10: this week's signal price/date from the
    # weekly API and the one-line reason from the Weekly Digest (best-effort,
    # may be missing on a given run). Display only, never a pick change.
    "signal_price",
    "signal_date",
    "signal_at",
    "watch_reason",
}

# Quant GT's analyst score is very jumpy: in 2026-06..09 snapshots the median
# day-over-day move was 0.14, p90 0.41, and >half of moves reverted within ~2
# trading days (e.g. HPE +0.38 -> +0.56 -> +0.31). The old 0.30 / label-change
# rules fired ~3.5 signal-only emails a week; 0.60 plus Strong Sell entry/exit
# replays to ~1 a week.
ANALYST_SIGNAL_MAJOR_DELTA = 0.60


def row_key(row: Dict[str, Any]) -> str:
    return row.get("symbol") or row.get("company") or json.dumps(row, sort_keys=True)


def parse_analyst_signal(value: Any) -> tuple[str, float | None]:
    """Parse strings like "Buy +0.27" into (label, score)."""
    text = str(value or "").strip()
    if not text:
        return "", None
    match = re.search(r"([+-]?\d+(?:\.\d+)?)\s*$", text)
    score = float(match.group(1)) if match else None
    label = text[: match.start()].strip() if match else text
    return re.sub(r"\s+", " ", label), score


def is_major_analyst_signal_change(old_value: Any, new_value: Any) -> bool:
    old_label, old_score = parse_analyst_signal(old_value)
    new_label, new_score = parse_analyst_signal(new_value)
    if str(old_value or "") == str(new_value or ""):
        return False
    if (old_label == "Strong Sell") != (new_label == "Strong Sell"):
        return True
    if old_score is None or new_score is None:
        return False
    return abs(new_score - old_score) >= ANALYST_SIGNAL_MAJOR_DELTA


def diff_rows(
    old_rows: List[Dict[str, Any]], new_rows: List[Dict[str, Any]]
) -> Dict[str, Any]:
    old_map = {row_key(row): row for row in old_rows}
    new_map = {row_key(row): row for row in new_rows}
    added = sorted(set(new_map) - set(old_map))
    removed = sorted(set(old_map) - set(new_map))
    changed = []
    for key in sorted(set(old_map) & set(new_map)):
        fields = {}
        keys = sorted(set(old_map[key]) | set(new_map[key]))
        for field in keys:
            if field in {"detail_error"} or field in DYNAMIC_NOISE_FIELDS:
                continue
            old_value = old_map[key].get(field, "")
            new_value = new_map[key].get(field, "")
            if field == "rating" and str(new_value or "").strip() == "":
                # Treat a newly-missing rating as scraper/source-field degradation, not a user-facing pick change.
                # Quant GT can still expose the actionable label in analyst_signal; an empty New cell is misleading.
                continue
            if field == "analyst_signal":
                if is_major_analyst_signal_change(old_value, new_value):
                    fields[field] = {"old": old_value, "new": new_value}
                continue
            if str(old_value) != str(new_value):
                fields[field] = {"old": old_value, "new": new_value}
        if fields:
            changed.append({"symbol": key, "fields": fields})
    return {"added": added, "removed": removed, "changed": changed}


def compare(old: Dict[str, Any], new: Dict[str, Any]) -> Dict[str, Any]:
    result = {"changed": False, "monthly": {}, "weekly": {}}
    for section in ["monthly", "weekly"]:
        sec = {}
        old_date = old.get(section, {}).get("pick_date")
        new_date = new.get(section, {}).get("pick_date")
        if (
            old_date != new_date
            and old_date not in (None, "", "Unknown")
            and new_date not in (None, "", "Unknown")
        ):
            sec["date"] = {"old": old_date, "new": new_date}
        row_diff = diff_rows(
            old.get(section, {}).get("rows", []), new.get(section, {}).get("rows", [])
        )
        sec.update(row_diff)
        sec_changed = bool(
            sec.get("date")
            or sec.get("added")
            or sec.get("removed")
            or sec.get("changed")
        )
        sec["changed_flag"] = sec_changed
        result[section] = sec
        result["changed"] = result["changed"] or sec_changed
    return result
