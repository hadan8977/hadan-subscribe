"""Preserved structural comparison; no legacy delivery or state."""

import re

IGNORE_KEYS = {"captured_at", "screenshots"}

PAGE_ERROR_MARKERS = (
    "Timeout",
    "Page.goto",
    "net::",
    "Navigation",
    "Target closed",
)


def is_capture_error_page(page: dict | None) -> bool:
    if not isinstance(page, dict):
        return True
    err = str(page.get("error") or "").strip()
    if err:
        return True
    # Snapshot pages must contain at least one stable signal. An empty page is
    # a capture failure, not a website/function update.
    has_signal = bool(
        page.get("headings")
        or page.get("nav")
        or page.get("buttons")
        or page.get("links")
    )
    return not has_signal


NEWS_HOST_MARKERS = (
    "finance.yahoo.com",
    "yahoo.com/m/",
    "fool.com",
    "cnbc.com",
    "seekingalpha.com",
    "marketwatch.com",
    "reuters.com",
    "bloomberg.com",
    "investors.com",
)

NEWS_AGE_RE = re.compile(
    r"\b\d+\s*(?:m|h|d|min|mins|hour|hours|day|days)\s+ago\b", re.I
)

STOCK_CARD_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,5} \S")

PERCENT_CELL_RE = re.compile(r"^[+\-\u2212]?\d+(?:\.\d+)?%$")

DIGEST_ARCHIVE_RE = re.compile(r"^[A-Z]{3} \d{1,2} ")

TRAILING_COUNT_RE = re.compile(r"\s+\d+$")


def _is_external_link(item: str) -> bool:
    return "http" in item and "quantgt.io" not in item


def is_dynamic_member_item(page_name: str, key: str, item: str) -> bool:
    if page_name == "weekly" and key == "buttons":
        return bool(STOCK_CARD_RE.match(item))
    if page_name == "track_record" and key == "buttons":
        return bool(PERCENT_CELL_RE.match(item))
    if page_name == "weekly_digest":
        # The section table of contents (internal #digest-* links) carries the
        # structure; headings repeat it plus the weekly headline.
        return (
            key == "headings"
            or (key == "buttons" and bool(DIGEST_ARCHIVE_RE.match(item)))
            or (key == "links" and _is_external_link(item))
        )
    if page_name == "research":
        return (key == "headings" and item != "Quant Research") or (
            key == "links" and "/research/" in item
        )
    return False


def is_noise_item(page_name: str, key: str, item) -> bool:
    """Return True for dynamic content that should not trigger user alerts."""
    if is_dynamic_member_item(page_name, key, str(item or "")):
        return True
    if page_name == "market_tools":
        # Market Tools is mostly external/news/calendar content and has proven
        # noisy. Treat its content diffs as non-actionable; separate health
        # checks cover scraper/login failures.
        return True
    if key == "links":
        # `item` arrives already stringified (e.g. "('headline 7m ago', 'https://...')"),
        # so a substring match over the whole value covers both text and href.
        combined = str(item or "")
        if any(host in combined for host in NEWS_HOST_MARKERS):
            return True
        if NEWS_AGE_RE.search(combined):
            return True
    return False


def filtered_set(page_name: str, key: str, values):
    kept = {x for x in values if not is_noise_item(page_name, key, x)}
    if page_name == "research" and key == "buttons":
        # Category tabs carry article counts ("ALL 12"); a new article is not a site change.
        kept = {TRAILING_COUNT_RE.sub("", x) for x in kept}
    return kept


def normalize(snapshot: dict):
    pages = {}
    for p in snapshot.get("pages", []):
        name = p.get("name")
        if is_capture_error_page(p):
            # Never compare a failed/empty capture as if the site removed UI.
            continue
        pages[name] = {
            "title": p.get("title"),
            "headings": sorted(set(p.get("headings") or [])),
            "nav": sorted(set(p.get("nav") or [])),
            "buttons": sorted(set(p.get("buttons") or [])),
            "links": sorted(
                set(
                    (x.get("text", ""), x.get("href", ""))
                    for x in (p.get("links") or [])
                )
            ),
        }
    return pages


def failed_pages(snapshot: dict):
    return sorted(
        str(p.get("name"))
        for p in snapshot.get("pages", [])
        if is_capture_error_page(p)
    )


def diff(old, new):
    # If latest snapshot had page capture failures, suppress site-change alerts.
    # A timeout means "unknown", not "page/nav removed".
    if failed_pages(new):
        return []
    # A capture-method change (e.g. the 2026-10 switch from placeholder pages
    # to real member pages) is a new baseline, not a website update.
    if old.get("capture_mode") != new.get("capture_mode"):
        return []
    oldn, newn = normalize(old), normalize(new)
    lines = []
    for name in sorted(set(oldn) | set(newn)):
        if name not in oldn:
            lines.append(f"New page captured: {name}")
            continue
        if name not in newn:
            # Missing in the old snapshot usually means the old capture failed;
            # do not alert that UI was "added" on recovery.
            continue
        for key in ["headings", "nav", "buttons", "links"]:
            oldset = filtered_set(name, key, set(map(str, oldn[name].get(key) or [])))
            newset = filtered_set(name, key, set(map(str, newn[name].get(key) or [])))
            added = sorted(newset - oldset)
            removed = sorted(oldset - newset)
            if added:
                lines.append(f"{name} {key} added: " + "; ".join(added[:8]))
            if removed:
                lines.append(f"{name} {key} removed: " + "; ".join(removed[:8]))
    return lines
