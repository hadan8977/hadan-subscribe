"""Subscriber-facing "picks changed" email: subject, HTML and plain text.

Design goals (2026-10 redesign):
- The subject alone tells a subscriber what happened ("Portfolio rebalance:
  +MRNA +VEEV -HPE -PANW") instead of every alert being "Quant GT Picks
  Updated" -- historically only ~20 of ~70 alerts were real holdings changes.
- Mobile first: one line per field change, GT Score re-ratings collapsed into
  wrapping chips, holdings as two-line rows rather than tall stacked cards.
- No internal plumbing in subscriber mail (scheduler window names, raw UTC
  ISO timestamps). Admin-only context goes in an explicit `banner`.

Everything here is pure string building over (data, diff, previous) so it can
be unit tested without Playwright or network access.
"""

from __future__ import annotations

import html
from typing import Any, Dict, List

from hadan.compat.picks_format import (
    compact_symbols,
    display_date,
    fetched_at_utc,
    field_label,
    format_fetched,
    format_gt,
    is_blank,
    parse_date,
    parse_gt_score,
    parse_pct,
    parse_signal,
    rows_by_symbol,
    section_title,
    since_signal,
    short_date,
    stock_count,
)

SECTIONS = ("monthly", "weekly")
SUBJECT_PREFIX = "Quant GT · "
# Scrape metadata that may still show up in an old diff; never shown to subscribers.
HIDDEN_FIELDS = {
    "source_kind",
    "detail_error",
    "gt_score_source",
    "analyst_signal_unavailable",
    "next_earnings_unavailable",
}

INK = "#0f172a"
MUTED = "#64748b"
FAINT = "#94a3b8"
LINE = "#e2e8f0"
BRAND = "#16a34a"
UP = "#15803d"
DOWN = "#b91c1c"
FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"


def esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))


# ---------------------------------------------------------------------------
# Diff interpretation
# ---------------------------------------------------------------------------


def _section_diff(diff: Dict[str, Any] | None, key: str) -> Dict[str, Any]:
    d = (diff or {}).get(key) or {}
    return d if d.get("changed_flag") else {}


def _visible_changes(sec: Dict[str, Any]) -> List[tuple[str, str, Any, Any]]:
    """Flatten diff rows to (symbol, field, old, new), dropping hidden fields."""
    out = []
    for row in sec.get("changed") or []:
        for name, vals in (row.get("fields") or {}).items():
            if name in HIDDEN_FIELDS:
                continue
            out.append(
                (
                    row.get("symbol") or "?",
                    name,
                    (vals or {}).get("old"),
                    (vals or {}).get("new"),
                )
            )
    return out


def _numeric(field: str, value: Any) -> float | None:
    if field == "gt_score":
        return parse_gt_score(value)
    if field == "analyst_signal":
        return parse_signal(value)[1]
    if field == "return":
        return parse_pct(value)
    return None


def _delta(field: str, old: Any, new: Any) -> float | None:
    a, b = _numeric(field, old), _numeric(field, new)
    return None if a is None or b is None else b - a


def classify(diff: Dict[str, Any] | None) -> str:
    """One of: rebalance, watchlist, both, signal, refresh, details, none."""
    if not diff or not diff.get("changed"):
        return "none"
    monthly, weekly = _section_diff(diff, "monthly"), _section_diff(diff, "weekly")
    m_moves = bool(monthly.get("added") or monthly.get("removed"))
    w_moves = bool(weekly.get("added") or weekly.get("removed"))
    if m_moves and w_moves:
        return "both"
    if m_moves:
        return "rebalance"
    if w_moves:
        return "watchlist"
    changes = _visible_changes(monthly) + _visible_changes(weekly)
    if any(field == "analyst_signal" for _, field, _, _ in changes):
        return "signal"
    if changes:
        return "details"
    if monthly.get("date") or weekly.get("date"):
        return "refresh"
    return "none"


def _moves(sec: Dict[str, Any], limit: int) -> str:
    return " ".join(
        part
        for part in (
            compact_symbols(list(sec.get("added") or []), limit, "+"),
            compact_symbols(list(sec.get("removed") or []), limit, "-"),
        )
        if part
    )


