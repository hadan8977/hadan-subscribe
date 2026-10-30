"""Preserved weekly digest formatting, without legacy state or delivery code."""

from __future__ import annotations
import html
from datetime import datetime, date, timezone
from typing import Any, Dict, List
from hadan.compat.picks_email import (
    BRAND,
    DOWN,
    FAINT,
    FONT,
    INK,
    LINE,
    MUTED,
    SUBJECT_PREFIX,
    UP,
    _pill,
    _table,
)


class DigestError(RuntimeError):
    pass


def esc(value):
    return html.escape(str(value if value is not None else ""))


def validate(digest: Dict[str, Any]) -> Dict[str, Any]:
    if digest.get("locked"):
        raise DigestError(
            "weekly digest is locked for this account (subscription problem?)"
        )
    week = str(digest.get("week_start") or "")
    try:
        date.fromisoformat(week)
    except ValueError as exc:
        raise DigestError(f"weekly digest has no valid week_start: {week!r}") from exc
    if not str(digest.get("headline") or "").strip():
        raise DigestError(f"weekly digest {week} has no headline")
    sections = digest.get("sections")
    if not isinstance(sections, dict):
        raise DigestError(f"weekly digest {week} has no sections")
    missing = [name for name in ("market_news", "watchlist") if not sections.get(name)]
    if missing:
        raise DigestError(
            f"weekly digest {week} is missing core sections: {', '.join(missing)}"
        )
    return digest


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _week_label(digest: Dict[str, Any]) -> str:
    day = date.fromisoformat(str(digest["week_start"]))
    return f"Week of {day:%b} {day.day}, {day:%Y}"


def build_subject(digest: Dict[str, Any]) -> str:
    return f"{SUBJECT_PREFIX}Weekly Digest: {str(digest.get('headline') or '').strip()}"


def _pct(value: Any, suffix: str = "") -> str:
    if not isinstance(value, (int, float)):
        return ""
    color = UP if value > 0 else (DOWN if value < 0 else INK)
    return f'<span style="color:{color};font-weight:700;white-space:nowrap;">{value:+.1f}%{esc(suffix)}</span>'


def _tone_color(tone: Any) -> str:
    return {"up": UP, "down": DOWN}.get(str(tone or "").lower(), INK)


def _paragraphs(text: Any) -> str:
    parts = [p.strip() for p in str(text or "").split("\n\n") if p.strip()]
    return "".join(
        f'<p style="margin:0 0 10px 0;font-size:14px;line-height:1.6;color:#334155;">{esc(p)}</p>'
        for p in parts
    )


def _section(title: str, body: str) -> str:
    return (
        f'<div style="margin:24px 0 0 0;">'
        f'<div style="font-size:17px;font-weight:800;color:{INK};margin:0 0 8px 0;">{esc(title)}</div>{body}</div>'
    )


def _stats_html(sections: Dict[str, Any]) -> str:
    chips = []
    for stat in sections.get("stats") or []:
        chips.append(
            f'<span style="display:inline-block;white-space:nowrap;border:1px solid {LINE};border-radius:8px;padding:6px 9px;'
            f'margin:0 6px 6px 0;font-size:12px;color:{MUTED};">{esc(stat.get("label"))} '
            f'<b style="color:{_tone_color(stat.get("tone"))};font-size:14px;">{esc(stat.get("value"))}</b></span>'
        )
    fg = sections.get("fear_greed") or {}
    if fg.get("value") is not None:
        prev = (
            f" (last week {fg.get('prev_week_value')})"
            if fg.get("prev_week_value") is not None
            else ""
        )
        chips.append(
            f'<span style="display:inline-block;white-space:nowrap;border:1px solid {LINE};border-radius:8px;padding:6px 9px;'
            f'margin:0 6px 6px 0;font-size:12px;color:{MUTED};">Fear &amp; Greed '
            f'<b style="color:{INK};font-size:14px;">{esc(fg.get("value"))} {esc(fg.get("label"))}</b>{esc(prev)}</span>'
        )
    return f'<div style="margin:14px 0 0 0;">{"".join(chips)}</div>' if chips else ""


