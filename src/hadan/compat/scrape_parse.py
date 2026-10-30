from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List


MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec"
KNOWN_DETAIL_LABELS = [
    "Buy price:",
    "Entry price:",
    "P/E (TTM)",
    "Market Cap",
    "Revenue (TTM)",
    "Revenue Growth (YoY)",
    "Next Earnings",
    "Analyst Signal",
    "Analyst Consensus",
    "Momentum",
    "Relative Strength",
]


def clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


ANALYST_SIGNAL_RE = re.compile(
    r"\b(Strong\s+Buy|Strong\s+Sell|Buy|Sell|Neutral|Hold)\s+([+-]?\d+(?:\.\d+)?)\b",
    re.I,
)


def parse_analyst_signal_text(value: Any) -> str:
    """Return only the source's rating label and numeric signal value."""
    match = ANALYST_SIGNAL_RE.search(clean_text(value))
    if not match:
        return ""
    label = " ".join(part.capitalize() for part in match.group(1).split())
    return f"{label} {match.group(2)}"


def normalize_header(value: Any) -> str:
    text = clean_text(value).lower()
    return re.sub(r"[^a-z0-9]+", "_", text).strip("_")


HEADER_ALIASES = {
    "company": "company",
    "name": "company",
    "symbol": "symbol",
    "ticker": "symbol",
    "held_since": "held_since",
    "holding_since": "held_since",
    "entry_date": "held_since",
    "price": "current_price",
    "current_price": "current_price",
    "last_price": "current_price",
    "buy_price": "buy_or_entry_price",
    "entry_price": "buy_or_entry_price",
    "return": "return",
    "portfolio_return": "return",
    "sector": "sector",
    "rating": "rating",
    "gt_score": "gt_score",
    "score": "gt_score",
}


WATCHLIST_CARD_RE = re.compile(
    r"^(?P<symbol>[A-Z][A-Z0-9.]{0,5})\s+(?P<company>.+?)\s+(?P<price>\$[0-9][0-9,.]*(?:\.\d+)?)\s+(?P<sector>[A-Za-z][A-Za-z &/.-]+)$"
)

# Quant GT's Watchlist card stopped rendering an inline $price (only symbol,
# company, and sector remain), which makes company/sector unsplittable by
# regex alone. Only the symbol is unambiguous here; company/sector/price are
# backfilled from the authenticated Watchlist API response instead.
WATCHLIST_CARD_SYMBOL_ONLY_RE = re.compile(
    r"^(?P<symbol>[A-Z][A-Z0-9.]{0,5})(?:\s+\S+){2,}$"
)


def canonical_header(value: Any) -> str | None:
    return HEADER_ALIASES.get(normalize_header(value))


def extract_pick_date(text: str, mode: str) -> str:
    clean = clean_text(text)
    if mode == "monthly":
        match = re.search(
            rf"\bUpdated\s+on\s+(?:{MONTHS})\s+\d{{1,2}},\s+\d{{4}}\b", clean, re.I
        )
        if match:
            return match.group(0)
        match = re.search(
            rf"\bUpdated\s+(?:{MONTHS})\s+\d{{1,2}},\s+\d{{4}}\b", clean, re.I
        )
        if match:
            return match.group(0)
        # No comma/year at all, and the day number can run directly into an
        # adjacent "MTD +N%" badge with no separating space (e.g.
        # "Updated August 1MTD +6.25%"), so the day can't be closed with \b.
        match = re.search(rf"\bUpdated\s+(?:{MONTHS})\s+\d{{1,2}}(?!\d)", clean, re.I)
        if match:
            return match.group(0)
        match = re.search(
            rf"\b(?:{MONTHS})\s+(?:Holdings\s+)?\d{{2}}/\d{{2}}/\d{{2}}\s*-\s*(?:now|present|current)\b",
            clean,
            re.I,
        )
        if match:
            return match.group(0)
        match = re.search(rf"\b(?:{MONTHS})\s+\d{{4}}\b", clean)
        if match:
            return match.group(0)
        match = re.search(rf"\b(?:{MONTHS})\s+Holdings\b", clean, re.I)
        if match:
            return match.group(0)
    else:
        match = re.search(
            rf"\bUpdated\s+on\s+(?:{MONTHS})\s+\d{{1,2}},\s+\d{{4}}\b", clean, re.I
        )
        if match:
            return match.group(0)
        match = re.search(
            rf"\bUpdated\s+(?:{MONTHS})\s+\d{{1,2}},\s+\d{{4}}\b", clean, re.I
        )
        if match:
            return match.group(0)
        match = re.search(
            rf"\bWeek\s+of\s+(?:{MONTHS})\s+\d{{1,2}},\s+\d{{4}}\b", clean, re.I
        )
        if match:
            return match.group(0)
        match = re.search(r"\b\d{2}/\d{2}/\d{2}\b", clean)
        if match:
            return match.group(0)
    return "Unknown"