def _signal_changes(diff: Dict[str, Any]) -> List[tuple[str, Any, Any]]:
    seen: Dict[str, tuple[str, Any, Any]] = {}
    for key in SECTIONS:
        for symbol, field, old, new in _visible_changes(_section_diff(diff, key)):
            if field == "analyst_signal":
                seen.setdefault(symbol, (symbol, old, new))
    return list(seen.values())


def build_subject(diff: Dict[str, Any] | None, data: Dict[str, Any]) -> str:
    kind = classify(diff)
    monthly, weekly = _section_diff(diff, "monthly"), _section_diff(diff, "weekly")
    weekly_title = section_title("weekly", data.get("weekly"))
    if kind == "both":
        text = f"Portfolio rebalance: {_moves(monthly, 2)} | {weekly_title}: {_moves(weekly, 2)}"
    elif kind == "rebalance":
        text = f"Portfolio rebalance: {_moves(monthly, 4)}"
    elif kind == "watchlist":
        text = f"{weekly_title}: {_moves(weekly, 4)}"
    elif kind == "signal":
        signals = _signal_changes(diff or {})
        if len(signals) == 1:
            symbol, old, new = signals[0]
            text = f"Analyst consensus: {symbol} {old or '—'} → {new or '—'}"
        else:
            symbols = [s for s, _, _ in signals]
            more = f" +{len(symbols) - 3} more" if len(symbols) > 3 else ""
            text = f"Analyst consensus changes: {', '.join(symbols[:3])}{more}"
    elif kind == "details":
        symbols = sorted(
            {
                s
                for k in SECTIONS
                for s, _, _, _ in _visible_changes(_section_diff(diff, k))
            }
        )
        fields = sorted(
            {
                field_label(f)
                for k in SECTIONS
                for _, f, _, _ in _visible_changes(_section_diff(diff, k))
            }
        )
        what = fields[0] if len(fields) == 1 else "Pick details"
        text = f"{what} updated: {compact_symbols(symbols, 3)}"
    elif kind == "refresh":
        which = (
            "Portfolio"
            if monthly.get("date") and not weekly.get("date")
            else weekly_title
        )
        new_date = (
            (monthly if which == "Portfolio" else weekly).get("date", {}).get("new")
        )
        text = f"{which} refreshed for {display_date(new_date)}, no stock changes"
    else:
        text = "Current picks"
    return SUBJECT_PREFIX + text


def _headline(kind: str, data: Dict[str, Any]) -> str:
    weekly_title = section_title("weekly", data.get("weekly"))
    return {
        "both": f"Portfolio rebalanced & {weekly_title} updated",
        "rebalance": "Portfolio rebalanced",
        "watchlist": f"{weekly_title} updated",
        "signal": "Analyst consensus change",
        "details": "Pick details updated",
        "refresh": "Picks refreshed",
        "none": "Current picks",
    }[kind]


def _section_summary(key: str, sec: Dict[str, Any], data: Dict[str, Any]) -> str:
    bits = []
    if sec.get("added"):
        bits.append(f"{len(sec['added'])} added")
    if sec.get("removed"):
        bits.append(f"{len(sec['removed'])} removed")
    changes = _visible_changes(sec)
    gt = {s for s, f, _, _ in changes if f == "gt_score"}
    signals = {s for s, f, _, _ in changes if f == "analyst_signal"}
    other = {s for s, f, _, _ in changes if f not in ("gt_score", "analyst_signal")}
    if signals:
        bits.append(
            f"analyst consensus moved on {len(signals)} stock{'s' if len(signals) != 1 else ''}"
        )
    if gt:
        bits.append(f"GT Score re-rated on {len(gt)}")
    if other:
        bits.append(f"details updated on {len(other)}")
    if not bits and sec.get("date"):
        bits.append("new update date, same stocks")
    title = section_title(key, data.get(key))
    date = display_date((data.get(key) or {}).get("pick_date"))
    return f"{title} ({date}): " + ", ".join(bits)


def summary_lines(diff: Dict[str, Any] | None, data: Dict[str, Any]) -> List[str]:
    return [
        _section_summary(k, _section_diff(diff, k), data)
        for k in SECTIONS
        if _section_diff(diff, k)
    ]


# ---------------------------------------------------------------------------
# HTML building blocks (table-based, inline styles: Gmail / QQ Mail / Outlook safe)
# ---------------------------------------------------------------------------


