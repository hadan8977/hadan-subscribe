"""Quant GT browser extraction algorithms retained from quantcheck; lifecycle is managed by sources.jobs."""

from __future__ import annotations
import re
from typing import Any, Dict, List
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from hadan.compat.scrape_parse import (
    clean_text,
    extract_pick_date,
    parse_analyst_signal_text,
    parse_watchlist_dialog_text,
    rows_from_card_texts,
    rows_from_matrix,
)

BASE = "https://quantgt.io"


def clean_detail_values(details: Dict[str, Any]) -> Dict[str, Any]:
    return {
        key: value if isinstance(value, bool) else clean_text(value)
        for key, value in details.items()
    }


def is_login_prompt_visible(page) -> bool:
    """Return true only for real auth prompts, not marketing copy mentioning sign in."""
    try:
        if page.locator('input[type="email"], input[type="password"]').count() > 0:
            return True
        return (
            page.get_by_role(
                "button", name=re.compile(r"^(log in|sign in)$", re.I)
            ).count()
            > 0
        )
    except Exception:
        return False


def has_auth_session(page) -> bool:
    try:
        cookies = page.context.cookies(BASE)
        return any(c.get("name") == "__Secure-authjs.session-token" for c in cookies)
    except Exception:
        return False


def is_watchlist_page(page) -> bool:
    try:
        return bool(
            page.evaluate(
                r"""() => {
              const text = (document.querySelector('main')?.innerText || document.body.innerText || '').replace(/\s+/g, ' ');
              const title = document.title || '';
              return /\/weekly-picks\b/i.test(location.pathname) || /\bWatchlist\b/i.test(title) || /\bWatchlist\b/i.test(text);
            }"""
            )
        )
    except Exception:
        return False


def has_picks_content(page) -> bool:
    try:
        return bool(
            page.evaluate(
                """() => {
              const text = (document.querySelector('main')?.innerText || document.body.innerText || '').replace(/\\s+/g, ' ');
              const tableRows = document.querySelectorAll('table tbody tr').length;
              const ariaRows = document.querySelectorAll('[role="row"] [role="cell"], [role="row"] [role="gridcell"]').length;
              return tableRows > 0 || ariaRows > 0 || /\\bGT\\s*Score\\b/i.test(text);
            }"""
            )
        )
    except Exception:
        return False


def has_subscription_gate(page) -> bool:
    try:
        return bool(
            page.evaluate(
                """() => {
              const root = document.querySelector('main') || document.body;
              const text = (root.innerText || document.body.innerText || '').replace(/\\s+/g, ' ').toLowerCase();
              const subscribeButton = [...root.querySelectorAll('button,a')].some(el => /subscribe/i.test((el.innerText || el.textContent || '').trim()));
              const blurred = [...root.querySelectorAll('*')].some(el => {
                const cls = String(el.className || '');
                const style = getComputedStyle(el);
                return /blur\\[|blur-/i.test(cls) || (style.filter && style.filter !== 'none');
              });
              return subscribeButton || blurred || /\\b(subscribe|subscription|upgrade|pricing|member access|paid plan)\\b/.test(text);
            }"""
            )
        )
    except Exception:
        return False


def is_watchlist_dialog_paywalled(text: str) -> bool:
    normalized = clean_text(text).lower()
    return (
        "subscriber-only pick" in normalized
        or "subscribe to unlock the full watchlist" in normalized
        or "subscribe to unlock" in normalized
    )


def wait_for_picks_content(page, timeout: int = 20000) -> None:
    page.wait_for_function(
        """() => {
          const text = (document.querySelector('main')?.innerText || document.body.innerText || '').replace(/\\s+/g, ' ');
          const tableRows = document.querySelectorAll('table tbody tr').length;
          const ariaRows = document.querySelectorAll('[role="row"] [role="cell"], [role="row"] [role="gridcell"]').length;
          return tableRows > 0 || ariaRows > 0 || /\\bGT\\s*Score\\b/i.test(text);
        }""",
        timeout=timeout,
    )