def _news_html(items: List[Dict[str, Any]]) -> str:
    rows = ""
    for item in items:
        url = ((item.get("sources") or [{}])[0] or {}).get("url") or ""
        title = esc(item.get("title"))
        if url.startswith("https://"):
            title = f'<a href="{esc(url)}" style="color:{INK};text-decoration:none;">{title}</a>'
        rows += f"""
      <tr><td style="padding:10px 0;border-top:1px solid {LINE};">
        <div style="font-size:15px;line-height:1.35;font-weight:700;color:{INK};">{title}</div>
        <div style="font-size:12px;color:{FAINT};margin:2px 0 4px 0;">{esc(item.get("source"))}</div>
        <div style="font-size:14px;line-height:1.55;color:#334155;">{esc(item.get("note"))}</div>
      </td></tr>"""
    return _table(rows)


def _rotation_html(rotation: Dict[str, Any]) -> str:
    points = sorted(
        (p for p in rotation.get("points") or [] if isinstance(p, dict)),
        key=lambda p: (
            p.get("week_change_pct")
            if isinstance(p.get("week_change_pct"), (int, float))
            else -1e9
        ),
        reverse=True,
    )
    rows = ""
    benchmark = rotation.get("benchmark_change_pct")
    entries = [
        (
            p.get("short") or p.get("ticker"),
            p.get("ticker"),
            p.get("week_change_pct"),
            p.get("quadrant"),
        )
        for p in points
    ]
    if isinstance(benchmark, (int, float)):
        entries.append(("S&P 500", "SPY", benchmark, None))
        entries.sort(
            key=lambda e: e[2] if isinstance(e[2], (int, float)) else -1e9, reverse=True
        )
    for name, ticker, change, quadrant in entries:
        tag = (
            f' <span style="font-size:11px;color:{FAINT};">{esc(quadrant)}</span>'
            if quadrant
            else ""
        )
        rows += f"""
      <tr>
        <td style="padding:6px 0;border-top:1px solid {LINE};font-size:14px;color:{INK};">{esc(name)} <span style="font-size:11px;color:{FAINT};">{esc(ticker)}</span>{tag}</td>
        <td align="right" style="padding:6px 0;border-top:1px solid {LINE};font-size:14px;">{_pct(change)}</td>
      </tr>"""
    week_table = (
        f'<div style="font-size:12px;color:{MUTED};margin:6px 0 2px 0;">Week by sector</div>{_table(rows)}'
        if rows
        else ""
    )
    return _paragraphs(rotation.get("body")) + week_table


def _monthly_html(items: List[Dict[str, Any]]) -> str:
    rows = ""
    for item in items:
        rows += f"""
      <tr><td style="padding:10px 0;border-top:1px solid {LINE};">
        <div style="font-size:15px;font-weight:800;color:{INK};">{esc(item.get("ticker"))} &nbsp;{_pct(item.get("week_change_pct"), " this week")}</div>
        <div style="font-size:14px;line-height:1.55;color:#334155;margin-top:3px;">{esc(item.get("body"))}</div>
      </td></tr>"""
    return _table(rows)


def _watchlist_html(items: List[Dict[str, Any]]) -> str:
    rows = ""
    for item in items:
        score = item.get("score")
        score_html = f"{score:.2f}" if isinstance(score, (int, float)) else esc(score)
        rows += f"""
      <tr>
        <td style="padding:8px 0;border-top:1px solid {LINE};">
          <div style="font-size:14px;font-weight:800;color:{INK};">{esc(item.get("ticker"))} <span style="font-weight:400;color:{MUTED};font-size:13px;">{esc(item.get("name"))}</span></div>
          <div style="font-size:12px;color:{UP};font-weight:600;margin-top:1px;">{esc(item.get("reason"))}</div>
        </td>
        <td align="right" valign="top" style="padding:8px 0 8px 10px;border-top:1px solid {LINE};font-size:15px;font-weight:700;color:{INK};white-space:nowrap;">{score_html}<span style="font-size:11px;color:{FAINT};font-weight:600;"> GT</span></td>
      </tr>"""
    return _table(rows)