def _pill(text: str, fg: str, bg: str) -> str:
    return (
        f'<span style="display:inline-block;font-size:10px;line-height:1;font-weight:700;letter-spacing:.04em;'
        f'color:{fg};background:{bg};border-radius:4px;padding:3px 5px;vertical-align:2px;">{esc(text)}</span>'
    )


def _signed_color(value: float | None) -> str:
    if value is None or abs(value) < 1e-9:
        return INK
    return UP if value > 0 else DOWN


def _row_meta(row: Dict[str, Any], key: str) -> str:
    bits = []
    if not is_blank(row.get("sector")):
        bits.append(esc(row.get("sector")))
    if key == "monthly" and not is_blank(row.get("gt_score")):
        bits.append(f"GT {esc(format_gt(row.get('gt_score')))}")
    if not is_blank(row.get("analyst_signal")):
        bits.append(esc(row.get("analyst_signal")))
    if not is_blank(row.get("next_earnings")):
        bits.append(f"Earnings {esc(short_date(row.get('next_earnings')))}")
    return " · ".join(bits)


def _signal_day(row: Dict[str, Any]) -> str:
    day = parse_date(row.get("signal_date"))
    return f"{day:%b} {day.day}" if day else str(row.get("signal_date") or "")


def _row_figure(row: Dict[str, Any], key: str, as_of=None) -> str:
    """Right-hand column: Return for the Portfolio, GT Score for the watchlist."""
    price = row.get("current_price")
    entry = row.get("buy_or_entry_price")
    sub = esc(price) if not is_blank(price) else ""
    if not is_blank(entry):
        label = "entry" if key == "monthly" else "buy"
        sub = f"{sub}<br>{label} {esc(entry)}" if sub else f"{label} {esc(entry)}"
    if key == "monthly":
        ret = row.get("return")
        main = (
            f'<span style="color:{_signed_color(parse_pct(ret))};">{esc(ret)}</span>'
            if not is_blank(ret)
            else "&nbsp;"
        )
    else:
        gt = parse_gt_score(row.get("gt_score"))
        main = (
            f'{gt:.2f}<span style="font-size:11px;color:{FAINT};font-weight:600;"> GT</span>'
            if gt is not None
            else "&nbsp;"
        )
        move = since_signal(row, as_of)
        if move is not None:
            when = (
                f" since {_signal_day(row)}"
                if row.get("signal_date")
                else " since signal"
            )
            sub += f'<br><span style="color:{_signed_color(move)};font-weight:700;">{move * 100:+.1f}%</span>{esc(when)}'

    return (
        f'<div style="font-size:16px;line-height:1.25;font-weight:700;color:{INK};white-space:nowrap;">{main}</div>'
        f'<div style="font-size:12px;line-height:1.4;color:{MUTED};white-space:nowrap;margin-top:2px;">{sub}</div>'
    )


def _stock_row(
    row: Dict[str, Any],
    key: str,
    badge: str = "",
    figure: bool = True,
    note: str = "",
    as_of=None,
) -> str:
    symbol = row.get("symbol") or "?"
    company = row.get("company") or ""
    meta = note or _row_meta(row, key)
    reason = row.get("watch_reason")
    reason_html = (
        f'<div style="font-size:12px;line-height:1.4;color:{UP};font-weight:600;margin-top:1px;">{esc(reason)}</div>'
        if key == "weekly" and not is_blank(reason)
        else ""
    )
    right = ""
    if figure:
        right = (
            f'<td valign="top" align="right" style="padding:10px 0 10px 10px;border-top:1px solid {LINE};width:1%;">'
            f"{_row_figure(row, key, as_of)}</td>"
        )
    return f"""
      <tr>
        <td valign="top" style="padding:10px 0;border-top:1px solid {LINE};">
          <div style="font-size:15px;line-height:1.3;font-weight:800;color:{INK};">{esc(symbol)} {badge}</div>
          <div style="font-size:13px;line-height:1.35;color:#334155;">{esc(company)}</div>
          {reason_html}
          <div style="font-size:12px;line-height:1.45;color:{MUTED};margin-top:2px;">{meta}</div>
        </td>
        {right}
      </tr>"""


