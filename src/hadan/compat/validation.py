from __future__ import annotations

import re
from typing import Any, Dict


ANALYST_SIGNAL_RE = re.compile(
    r"^(?:Strong Buy|Strong Sell|Buy|Sell|Neutral|Hold) [+-]?\d+(?:\.\d+)?$"
)


def invalid_analyst_signal_rows(rows):
    return [
        str(row.get("symbol") or row.get("company") or "?")
        for row in rows
        if row.get("analyst_signal")
        and not ANALYST_SIGNAL_RE.fullmatch(str(row.get("analyst_signal")).strip())
    ]


def validate_member_picks_data(data: Dict[str, Any]) -> None:
    """Reject unauthenticated/demo or partial Quant GT captures before state writes."""
    monthly = data.get("monthly") or {}
    weekly = data.get("weekly") or {}
    weekly_rows = weekly.get("rows") or []
    if not weekly_rows:
        raise RuntimeError(
            "logged-in weekly picks validation failed: no weekly rows captured"
        )
    weekly_date = str(weekly.get("pick_date") or "")
    weekly_kind = str(weekly.get("kind") or "weekly_picks")
    weekly_symbols = [str(row.get("symbol") or "").strip().upper() for row in weekly_rows]
    if any(not symbol for symbol in weekly_symbols):
        raise RuntimeError(
            "logged-in weekly picks validation failed: blank symbol in loaded rows"
        )
    duplicate_weekly = sorted(
        {symbol for symbol in weekly_symbols if weekly_symbols.count(symbol) > 1}
    )
    if duplicate_weekly:
        raise RuntimeError(
            "logged-in weekly picks validation failed: duplicate symbols: "
            + ", ".join(duplicate_weekly[:5])
        )
    generic_detail_rows = [
        row
        for row in weekly_rows
        if "This company designs, develops, and markets a range of consumer and enterprise technology products and services worldwide"
        in str(row.get("relative_strength") or "")
    ]
    current_prices = {
        str(row.get("current_price") or "")
        for row in weekly_rows
        if row.get("current_price")
    }
    demo_symbol_set = {"SNDK", "LITE", "AAOI", "FORM", "VIAV", "ENPH"}
    if weekly_date == "05/15/26" and demo_symbol_set.issubset(set(weekly_symbols)):
        raise RuntimeError(
            "rejected unauthenticated/demo Weekly Picks signature: 05/15/26"
        )
    if len(set(weekly_symbols)) < 8:
        raise RuntimeError(
            f"logged-in weekly picks validation failed: expected near-complete Weekly Top 10, captured {len(set(weekly_symbols))} unique rows"
        )
    if not (monthly.get("rows") or []):
        raise RuntimeError(
            "logged-in monthly picks validation failed: no monthly rows captured"
        )

    monthly_rows = monthly.get("rows") or []
    monthly_symbols = [str(row.get("symbol") or "").strip().upper() for row in monthly_rows]
    if any(not symbol for symbol in monthly_symbols):
        raise RuntimeError(
            "logged-in monthly picks validation failed: blank symbol in loaded rows"
        )
    duplicate_monthly = sorted(
        {symbol for symbol in monthly_symbols if monthly_symbols.count(symbol) > 1}
    )
    if duplicate_monthly:
        raise RuntimeError(
            "logged-in monthly picks validation failed: duplicate symbols: "
            + ", ".join(duplicate_monthly[:5])
        )
    # Quant GT can omit buy/entry price for an otherwise complete monthly row.
    # Keep hard gates on the independent detail fields that prove real loading.
    required_monthly_fields = [
        "symbol",
        "company",
        "current_price",
        "return",
        "sector",
        "gt_score",
        "next_earnings",
        "analyst_signal",
    ]
    bad_monthly = []
    for row in monthly_rows:
        missing = [
            field for field in required_monthly_fields if row.get(field) in (None, "")
        ]
        if missing:
            bad_monthly.append(
                f"{row.get('symbol') or row.get('company') or '?'} missing {','.join(missing)}"
            )
    if bad_monthly:
        raise RuntimeError(
            "logged-in monthly picks validation failed: incomplete loaded rows: "
            + "; ".join(bad_monthly[:5])
        )
    malformed_monthly_signals = invalid_analyst_signal_rows(monthly_rows)
    if malformed_monthly_signals:
        raise RuntimeError(
            "logged-in monthly picks validation failed: malformed analyst signal for: "
            + ", ".join(malformed_monthly_signals[:5])
        )

    if weekly_kind == "watchlist":
        required_watchlist_fields = [
            "symbol",
            "company",
            "current_price",
            "sector",
            "gt_score",
        ]
        optional_source_fields = {
            "next_earnings": "next_earnings_unavailable",
            "analyst_signal": "analyst_signal_unavailable",
        }
        bad_watchlist = []
        for row in weekly_rows:
            missing = [
                field
                for field in required_watchlist_fields
                if row.get(field) in (None, "")
            ]
            missing.extend(
                field
                for field, unavailable_flag in optional_source_fields.items()
                if row.get(field) in (None, "")
                and row.get(unavailable_flag) is not True
            )
            if row.get("gt_score_source") not in {
                "weekly_api_score",
                "dialog_components",
            }:
                missing.append("gt_score_source")
            if missing:
                bad_watchlist.append(
                    f"{row.get('symbol') or row.get('company') or '?'} missing {','.join(missing)}"
                )
        if bad_watchlist:
            raise RuntimeError(
                "logged-in watchlist validation failed: incomplete detail rows: "
                + "; ".join(bad_watchlist[:5])
            )
        malformed_watchlist_signals = invalid_analyst_signal_rows(weekly_rows)
        if malformed_watchlist_signals:
            raise RuntimeError(
                "logged-in watchlist validation failed: malformed analyst signal for: "
                + ", ".join(malformed_watchlist_signals[:5])
            )
        return
    if len(generic_detail_rows) >= max(3, len(weekly_rows) // 2):
        raise RuntimeError(
            "rejected unauthenticated/demo Weekly Picks signature: generic placeholder details"
        )
    if (
        len(weekly_rows) >= 5
        and len(current_prices) == 1
        and "$184.62" in current_prices
    ):
        raise RuntimeError(
            "rejected unauthenticated/demo Weekly Picks signature: repeated fake current price"
        )

    # Newer Quant GT weekly detail panels may omit buy/entry price for some
    # picks, while still rendering the rest of the authenticated detail data.
    # Keep the quality gate focused on fields that indicate real detail loading.
    required_weekly_detail_fields = [
        "symbol",
        "company",
        "current_price",
        "sector",
        "gt_score",
        "next_earnings",
        "analyst_signal",
    ]
    complete_detail_rows = [
        row
        for row in weekly_rows
        if all(
            row.get(field) not in (None, "") for field in required_weekly_detail_fields
        )
    ]
    detailish_rows = [
        row
        for row in weekly_rows
        if any(
            row.get(field) not in (None, "") for field in required_weekly_detail_fields
        )
    ]
    if detailish_rows and len(complete_detail_rows) != len(weekly_rows):
        bad_weekly = []
        for row in weekly_rows:
            missing = [
                field
                for field in required_weekly_detail_fields
                if row.get(field) in (None, "")
            ]
            if missing:
                bad_weekly.append(
                    f"{row.get('symbol') or row.get('company') or '?'} missing {','.join(missing)}"
                )
        raise RuntimeError(
            "logged-in weekly picks validation failed: incomplete detail rows: "
            + "; ".join(bad_weekly[:5])
        )
    malformed_weekly_signals = invalid_analyst_signal_rows(weekly_rows)
    if malformed_weekly_signals:
        raise RuntimeError(
            "logged-in weekly picks validation failed: malformed analyst signal for: "
            + ", ".join(malformed_weekly_signals[:5])
        )