def wait_for_parsable_picks_rows(
    page, mode: str, attempts: int = 3, timeout: int = 20000
) -> List[Dict[str, Any]]:
    """Wait until the rendered page can be parsed into real pick rows.

    Quant GT sometimes renders headings/labels like "GT Score" before the actual
    table/card rows finish hydrating. Treating that as success caused occasional
    zero-row Monthly captures. This helper requires parsed rows, and reloads the
    page before retrying so scheduled runs self-heal instead of immediately
    falling into the validation gate.
    """
    errors = []
    attempts = max(1, int(attempts or 1))
    for attempt in range(1, attempts + 1):
        try:
            wait_for_picks_content(page, timeout=timeout)
            # Give client-side table/card hydration one short extra beat after
            # the first content signal, then parse real rows.
            page.wait_for_timeout(1200)
            rows = rows_from_table(page, mode)
            if rows:
                return rows
            text = clean_text(page.locator("main").inner_text(timeout=3000))[:500]
            errors.append(f"attempt {attempt}: no parsed {mode} rows; main={text!r}")
        except Exception as e:
            errors.append(f"attempt {attempt}: {type(e).__name__}: {e}")
        if attempt < attempts:
            try:
                page.reload(wait_until="domcontentloaded", timeout=45000)
                try:
                    page.wait_for_load_state("load", timeout=15000)
                except PlaywrightTimeoutError:
                    pass
                page.wait_for_timeout(2500)
            except Exception as e:
                errors.append(
                    f"attempt {attempt}: reload failed: {type(e).__name__}: {e}"
                )
    raise RuntimeError(
        f"{mode} page did not produce parsable picks rows after {attempts} attempts: "
        + " | ".join(errors[-3:])
    )


def login(page, email: str, password: str):
    # Prefer the existing authenticated session and only fall back to manual login
    # when the page genuinely lacks usable picks content.
    page.goto(f"{BASE}/quantgt-picks", wait_until="domcontentloaded", timeout=45000)
    try:
        page.wait_for_load_state("load", timeout=15000)
    except PlaywrightTimeoutError:
        pass
    page.wait_for_timeout(2500)
    if (
        has_auth_session(page)
        and has_picks_content(page)
        and not is_login_prompt_visible(page)
    ):
        return
    page.goto(
        f"{BASE}/login?redirect=/quantgt-picks",
        wait_until="domcontentloaded",
        timeout=45000,
    )
    try:
        page.wait_for_load_state("load", timeout=15000)
    except PlaywrightTimeoutError:
        pass
    page.wait_for_timeout(1000)
    if page.get_by_role("button", name=re.compile("log in|sign in", re.I)).count() > 0:
        page.get_by_role(
            "button", name=re.compile("log in|sign in", re.I)
        ).first.click()
    email_box = (
        page.get_by_placeholder("you@example.com")
        if page.get_by_placeholder("you@example.com").count()
        else page.locator('input[type="email"]')
    )
    pass_box = (
        page.get_by_placeholder("min. 8 characters")
        if page.get_by_placeholder("min. 8 characters").count()
        else page.locator('input[type="password"]')
    )
    email_box.fill(email)
    pass_box.fill(password)
    page.get_by_role("button", name=re.compile("log in|sign in", re.I)).first.click()
    try:
        page.wait_for_load_state("domcontentloaded", timeout=20000)
    except PlaywrightTimeoutError:
        pass
    page.wait_for_timeout(3000)
    page.goto(f"{BASE}/quantgt-picks", wait_until="domcontentloaded", timeout=45000)
    try:
        page.wait_for_load_state("load", timeout=15000)
    except PlaywrightTimeoutError:
        pass
    page.wait_for_timeout(3000)
    wait_for_picks_content(page)
    if not has_picks_content(page) or is_login_prompt_visible(page):
        raise RuntimeError("Login did not complete or picks table not visible")