def _table(rows_html: str) -> str:
    return (
        '<table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" '
        f'style="border-collapse:collapse;width:100%;">{rows_html}</table>'
    )


def _eyebrow(text: str, extra: str = "") -> str:
    tail = (
        f'<span style="color:{FAINT};font-weight:600;letter-spacing:0;text-transform:none;"> · {esc(extra)}</span>'
        if extra
        else ""
    )
    return (
        f'<div style="font-size:11px;line-height:1.3;font-weight:800;letter-spacing:.08em;text-transform:uppercase;'
        f'color:{MUTED};margin:0 0 4px 0;">{esc(text)}{tail}</div>'
    )


def _value_change(field: str, old: Any, new: Any) -> str:
    delta = _delta(field, old, new)
    delta_html = ""
    if delta is not None and abs(delta) >= 0.005:
        delta_html = f' <span style="color:{_signed_color(delta)};font-weight:700;">({delta:+.2f})</span>'
    old_text = esc(old) if not is_blank(old) else "—"
    new_text = esc(new) if not is_blank(new) else "—"
    return (
        f'<span style="color:{MUTED};">{old_text}</span>'
        f' <span style="color:{FAINT};">→</span> '
        f'<span style="color:{INK};font-weight:700;">{new_text}</span>{delta_html}'
    )


def _gt_chips(items: List[tuple[str, Any, Any]]) -> str:
    chips = []
    for symbol, old, new in items:
        delta = _delta("gt_score", old, new)
        arrow = (
            "" if delta is None or abs(delta) < 0.005 else ("▲" if delta > 0 else "▼")
        )
        chips.append(
            f'<span style="display:inline-block;white-space:nowrap;border:1px solid {LINE};border-radius:6px;'
            f'padding:4px 7px;margin:0 6px 6px 0;font-size:12px;line-height:1.2;color:{INK};">'
            f'<b>{esc(symbol)}</b> <span style="color:{MUTED};">{esc(format_gt(old))}→</span>{esc(format_gt(new))}'
            f'<span style="color:{_signed_color(delta)};"> {arrow}</span></span>'
        )
    return "".join(chips)


def _changes_block(
    key: str, sec: Dict[str, Any], data: Dict[str, Any], previous: Dict[str, Any] | None
) -> str:
    current_rows = rows_by_symbol((data.get(key) or {}).get("rows"))
    previous_rows = rows_by_symbol(((previous or {}).get(key) or {}).get("rows"))
    title = section_title(key, data.get(key))
    date_info = sec.get("date") or {}
    when = display_date((data.get(key) or {}).get("pick_date"))
    if date_info.get("old"):
        when = f"{short_date(display_date(date_info.get('new')))} · was {short_date(display_date(date_info.get('old')))}"
    parts = [_eyebrow(title, when)]

    rows_html = ""
    for symbol in sec.get("added") or []:
        row = current_rows.get(symbol, {"symbol": symbol})
        rows_html += _stock_row(row, key, _pill("ADDED", UP, "#dcfce7"), figure=False)
    for symbol in sec.get("removed") or []:
        row = previous_rows.get(symbol, {"symbol": symbol})
        note = ""
        if key == "monthly" and not is_blank(row.get("return")):
            ret = row.get("return")
            note = f'Last return <span style="color:{_signed_color(parse_pct(ret))};font-weight:700;">{esc(ret)}</span>'
        rows_html += _stock_row(
            row, key, _pill("REMOVED", DOWN, "#fee2e2"), figure=False, note=note
        )

    changes = _visible_changes(sec)
    for symbol, field, old, new in changes:
        if field == "gt_score":
            continue
        rows_html += f"""
      <tr>
        <td valign="top" style="padding:9px 10px 9px 0;border-top:1px solid {LINE};width:52px;font-size:14px;font-weight:800;color:{INK};">{esc(symbol)}</td>
        <td valign="top" style="padding:9px 0;border-top:1px solid {LINE};">
          <div style="font-size:11px;line-height:1.3;color:{MUTED};text-transform:uppercase;letter-spacing:.04em;">{esc(field_label(field))}</div>
          <div style="font-size:14px;line-height:1.4;">{_value_change(field, old, new)}</div>
        </td>
      </tr>"""
    if rows_html:
        parts.append(_table(rows_html))

    gt_items = [(s, o, n) for s, f, o, n in changes if f == "gt_score"]
    if gt_items:
        parts.append(
            f'<div style="font-size:12px;color:{MUTED};margin:10px 0 6px 0;">GT Score re-rated</div>'
            f"<div>{_gt_chips(gt_items)}</div>"
        )
    if len(parts) == 1:
        parts.append(
            f'<div style="font-size:13px;color:{MUTED};">New update date, same stocks.</div>'
        )
    return f'<div style="margin:0 0 18px 0;">{"".join(parts)}</div>'