def _looks_like_detail_row(cells: list[str]) -> bool:
    if not cells:
        return False
    joined = " ".join(cells)
    return cells[0].startswith("$") or any(
        label in joined for label in KNOWN_DETAIL_LABELS
    )


def rows_from_matrix(
    matrix: Iterable[Iterable[Any]], mode: str
) -> List[Dict[str, Any]]:
    rows = [[clean_text(cell) for cell in row] for row in matrix]
    rows = [row for row in rows if any(row)]
    if not rows:
        return []

    header_index = None
    headers: list[str | None] = []
    for idx, row in enumerate(rows[:8]):
        candidate = [canonical_header(cell) for cell in row]
        if "symbol" in candidate and "gt_score" in candidate:
            header_index = idx
            headers = candidate
            break

    if header_index is not None:
        data_rows = rows[header_index + 1 :]
        out = []
        for row in data_rows:
            if _looks_like_detail_row(row):
                continue
            item: dict[str, Any] = {}
            for col, key in enumerate(headers):
                if key and col < len(row):
                    if (
                        mode == "weekly"
                        and key == "current_price"
                        and normalize_header(rows[header_index][col]) == "price"
                    ):
                        key = "buy_or_entry_price"
                    item[key] = row[col]
            if item.get("symbol") and item.get("gt_score"):
                out.append(item)
        return out

    out = []
    for cells in rows:
        if _looks_like_detail_row(cells):
            continue
        if mode == "monthly" and len(cells) >= 7 and not cells[0].startswith("$"):
            out.append(
                {
                    "company": cells[0],
                    "symbol": cells[1],
                    "held_since": cells[2],
                    "return": cells[3],
                    "sector": cells[4],
                    "rating": cells[5],
                    "gt_score": cells[6],
                }
            )
        elif mode == "weekly" and len(cells) >= 5 and not cells[0].startswith("$"):
            # Current Quant GT weekly table has no Rating column:
            # COMPANY, SYMBOL, PRICE, SECTOR, GT SCORE.
            out.append(
                {
                    "company": cells[0],
                    "symbol": cells[1],
                    "buy_or_entry_price": cells[2],
                    "sector": cells[3],
                    "gt_score": cells[4],
                }
            )
    return out


def row_from_card_text(text: str, mode: str) -> Dict[str, Any] | None:
    clean = clean_text(text)
    if not clean:
        return None
    if mode == "weekly":
        watchlist_match = WATCHLIST_CARD_RE.match(clean)
        if watchlist_match:
            return {
                "symbol": watchlist_match.group("symbol"),
                "company": clean_text(watchlist_match.group("company")),
                "current_price": watchlist_match.group("price"),
                "sector": clean_text(watchlist_match.group("sector")),
                "source_kind": "watchlist",
            }
        if not re.search(r"\bGT\s*Score\b", clean, re.I):
            symbol_only_match = WATCHLIST_CARD_SYMBOL_ONLY_RE.match(clean)
            if symbol_only_match:
                return {
                    "symbol": symbol_only_match.group("symbol"),
                    "source_kind": "watchlist",
                }
    symbol_match = re.search(r"\b[A-Z][A-Z0-9.]{0,5}\b", clean)
    score_match = re.search(r"\bGT\s*Score\b[:\s]*([0-9]+(?:\.[0-9]+)?)", clean, re.I)
    if not symbol_match or not score_match:
        return None
    row: dict[str, Any] = {
        "symbol": symbol_match.group(0),
        "gt_score": score_match.group(1),
    }
    for key, label in [
        ("company", "Company"),
        ("sector", "Sector"),
        ("rating", "Rating"),
        ("held_since", "Held Since"),
        ("return", "Return"),
    ]:
        value = _value_after_label(clean, label)
        if value:
            row[key] = value
    if "company" not in row:
        before_symbol = clean[: symbol_match.start()].strip(" -|")
        if before_symbol:
            row["company"] = before_symbol.split(" GT Score")[0].strip()
    required = {"company", "symbol", "gt_score"}
    if required.issubset(row):
        return row
    return None