def _signals_html(items: List[Dict[str, Any]]) -> str:
    rows = ""
    for item in items:
        direction = str(item.get("direction") or "").upper()
        pill = (
            _pill(direction, UP, "#dcfce7")
            if direction == "LONG"
            else _pill(direction or "—", DOWN, "#fee2e2")
        )
        since = (
            f' <span style="font-size:12px;color:{FAINT};">since {esc(_short_day(item.get("signal_date")))}</span>'
            if item.get("signal_date")
            else ""
        )
        rows += f"""
      <tr>
        <td style="padding:7px 0;border-top:1px solid {LINE};font-size:14px;font-weight:800;color:{INK};">{esc(item.get("ticker"))} {pill}</td>
        <td align="right" style="padding:7px 0;border-top:1px solid {LINE};font-size:14px;">{_pct(item.get("gain_since_signal_pct"))}{since}</td>
      </tr>"""
    return _table(rows)


def _short_day(value: Any) -> str:
    try:
        day = date.fromisoformat(str(value))
    except ValueError:
        return str(value or "")
    return f"{day:%b} {day.day}"


def _earnings_html(items: List[Dict[str, Any]]) -> str:
    rows = ""
    for item in items:
        try:
            day = date.fromisoformat(str(item.get("date")))
            label = f"{day:%a} {day:%b} {day.day}"
        except ValueError:
            label = str(item.get("date") or "")
        symbols = [str(s) for s in item.get("symbols") or []]
        extra = int(item.get("total") or len(symbols)) - len(symbols)
        text = ", ".join(symbols) + (f" +{extra} more" if extra > 0 else "")
        rows += f"""
      <tr>
        <td style="padding:6px 12px 6px 0;border-top:1px solid {LINE};font-size:13px;color:{MUTED};white-space:nowrap;width:1%;">{esc(label)}</td>
        <td style="padding:6px 0;border-top:1px solid {LINE};font-size:14px;font-weight:700;color:{INK};">{esc(text) or "—"}</td>
      </tr>"""
    return _table(rows)


def build_html(digest: Dict[str, Any], banner: str | None = None) -> str:
    s = digest.get("sections") or {}
    blocks = []
    if s.get("market_news"):
        blocks.append(
            _section("The news that moved the market", _news_html(s["market_news"]))
        )
    if s.get("rotation"):
        blocks.append(_section("Sector rotation", _rotation_html(s["rotation"])))
    if s.get("monthly"):
        blocks.append(
            _section("Portfolio holdings this week", _monthly_html(s["monthly"]))
        )
    if s.get("watchlist"):
        blocks.append(
            _section("This week's watchlist", _watchlist_html(s["watchlist"]))
        )
    if s.get("tradingview"):
        blocks.append(_section("Signal highlights", _signals_html(s["tradingview"])))
    if s.get("earnings"):
        blocks.append(_section("Earnings next week", _earnings_html(s["earnings"])))
    banner_html = ""
    if banner:
        banner_html = (
            f'<div style="background:#fffbeb;border:1px solid #fde68a;border-radius:8px;padding:9px 11px;'
            f'font-size:13px;line-height:1.45;color:#92400e;margin:10px 0 0 0;">{esc(banner)}</div>'
        )
    summary = (s.get("summary") or {}).get("body") or ""
    return f"""<!doctype html>
<html>
  <head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(digest.get("headline"))}</title></head>
  <body style="margin:0;padding:0;background:#f1f5f4;font-family:{FONT};color:{INK};">
    <div style="display:none;max-height:0;overflow:hidden;opacity:0;color:transparent;">{esc(summary)}</div>
    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="background:#f1f5f4;">
      <tr><td align="center" style="padding:14px 8px;">
        <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="max-width:600px;background:#ffffff;border:1px solid {LINE};border-radius:14px;">
          <tr><td style="padding:20px 18px 20px 18px;">
            <div style="font-size:11px;font-weight:800;letter-spacing:.1em;text-transform:uppercase;color:{BRAND};">Quant GT Weekly Digest<span style="color:{FAINT};font-weight:600;letter-spacing:0;text-transform:none;"> · {esc(_week_label(digest))}</span></div>
            <h1 style="font-size:23px;line-height:1.25;margin:6px 0 8px 0;color:{INK};">{esc(digest.get("headline"))}</h1>
            <div style="font-size:15px;line-height:1.55;color:#334155;">{esc(summary)}</div>
            {banner_html}
            {_stats_html(s)}
            {"".join(blocks)}
            <div style="margin:24px 0 0 0;padding:12px 0 0 0;border-top:1px solid {LINE};font-size:12px;line-height:1.55;color:{MUTED};">
              Quant GT's weekly digest for members, {esc(_week_label(digest).lower())}. For information only, not investment advice.
            </div>
          </td></tr>
        </table>
      </td></tr>
    </table>
  </body>
</html>"""