def _holdings_block(
    key: str, data: Dict[str, Any], added: List[str], as_of=None
) -> str:
    section = data.get(key) or {}
    rows = section.get("rows") or []
    title = section_title(key, section)
    header = _eyebrow(
        f"{title} · {stock_count(len(rows))}", display_date(section.get("pick_date"))
    )
    if not rows:
        return f'<div style="margin:22px 0 0 0;">{header}<div style="font-size:13px;color:{MUTED};">No rows captured.</div></div>'
    body = "".join(
        _stock_row(
            row,
            key,
            _pill("NEW", UP, "#dcfce7") if (row.get("symbol") in added) else "",
            as_of=as_of,
        )
        for row in rows
    )
    return f'<div style="margin:22px 0 0 0;">{header}{_table(body)}</div>'


def build_html(
    data: Dict[str, Any],
    diff: Dict[str, Any] | None = None,
    previous: Dict[str, Any] | None = None,
    banner: str | None = None,
) -> str:
    kind = classify(diff)
    headline = _headline(kind, data)
    summaries = summary_lines(diff, data)
    preheader = build_subject(diff, data).removeprefix(SUBJECT_PREFIX) + (
        " — " + "; ".join(summaries) if summaries else ""
    )
    fetched = format_fetched(data)

    banner_html = ""
    if banner:
        banner_html = (
            f'<div style="background:#fffbeb;border:1px solid #fde68a;border-radius:8px;padding:9px 11px;'
            f'font-size:13px;line-height:1.45;color:#92400e;margin:0 0 14px 0;">{esc(banner)}</div>'
        )
    summary_html = "".join(
        f'<div style="font-size:14px;line-height:1.5;color:#334155;">{esc(line)}</div>'
        for line in summaries
    )

    changes_html = ""
    if kind != "none":
        blocks = "".join(
            _changes_block(k, _section_diff(diff, k), data, previous)
            for k in SECTIONS
            if _section_diff(diff, k)
        )
        changes_html = (
            f'<div style="margin:20px 0 4px 0;padding:16px 14px 0 14px;background:#f8fafc;border:1px solid {LINE};border-radius:10px;">'
            f'<div style="font-size:16px;font-weight:800;color:{INK};margin:0 0 12px 0;">What changed</div>{blocks}</div>'
        )

    ordered = sorted(SECTIONS, key=lambda k: 0 if _section_diff(diff, k) else 1)
    holdings_html = "".join(
        _holdings_block(
            k,
            data,
            list(_section_diff(diff, k).get("added") or []),
            fetched_at_utc(data),
        )
        for k in ordered
    )

    return f"""<!doctype html>
<html>
  <head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(headline)}</title></head>
  <body style="margin:0;padding:0;background:#f1f5f4;font-family:{FONT};color:{INK};">
    <div style="display:none;max-height:0;overflow:hidden;opacity:0;color:transparent;">{esc(preheader)}</div>
    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="background:#f1f5f4;">
      <tr><td align="center" style="padding:14px 8px;">
        <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="max-width:600px;background:#ffffff;border:1px solid {LINE};border-radius:14px;">
          <tr><td style="padding:20px 18px 18px 18px;">
            <div style="font-size:11px;font-weight:800;letter-spacing:.1em;text-transform:uppercase;color:{BRAND};">Quant GT Picks Alert</div>
            <h1 style="font-size:23px;line-height:1.25;margin:6px 0 6px 0;color:{INK};">{esc(headline)}</h1>
            {banner_html}
            {summary_html}
            {changes_html}
            {holdings_html}
            <div style="margin:22px 0 0 0;padding:12px 0 0 0;border-top:1px solid {LINE};font-size:12px;line-height:1.55;color:{MUTED};">
              Full details for every stock are in the attached Excel report, along with screenshots of the Portfolio and Weekly Watchlist pages.<br>
              {("Data checked " + esc(fetched) + ".") if fetched else ""}
            </div>
          </td></tr>
        </table>
      </td></tr>
    </table>
  </body>
</html>"""