def rows_from_card_texts(texts: Iterable[str], mode: str) -> List[Dict[str, Any]]:
    out = []
    seen = set()
    for text in texts:
        row = row_from_card_text(text, mode)
        if not row:
            continue
        key = row.get("symbol") or repr(row)
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def parse_watchlist_dialog_text(text: str, symbol: str) -> Dict[str, str]:
    clean = clean_text(text)
    labels = [
        "P/E (TTM)",
        "Market Cap",
        "Revenue (TTM)",
        "Revenue Growth (YoY)",
        "Next Earnings",
        "Analyst Signal",
        "Analyst Consensus",
        "Momentum",
        "Relative Strength",
        "Headlines",
        "More Headlines",
        "Close",
    ]

    def value_after(label: str, next_labels: Iterable[str]) -> str:
        match = re.search(re.escape(label) + r"\s*:?\s*", clean, re.I)
        if not match:
            return ""
        end = len(clean)
        for next_label in next_labels:
            next_match = re.search(
                re.escape(next_label) + r"\s*:?\s*", clean[match.end() :], re.I
            )
            if next_match:
                end = min(end, match.end() + next_match.start())
        return clean_text(clean[match.end() : end])

    def first_token_after(label: str) -> str:
        # Used only to detect an "unavailable" placeholder (-, N/A, ...)
        # right after a label. When Momentum/Relative Strength have no data,
        # Quant GT renders no label text for them at all (not even a "-"),
        # so value_after's boundary search skips past them and can swallow
        # an entire company-description paragraph up to the next label that
        # *does* appear (e.g. "More Headlines") instead of stopping at a
        # lone "-". Reading only the first whitespace-delimited token right
        # after the label sidesteps that instead of widening the boundary
        # search.
        match = re.search(re.escape(label) + r"\s*:?\s*", clean, re.I)
        if not match:
            return ""
        remainder = clean[match.end() :].strip()
        return remainder.split(" ", 1)[0] if remainder else ""

    entry = re.search(rf"\b{re.escape(symbol)}\s*:\s*(\$[0-9.,]+)", clean, re.I)
    momentum = value_after("Momentum", ["Relative Strength"])
    relative_strength = value_after(
        "Relative Strength", ["Headlines", "More Headlines", "Close"]
    )
    next_earnings_raw = value_after("Next Earnings", labels[5:])
    analyst_signal_raw = value_after("Analyst Signal", labels[6:]) or value_after(
        "Analyst Consensus", labels[7:]
    )
    unavailable_values = {"—", "–", "-", "N/A", "NA"}
    next_earnings_unavailable = (
        next_earnings_raw.upper() in unavailable_values
        or first_token_after("Next Earnings").upper() in unavailable_values
    )
    analyst_signal_unavailable = (
        analyst_signal_raw.upper() in unavailable_values
        or first_token_after("Analyst Signal").upper() in unavailable_values
        or first_token_after("Analyst Consensus").upper() in unavailable_values
    )
    analyst_signal = (
        ""
        if analyst_signal_unavailable
        else parse_analyst_signal_text(analyst_signal_raw)
    )
    momentum_match = re.search(r"([0-9.]+)\s*/\s*2", momentum)
    strength_match = re.search(r"([0-9.]+)\s*/\s*3", relative_strength)
    if momentum_match:
        momentum = f"{momentum_match.group(1)}/2"
    if strength_match:
        relative_strength = f"{strength_match.group(1)}/3"
    details = {
        "buy_or_entry_price": entry.group(1) if entry else "",
        "pe_ttm": value_after("P/E (TTM)", labels[1:]),
        "market_cap": value_after("Market Cap", labels[2:]),
        "revenue_ttm": value_after("Revenue (TTM)", labels[3:]),
        "revenue_growth_yoy": value_after("Revenue Growth (YoY)", labels[4:]),
        "next_earnings": "" if next_earnings_unavailable else next_earnings_raw,
        "next_earnings_unavailable": next_earnings_unavailable,
        "analyst_signal": analyst_signal,
        "analyst_signal_unavailable": analyst_signal_unavailable,
        "momentum": momentum,
        "relative_strength": relative_strength,
    }
    if momentum_match and strength_match:
        score = float(momentum_match.group(1)) + float(strength_match.group(1))
        details["gt_score"] = f"{score:.2f}/5"
    return details


def _value_after_label(text: str, label: str) -> str:
    labels = [
        "Company",
        "Symbol",
        "Held Since",
        "Return",
        "Sector",
        "Rating",
        "GT Score",
        *KNOWN_DETAIL_LABELS,
    ]
    pattern = re.compile(rf"\b{re.escape(label)}\b\s*:?\s*", re.I)
    match = pattern.search(text)
    if not match:
        return ""
    end = len(text)
    for next_label in labels:
        if next_label.lower() == label.lower():
            continue
        next_match = re.search(
            rf"\b{re.escape(next_label)}\b\s*:?", text[match.end() :], re.I
        )
        if next_match:
            end = min(end, match.end() + next_match.start())
    return clean_text(text[match.end() : end])