def build_text(digest: Dict[str, Any], banner: str | None = None) -> str:
    s = digest.get("sections") or {}
    lines = [
        f"Quant GT Weekly Digest — {_week_label(digest)}",
        "",
        str(digest.get("headline") or ""),
    ]
    summary = (s.get("summary") or {}).get("body")
    if summary:
        lines += [summary]
    if banner:
        lines += ["", f"Note: {banner}"]
    stats = [f"{x.get('label')} {x.get('value')}" for x in s.get("stats") or []]
    fg = s.get("fear_greed") or {}
    if fg.get("value") is not None:
        stats.append(f"Fear & Greed {fg.get('value')} {fg.get('label') or ''}".strip())
    if stats:
        lines += ["", " · ".join(stats)]
    if s.get("market_news"):
        lines += ["", "THE NEWS"]
        for item in s["market_news"]:
            lines += [
                f"- {item.get('title')} ({item.get('source')})",
                f"  {item.get('note') or ''}",
            ]
    if s.get("rotation"):
        lines += ["", "SECTOR ROTATION", str(s["rotation"].get("body") or "")]
    if s.get("monthly"):
        lines += ["", "PORTFOLIO HOLDINGS THIS WEEK"]
        for item in s["monthly"]:
            change = item.get("week_change_pct")
            change_text = (
                f" ({change:+.1f}% this week)"
                if isinstance(change, (int, float))
                else ""
            )
            lines += [f"- {item.get('ticker')}{change_text}: {item.get('body') or ''}"]
    if s.get("watchlist"):
        lines += ["", "THIS WEEK'S WATCHLIST"]
        for item in s["watchlist"]:
            score = item.get("score")
            score_text = (
                f"{score:.2f}" if isinstance(score, (int, float)) else str(score or "")
            )
            lines += [
                f"- {item.get('ticker')} {item.get('name') or ''} | GT {score_text} | {item.get('reason') or ''}"
            ]
    if s.get("tradingview"):
        lines += ["", "SIGNAL HIGHLIGHTS"]
        for item in s["tradingview"]:
            gain = item.get("gain_since_signal_pct")
            gain_text = f"{gain:+.1f}%" if isinstance(gain, (int, float)) else ""
            lines += [
                f"- {item.get('ticker')} {str(item.get('direction') or '').upper()} {gain_text} since {_short_day(item.get('signal_date'))}".rstrip()
            ]
    if s.get("earnings"):
        lines += ["", "EARNINGS NEXT WEEK"]
        for item in s["earnings"]:
            lines += [
                f"- {item.get('date')}: {', '.join(str(x) for x in item.get('symbols') or [])}"
            ]
    lines += ["", "For information only, not investment advice."]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