# ---------------------------------------------------------------------------
# Plain text (multipart alternative; also what historical_resend validates)
# ---------------------------------------------------------------------------


def _text_row(row: Dict[str, Any], key: str, as_of=None) -> str:
    bits = []
    if key == "monthly" and not is_blank(row.get("return")):
        bits.append(str(row.get("return")))
    if not is_blank(row.get("gt_score")):
        bits.append(f"GT {format_gt(row.get('gt_score'))}")
    if not is_blank(row.get("current_price")):
        price = str(row.get("current_price"))
        if not is_blank(row.get("buy_or_entry_price")):
            price += f" ({'entry' if key == 'monthly' else 'buy'} {row.get('buy_or_entry_price')})"
        bits.append(price)
    if not is_blank(row.get("analyst_signal")):
        bits.append(str(row.get("analyst_signal")))
    move = since_signal(row, as_of) if key == "weekly" else None
    if move is not None:
        bits.append(f"{move * 100:+.1f}% since {_signal_day(row) or 'signal'}")
    if key == "weekly" and not is_blank(row.get("watch_reason")):
        bits.append(str(row.get("watch_reason")))
    return f"{row.get('symbol') or '?'} — {row.get('company') or ''}" + (
        f" | {' · '.join(bits)}" if bits else ""
    )


def build_text(
    data: Dict[str, Any],
    diff: Dict[str, Any] | None = None,
    previous: Dict[str, Any] | None = None,
    banner: str | None = None,
) -> str:
    kind = classify(diff)
    lines = [_headline(kind, data)]
    if banner:
        lines += ["", f"Note: {banner}"]
    if kind != "none":
        lines += ["", "Changes:"]
        for key in SECTIONS:
            sec = _section_diff(diff, key)
            if not sec:
                continue
            title = section_title(key, data.get(key))
            date_info = sec.get("date") or {}
            when = display_date((data.get(key) or {}).get("pick_date"))
            if date_info.get("old"):
                when = f"{display_date(date_info.get('new'))} (previously {display_date(date_info.get('old'))})"
            lines.append(f"{title} — {when}")
            current_rows = rows_by_symbol((data.get(key) or {}).get("rows"))
            previous_rows = rows_by_symbol(
                ((previous or {}).get(key) or {}).get("rows")
            )
            for symbol in sec.get("added") or []:
                row = current_rows.get(symbol, {"symbol": symbol})
                lines.append(
                    f"  + Added {symbol} — {row.get('company') or ''}".rstrip(" —")
                )
            for symbol in sec.get("removed") or []:
                row = previous_rows.get(symbol, {"symbol": symbol})
                lines.append(
                    f"  - Removed {symbol} — {row.get('company') or ''}".rstrip(" —")
                )
            changes = _visible_changes(sec)
            for symbol, field, old, new in changes:
                if field != "gt_score":
                    lines.append(
                        f"  * {symbol} {field_label(field)}: {old if not is_blank(old) else '—'} -> {new if not is_blank(new) else '—'}"
                    )
            gt_items = [
                f"{s} {format_gt(o)}->{format_gt(n)}"
                for s, f, o, n in changes
                if f == "gt_score"
            ]
            if gt_items:
                lines.append("  * GT Score re-rated: " + ", ".join(gt_items))
    for key in sorted(SECTIONS, key=lambda k: 0 if _section_diff(diff, k) else 1):
        section = data.get(key) or {}
        rows = section.get("rows") or []
        lines += [
            "",
            f"{section_title(key, section)} — {display_date(section.get('pick_date'))} · {stock_count(len(rows))}",
        ]
        lines += [
            f"  {i}. {_text_row(row, key, fetched_at_utc(data))}"
            for i, row in enumerate(rows, 1)
        ] or ["  (no rows captured)"]
    fetched = format_fetched(data)
    lines += ["", "Full details: attached Excel report and page screenshots."]
    if fetched:
        lines.append(f"Data checked {fetched}.")
    return "\n".join(lines)