def assert_authenticated_page(page, label: str):
    """Reject unauthenticated/demo/paywalled pages before their content can enter monitor state."""
    if is_login_prompt_visible(page):
        raise RuntimeError(f"{label} page is not authenticated: login prompt visible")
    if has_subscription_gate(page):
        raise RuntimeError(
            f"{label} page is not accessible: subscription/paywall gate visible"
        )
    cookies = page.context.cookies(BASE)
    names = {c.get("name") for c in cookies}
    if "__Secure-authjs.session-token" not in names:
        raise RuntimeError(
            f"{label} page is not authenticated: missing auth session cookie"
        )
    if not has_picks_content(page):
        raise RuntimeError(f"{label} page has no picks content")


def rows_from_table(page, mode: str) -> List[Dict[str, Any]]:
    js = r"""
    (mode) => {
      const clean = s => (s || '').trim().replace(/\s+/g, ' ');
      const tableRows = [...document.querySelectorAll('table tr')]
        .map(tr => [...tr.querySelectorAll('th,td')].map(td => clean(td.innerText || td.textContent)))
        .filter(row => row.some(Boolean));
      const ariaRows = [...document.querySelectorAll('[role="row"]')]
        .map(tr => [...tr.querySelectorAll('[role="columnheader"],[role="cell"],[role="gridcell"]')].map(td => clean(td.innerText || td.textContent)))
        .filter(row => row.some(Boolean));
      const cards = [...document.querySelectorAll('main article, main [data-slot*="card"], main [class*="card"], main [class*="Card"]')]
        .map(el => clean(el.innerText || el.textContent))
        .filter(text => text && (/GT\s*Score|Rating|Sector|Held Since|Return/i.test(text) || (mode === 'weekly' && /^[A-Z][A-Z0-9.]{0,5}(\s+\S+){2,}/.test(text))));
      return {matrix: tableRows.length ? tableRows : ariaRows, cards};
    }
    """
    payload = page.evaluate(js, mode)
    rows = rows_from_matrix(payload.get("matrix") or [], mode)
    if rows:
        return rows
    return rows_from_card_texts(payload.get("cards") or [], mode)


def extract_details_for_visible_expanded(page) -> Dict[str, str]:
    # The Chart GT layout starts with a ticker/chart, while the legacy layout
    # starts with a price. Both use one spanning cell for the expanded details.
    js = r"""
    () => {
      const rows = [...document.querySelectorAll('main table tbody tr')];
      for (const tr of rows) {
        const tds = [...tr.children].filter(el => el.tagName === 'TD');
        const cells = tds.map(td => td.innerText.trim());
        if (tds.length === 1 && (tds[0].colSpan > 1 || cells[0].startsWith('$'))) {
          const txt = cells[0].replace(/\s+/g, ' ');
          const get = (label, nextLabels) => {
            const i = txt.indexOf(label);
            if (i < 0) return '';
            let start = i + label.length;
            let end = txt.length;
            for (const nl of nextLabels) {
              const j = txt.indexOf(nl, start);
              if (j >= 0 && j < end) end = j;
            }
            return txt.slice(start, end).trim();
          };
          return {
            raw: txt,
            current_price: (txt.match(/^\$[0-9.,]+/)||[''])[0],
            chart_return: (txt.match(/([+-][0-9.]+%) ·/)||['',''])[1],
            buy_or_entry_price: get('Buy price:', ['P/E (TTM)', 'Market Cap']) || get('Entry price:', ['P/E (TTM)', 'Market Cap']),
            pe_ttm: get('P/E (TTM)', ['Market Cap']),
            market_cap: get('Market Cap', ['Revenue (TTM)']),
            revenue_ttm: get('Revenue (TTM)', ['Revenue Growth (YoY)']),
            revenue_growth_yoy: get('Revenue Growth (YoY)', ['Next Earnings']),
            next_earnings: get('Next Earnings', ['Analyst Signal', 'Analyst Consensus']),
            analyst_signal: get('Analyst Signal', ['Momentum']) || get('Analyst Consensus', ['Momentum']),
            momentum: get('Momentum', ['Relative Strength']),
            relative_strength: get('Relative Strength', ['Sandisk', 'Applied', 'Lumentum', 'Viavi', 'Ciena', 'FORM', 'DigitalOcean', 'Planet', 'Western', 'Corning'])
          };
        }
      }
      return {};
    }
    """
    return page.evaluate(js)


def expand_and_attach_details(
    page, rows: List[Dict[str, Any]], mode: str
) -> List[Dict[str, Any]]:
    # Expand each data row by symbol and extract metrics from the detail row immediately below it.
    detail_js = r"""
    async (sym) => {
      const sleep = ms => new Promise(r => setTimeout(r, ms));
      const findRow = () => [...document.querySelectorAll('main table tbody tr')]
        .find(tr => [...tr.children].some(td => td.tagName === 'TD' && td.innerText.trim() === sym));
      const detailFor = row => {
        const detail = row?.nextElementSibling;
        const cells = [...(detail?.children || [])].filter(el => el.tagName === 'TD');
        return cells.length === 1 && (cells[0].colSpan > 1 || cells[0].innerText.trim().startsWith('$'))
          ? detail : null;
      };
      let row = findRow();
      if (!row) return {detail_error: 'row not found'};
      let detail = detailFor(row);
      if (!detail) {
        row.click();
      }
      // Chart hydration can lag behind the click. Wait for the actual metrics,
      // not the chart's first character or a fixed animation delay.
      for (let attempt = 0; attempt < 80; attempt++) {
        row = findRow();
        detail = detailFor(row);
        if (detail && /Next Earnings/.test(detail.innerText) && /Analyst (Signal|Consensus)/.test(detail.innerText)) break;
        await sleep(100);
      }
      if (!detail) return {detail_error: 'detail row not found'};
      if (!/Next Earnings/.test(detail.innerText) || !/Analyst (Signal|Consensus)/.test(detail.innerText)) {
        if (row) row.click();
        return {detail_error: 'detail metrics did not load'};
      }
      const txt = detail.innerText.trim().replace(/\s+/g, ' ');
      const get = (label, nextLabels) => {
        const i = txt.indexOf(label);
        if (i < 0) return '';
        const start = i + label.length;
        let end = txt.length;
        for (const nl of nextLabels) {
          const j = txt.indexOf(nl, start);
          if (j >= 0 && j < end) end = j;
        }
        return txt.slice(start, end).trim();
      };
      const out = {
        current_price: (txt.match(/^\$[0-9.,]+/)||[''])[0],
        chart_return: (txt.match(/([+-][0-9.]+%) ·/)||['',''])[1],
        buy_or_entry_price: get('Buy price:', ['P/E (TTM)', 'Market Cap']) || get('Entry price:', ['P/E (TTM)', 'Market Cap']) || ((txt.match(new RegExp('\\b' + sym.replace(/[.*+?^${}()|[\\]\\]/g, '\\$&') + '\\s*:\\s*(\\$[0-9.,]+)')) || ['', ''])[1]),
        pe_ttm: get('P/E (TTM)', ['Market Cap']),
        market_cap: get('Market Cap', ['Revenue (TTM)']),
        revenue_ttm: get('Revenue (TTM)', ['Revenue Growth (YoY)']),
        revenue_growth_yoy: get('Revenue Growth (YoY)', ['Next Earnings']),
        next_earnings: get('Next Earnings', ['Analyst Signal', 'Analyst Consensus']),
        analyst_signal: get('Analyst Signal', ['Momentum']) || get('Analyst Consensus', ['Momentum']),
        momentum: get('Momentum', ['Relative Strength']),
        relative_strength: get('Relative Strength', [])
      };
      // Collapse after extraction so the next lookup is clean.
      row = findRow();
      if (detailFor(row)) row.click();
      await sleep(150);
      return out;
    }
    """
    for r in rows:
        sym = r.get("symbol")
        if not sym:
            continue
        try:
            details = page.evaluate(detail_js, sym)
            if details:
                # Current Chart GT details omit the price at the start. Keep
                # prices and other values already captured from the main row.
                r.update(
                    {
                        key: value
                        for key, value in clean_detail_values(details).items()
                        if value != ""
                    }
                )
                r["analyst_signal"] = parse_analyst_signal_text(r.get("analyst_signal"))
        except Exception as e:
            r["detail_error"] = f"detail not captured: {type(e).__name__}"
    return rows


def format_watchlist_score(value: Any) -> str:
    try:
        return f"{float(value):.2f}/5"
    except (TypeError, ValueError):
        return ""


def merge_watchlist_api_scores(
    rows: List[Dict[str, Any]], api_rows: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Attach the authenticated Watchlist API's native score, and backfill
    company/sector/current_price when the card text no longer renders them."""
    api_by_symbol = {
        clean_text(item.get("ticker")).upper(): item
        for item in api_rows
        if clean_text(item.get("ticker"))
    }
    missing = []
    for row in rows:
        symbol = clean_text(row.get("symbol")).upper()
        item = api_by_symbol.get(symbol)
        score = format_watchlist_score(item.get("score")) if item else ""
        if not score:
            missing.append(symbol or "?")
            continue
        row["gt_score"] = score
        row["gt_score_source"] = "weekly_api_score"
        if not row.get("company") and item.get("name"):
            row["company"] = clean_text(item.get("name"))
        if not row.get("sector") and item.get("sector"):
            row["sector"] = clean_text(item.get("sector"))
        # `price` is the price at this week's signal (`signal_ts`); `sell_price`
        # is the live price (it matches the Portfolio page's current prices).
        if isinstance(item.get("price"), (int, float)):
            row["signal_price"] = f"${item['price']:,.2f}"
        if item.get("signal_ts"):
            row["signal_at"] = str(item["signal_ts"])
            row["signal_date"] = str(item["signal_ts"])[:10]
        if not row.get("current_price"):
            api_price = item.get("sell_price")
            if api_price is None:
                api_price = item.get("price")
            if isinstance(api_price, (int, float)):
                row["current_price"] = f"${api_price:,.2f}"
    if missing:
        raise RuntimeError(
            "watchlist API response missing valid GT Score for: "
            + ", ".join(missing[:5])
        )
    return rows


def fetch_watchlist_api_rows(page) -> List[Dict[str, Any]]:
    """Read the member-only Watchlist payload used by the rendered page itself."""
    payload = page.evaluate(
        """async () => {
          const response = await fetch('/api/proxy/api/weekly/stocks', { credentials: 'same-origin' });
          let body = null;
          try { body = await response.json(); } catch (_) {}
          return { status: response.status, body };
        }"""
    )
    status = int((payload or {}).get("status") or 0)
    rows = (payload or {}).get("body")
    if status != 200 or not isinstance(rows, list) or not rows:
        raise RuntimeError(f"watchlist API did not return member rows: status={status}")
    return rows


def attach_digest_reasons(
    page, rows: List[Dict[str, Any]], api_rows: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Best effort: add the Weekly Digest's one-line reason ("At a 52-week high")
    to each watchlist row. Only used when the digest is for the same week as the
    watchlist; any failure leaves rows untouched -- this is display context and
    must never break the picks scrape."""
    try:
        payload = page.evaluate(
            """async () => {
              const response = await fetch('/api/proxy/api/weekly-digest/latest', { credentials: 'same-origin' });
              let body = null;
              try { body = await response.json(); } catch (_) {}
              return { status: response.status, body };
            }"""
        )
        body = (payload or {}).get("body") or {}
        if int((payload or {}).get("status") or 0) != 200 or not isinstance(body, dict):
            return rows
        weeks = {str(item.get("week_start") or "") for item in api_rows}
        if str(body.get("week_start") or "") not in weeks:
            return rows
        reasons = {
            clean_text(item.get("ticker")).upper(): clean_text(item.get("reason"))
            for item in ((body.get("sections") or {}).get("watchlist") or [])
            if isinstance(item, dict) and item.get("ticker") and item.get("reason")
        }
        for row in rows:
            reason = reasons.get(clean_text(row.get("symbol")).upper())
            if reason:
                row["watch_reason"] = reason
    except Exception:
        return rows
    return rows


def expand_watchlist_and_attach_details(
    page, rows: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    for row in rows:
        symbol = row.get("symbol")
        if not symbol:
            continue
        last_error = "dialog did not open"
        paywall_detected = False
        for _ in range(2):
            try:
                # Scope to <main>: a page chrome element (e.g. an account
                # avatar showing a single-letter initial) can have the exact
                # same text as a short ticker like "U" and would otherwise
                # win page-wide text matching, opening the wrong element.
                page.locator("main").get_by_text(symbol, exact=True).first.click()
                dialog = page.locator('[role="dialog"]')
                dialog.wait_for(state="visible", timeout=8000)
                text = clean_text(dialog.inner_text(timeout=5000))
                if is_watchlist_dialog_paywalled(text):
                    paywall_detected = True
                    raise RuntimeError(
                        "watchlist detail is paywalled: current session lacks Watchlist subscription access"
                    )
                if not re.match(rf"^{re.escape(symbol)}\b", text):
                    raise RuntimeError(f"dialog symbol mismatch for {symbol}")
                details = parse_watchlist_dialog_text(text, symbol)
                row.update(clean_detail_values(details))
                page.get_by_role("button", name="Close").last.click()
                dialog.wait_for(state="hidden", timeout=5000)
                last_error = ""
                break
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                try:
                    page.keyboard.press("Escape")
                    page.locator('[role="dialog"]').wait_for(
                        state="hidden", timeout=3000
                    )
                except Exception:
                    pass
        if paywall_detected:
            raise RuntimeError(
                "watchlist detail is paywalled: current session lacks Watchlist subscription access"
            )
        if last_error:
            row["detail_error"] = last_error
    return rows


def _wait_for_screenshot_ready(page, name: str) -> None:
    """Wait for the page to finish hydrating before taking an email screenshot.

    Playwright's full_page screenshot uses the document height at capture time. On
    Quant GT weekly picks the shell/table can appear first at ~1200px tall, then
    the full Top 10 list and expanded detail row hydrate a second or two later at
    ~1800px. Capturing immediately produced truncated weekly attachments.
    """
    wait_for_picks_content(page)
    rows = wait_for_parsable_picks_rows(page, name)
    if name == "weekly" and len(rows) < 10:
        raise RuntimeError(
            f"weekly screenshot not ready: expected 10 parsed rows, got {len(rows)}"
        )
    try:
        page.wait_for_function(
            r"""(mode) => {
              const height = Math.max(document.body.scrollHeight, document.documentElement.scrollHeight);
              if (mode === 'weekly') {
                const text = (document.querySelector('main')?.innerText || document.body.innerText || '').replace(/\s+/g, ' ');
                if (/\bWatchlist\b/i.test(text) || /\/weekly-picks\b/i.test(location.pathname)) {
                  return height >= window.innerHeight;
                }
                return height > window.innerHeight + 400;
              }
              return height >= window.innerHeight;
            }""",
            arg=name,
            timeout=20000,
        )
    except PlaywrightTimeoutError:
        pass
    page.wait_for_timeout(1000)


def dismiss_screenshot_overlays(page) -> None:
    """Close optional product tours so email screenshots show unobscured data."""
    dialog = page.get_by_role("dialog", name=re.compile(r"Latest Holdings", re.I))
    if dialog.count() == 0:
        return
    close = dialog.get_by_role("button", name=re.compile(r"close", re.I))
    if close.count() == 0:
        close = dialog.locator('button[aria-label*="close" i], button:has-text("×")')
    if close.count() > 0:
        close.click(timeout=3000)
        page.wait_for_timeout(300)


def _assert_screenshot_symbols_match(
    page, name: str, expected_rows: List[Dict[str, Any]]
) -> None:
    expected_symbols = [
        str(row.get("symbol") or "").strip().upper()
        for row in expected_rows
        if row.get("symbol")
    ]
    page_rows = rows_from_table(page, name)
    page_symbols = [
        str(row.get("symbol") or "").strip().upper()
        for row in page_rows
        if row.get("symbol")
    ]
    if name == "weekly":
        expected_symbols = expected_symbols[:10]
        page_symbols = page_symbols[:10]
    if expected_symbols != page_symbols:
        raise RuntimeError(
            f"{name} screenshot symbols mismatch: expected={expected_symbols} actual={page_symbols}"
        )
