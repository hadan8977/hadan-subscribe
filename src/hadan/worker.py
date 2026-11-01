from __future__ import annotations

import html
import json
import os
import random
import re
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

from .db import audit, enqueue, uid
from .security import action_token, decrypt, digest, mac, redact
from .service import Problem
from .transport import DeliveryError, MailTooLarge, Transport


def report_email_html(rendered_html, text, app_url):
    """Keep the report tables but avoid the content pattern DirectMail rejects.

    DirectMail's content anti-spam rejects the full deck (headline, change list
    and tables together). The tables alone pass, so the HTML keeps the tables
    and the plain-text part carries the full change list.
    """
    tables = re.findall(r"<table\b.*?</table>", rendered_html or "", re.S)
    body = "".join(tables) if tables else "<pre>" + html.escape(text) + "</pre>"
    return (
        "<html><body><h2>Quant GT update</h2>"
        + body
        + '<p><a href="'
        + html.escape(app_url)
        + '">Manage subscription</a></p></body></html>'
    )


def _picks_table(title, block):
    rows = []
    for line in block[1:]:
        match = re.match(r"^\s*(\d+)\.\s+([A-Za-z0-9.\-]+)\s+—\s+(.*)$", line)
        if not match:
            rows.append(
                '<tr><td colspan="3" style="padding:4px 12px">'
                + html.escape(line.strip())
                + "</td></tr>"
            )
            continue
        rank, symbol, rest = match.groups()
        if " | " in rest:
            company, metrics = rest.split(" | ", 1)
        else:
            company, metrics = rest, ""
        rows.append(
            '<tr><td style="padding:6px 12px;border-bottom:1px solid #e2e8f0;white-space:nowrap">'
            + html.escape(rank + ". " + symbol)
            + '</td><td style="padding:6px 12px;border-bottom:1px solid #e2e8f0">'
            + html.escape(company)
            + '</td><td style="padding:6px 12px;border-bottom:1px solid #e2e8f0">'
            + html.escape(metrics)
            + "</td></tr>"
        )
    return (
        '<table style="width:100%;border-collapse:collapse;font-family:Arial,sans-serif;font-size:14px;margin:0 0 18px 0">'
        '<tr><th colspan="3" style="text-align:left;padding:12px;background:#eef4f3;border-bottom:2px solid #0f172a;font-size:17px">'
        + html.escape(title)
        + '</th></tr><tr><td colspan="3" style="padding:10px 12px;color:#475569">'
        + html.escape(block[0])
        + '</td></tr><tr><th style="text-align:left;padding:6px 12px;border-bottom:1px solid #cbd5e1"># / Symbol</th>'
        '<th style="text-align:left;padding:6px 12px;border-bottom:1px solid #cbd5e1">Company</th>'
        '<th style="text-align:left;padding:6px 12px;border-bottom:1px solid #cbd5e1">Details</th></tr>'
        + "".join(rows)
        + "</table>"
    )


def _compact_row(line):
    match = re.match(r"^\s*(\d+)\.\s+([A-Za-z0-9.\-]+)\s+—\s+(.*)$", line)
    if not match:
        return None
    rank, symbol, rest = match.groups()
    company, metrics_text = (rest.split(" | ", 1) + [""])[:2] if " | " in rest else (rest, "")
    metrics = [item.strip() for item in metrics_text.split(" · ") if item.strip()]
    gt = next((item for item in metrics if item.startswith("GT ")), "")
    price = next((item for item in metrics if item.startswith("$")), "")
    ret = next((item for item in metrics if re.match(r"^[+-]\d+(\.\d+)?%$", item)), "")
    signal = next(
        (item for item in metrics if re.search(r"\b(Buy|Sell|Neutral)\b", item)), ""
    )
    used = {gt, price, ret, signal}
    note = " · ".join(item for item in metrics if item not in used)
    return {
        "rank": rank,
        "symbol": symbol,
        "company": company.strip(),
        "gt": gt,
        "price": price,
        "ret": ret,
        "signal": signal,
        "note": note,
    }


def _compact_changes(lines):
    blocks = []
    current = None
    for line in lines:
        if line.startswith("  "):
            if current is not None:
                current["details"].append(line.strip())
            continue
        current = {"title": line, "details": []}
        blocks.append(current)
    for block in blocks:
        match = re.match(
            r"^(.*?) — (.*?) \(previously (.*?)\)$", block["title"]
        )
        if match:
            section, date, previous = match.groups()
            block["label"] = f"{section} · {date} (was {previous})"
        else:
            block["label"] = block["title"]
        added, removed, gt, other = [], [], "", []
        for detail in block["details"]:
            add = re.match(r"^\+\s+Added\s+(\S+)", detail)
            remove = re.match(r"^-\s+Removed\s+(\S+)", detail)
            rerated = re.match(r"^\*\s+GT Score re-rated:\s*(.*)$", detail)
            if add:
                added.append(add.group(1))
            elif remove:
                removed.append(remove.group(1))
            elif rerated:
                gt = rerated.group(1)
            else:
                other.append(detail)
        block.update(added=added, removed=removed, gt=gt, other=other)
    return blocks


def _compact_change_label(block):
    match = re.match(r"^(.*?) \(was (.*?)\)$", block["label"])
    if match:
        return match.group(1), match.group(2)
    return block["label"], ""


def _compact_change_lines(block):
    lines = []
    if block["added"]:
        lines.append(("+ Added", "#15803d", ", ".join(block["added"])))
    if block["removed"]:
        lines.append(("- Removed", "#b91c1c", ", ".join(block["removed"])))
    if block["gt"]:
        lines.append(("GT re-rated", "#64748b", block["gt"]))
    if not lines and block["other"]:
        lines.append(("", "#334155", "; ".join(block["other"])))
    return lines


def _compact_section_title(title, block):
    match = re.match(r"^(.*?) — (.*?) · (\d+ stocks)$", block[0])
    if match:
        return f"{title} · {match.group(3)} · {match.group(2)}"
    return title


def _signal_color(signal):
    if "Buy" in signal:
        return "#15803d"
    if "Sell" in signal:
        return "#b91c1c"
    return "#64748b"


def _compact_holdings(title, block, weekly, added=()):
    rows = [row for row in (_compact_row(line) for line in block[1:]) if row]
    added_symbols = {item.upper() for item in added}
    if weekly:
        columns = 4
        headers = [
            ("Stock", "18%", "left"),
            ("GT", "12%", "left"),
            ("Signal", "34%", "left"),
            ("Price", "36%", "right"),
        ]
    else:
        columns = 5
        headers = [
            ("Stock", "18%", "left"),
            ("GT", "12%", "left"),
            ("Signal", "34%", "left"),
            ("Return", "17%", "right"),
            ("Price", "19%", "right"),
        ]
    out = [
        '<table width="100%" cellspacing="0" cellpadding="0" style="table-layout:fixed;border-collapse:collapse;font-family:Arial,sans-serif;font-size:12.5px;line-height:1.45;margin:0 0 16px 0">',
        '<tr><td colspan="' + str(columns) + '" style="padding:14px 0 6px 0;border-bottom:2px solid #1f8a70;font-size:11px;font-weight:800;letter-spacing:.06em;text-transform:uppercase;color:#1f8a70">'
        + html.escape(_compact_section_title(title, block))
        + "</td></tr>",
        '<tr>'
        + "".join(
            (
                '<th colspan="2" style="width:'
                + width
                + ";text-align:"
                + align
                + ';padding:5px 0;background:#f8fafc;color:#64748b;font-size:10px;font-weight:700;letter-spacing:.04em;text-transform:uppercase">'
                + name
                + "</th>"
            )
            if weekly and name == "Price"
            else (
                '<th style="width:'
                + width
                + ";text-align:"
                + align
                + ';padding:5px 0;background:#f8fafc;color:#64748b;font-size:10px;font-weight:700;letter-spacing:.04em;text-transform:uppercase">'
                + name
                + "</th>"
            )
            for name, width, align in headers
        )
        + "</tr>",
    ]
    for row in rows:
        gt = row["gt"].removeprefix("GT ") if row["gt"] else ""
        cells = [
            '<td style="padding:6px 0;border-bottom:1px solid #eef2f7;font-weight:800">'
            + (
                '<span style="color:#15803d">'
                + html.escape(row["symbol"])
                + '</span> <span style="display:inline-block;font-size:9px;font-weight:700;color:#15803d;background:#dcfce7;border-radius:4px;padding:1px 4px;margin-left:3px;">NEW</span>'
                if row["symbol"].upper() in added_symbols
                else html.escape(row["symbol"])
            )
            + "</td>",
            '<td style="padding:6px 0;border-bottom:1px solid #eef2f7;font-weight:800;color:#0f172a">'
            + html.escape(gt)
            + "</td>",
            '<td style="padding:6px 0;border-bottom:1px solid #eef2f7;font-weight:400;color:#64748b">'
            + html.escape(row["signal"])
            + "</td>",
        ]
        if weekly:
            cells.append(
                '<td colspan="2" style="padding:6px 0;border-bottom:1px solid #eef2f7;text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums;color:#334155">'
                + html.escape(row["price"])
                + "</td>"
            )
        else:
            ret = row["ret"]
            color = "#15803d" if ret.startswith("+") else "#b91c1c"
            cells.append(
                '<td style="padding:6px 0;border-bottom:1px solid #eef2f7;text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums;font-weight:700;color:'
                + color
                + '">'
                + html.escape(ret)
                + "</td>"
            )
            cells.append(
                '<td style="padding:6px 0;border-bottom:1px solid #eef2f7;text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums;color:#334155">'
                + html.escape(row["price"])
                + "</td>"
            )
        out.append("<tr>" + "".join(cells) + "</tr>")
    out.append("</table>")
    return "".join(out)


def _compact_changes_html(changes):
    blocks = _compact_changes(changes)
    rows = []
    for block in blocks:
        main, previous = _compact_change_label(block)
        head = '<div style="font-size:13px;font-weight:700;color:#0f172a">' + html.escape(main)
        if previous:
            head += (
                ' <span style="font-weight:400;color:#64748b">(was '
                + html.escape(previous)
                + ")</span>"
            )
        head += "</div>"
        lines = []
        for label, color, value in _compact_change_lines(block):
            if label:
                lines.append(
                    '<div style="font-size:12.5px;line-height:1.65"><span style="color:'
                    + color
                    + ';font-weight:700">'
                    + html.escape(label)
                    + '</span> <span style="color:#334155">'
                    + html.escape(value)
                    + "</span></div>"
                )
            else:
                lines.append(
                    '<div style="font-size:12.5px;line-height:1.65;color:#334155">'
                    + html.escape(value)
                    + "</div>"
                )
        rows.append(
            '<div style="margin:0 0 10px 0">' + head + "".join(lines) + "</div>"
        )
    return (
        '<table width="100%" cellspacing="0" cellpadding="0" style="border-collapse:collapse;background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;margin:0 0 16px 0">'
        '<tr><td style="padding:12px 12px 6px 12px;font-size:15px;font-weight:800">What changed</td></tr>'
        '<tr><td style="padding:0 12px 12px 12px">'
        + "".join(rows)
        + "</td></tr></table>"
    )


def _compact_added(changes):
    monthly, weekly = set(), set()
    for block in _compact_changes(changes):
        if block["label"].startswith("Portfolio"):
            monthly.update(block["added"])
        elif block["label"].startswith("Weekly Watchlist"):
            weekly.update(block["added"])
    return monthly, weekly


def _compact_text(changes, monthly, weekly, footer, app_url):
    monthly_added, weekly_added = _compact_added(changes)
    lines = ["Quant GT Picks Update", "", "What changed"]
    for block in _compact_changes(changes):
        main, previous = _compact_change_label(block)
        lines.append(main + (f" (was {previous})" if previous else ""))
        for label, _color, value in _compact_change_lines(block):
            lines.append((label + ": " if label else "") + value)
    lines.extend(["", _compact_section_title("Monthly Picks", monthly)])
    for line in monthly[1:]:
        row = _compact_row(line)
        if row:
            gt = row["gt"].removeprefix("GT ") if row["gt"] else ""
            lines.append(
                " | ".join(
                    item
                    for item in (
                        row["symbol"] + (" (NEW)" if row["symbol"] in monthly_added else ""),
                        gt,
                        row["signal"],
                        row["ret"],
                        row["price"],
                    )
                    if item
                )
            )
    lines.extend(["", _compact_section_title("Weekly Picks", weekly)])
    for line in weekly[1:]:
        row = _compact_row(line)
        if row:
            gt = row["gt"].removeprefix("GT ") if row["gt"] else ""
            lines.append(
                " | ".join(
                    item
                    for item in (
                        row["symbol"] + (" (NEW)" if row["symbol"] in weekly_added else ""),
                        gt,
                        row["signal"],
                        row["price"],
                    )
                    if item
                )
            )
    lines.extend(["", *footer, "", "Manage subscription: " + app_url])
    return "\n".join(lines)


def _compact_html(changes, monthly, weekly, footer, app_url):
    monthly_added, weekly_added = _compact_added(changes)
    footer_html = "".join(
        "<div>" + html.escape(line) + "</div>" for line in footer
    )
    return (
        '<html><body style="margin:0;padding:0;background:#f1f5f4;font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',Roboto,Helvetica,Arial,sans-serif;color:#0f172a;">'
        '<table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="background:#f1f5f4;">'
        '<tr><td align="center" style="padding:14px 8px;">'
        '<table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="max-width:600px;background:#ffffff;border:1px solid #dbe3e3;border-radius:14px;">'
        '<tr><td style="padding:18px 16px 16px 16px;">'
        '<div style="font-size:11px;font-weight:800;letter-spacing:.1em;text-transform:uppercase;color:#1f8a70;">Quant GT Picks Update</div>'
        + _compact_changes_html(changes)
        + _compact_holdings("Monthly Picks", monthly, False, monthly_added)
        + _compact_holdings("Weekly Picks", weekly, True, weekly_added)
        + '<div style="margin:14px 0 0 0;padding-top:10px;border-top:1px solid #e2e8f0;font-size:11px;line-height:1.5;color:#64748b;">'
        + footer_html
        + "</div>"
        + '<p style="margin:10px 0 0 0;font-size:12px;"><a href="'
        + html.escape(app_url)
        + '" style="color:#1f8a70;">Manage subscription</a></p>'
        "</td></tr></table></td></tr></table></body></html>"
    )


def report_picks_payload(body, app_url):
    """Split a picks report into What changed, Monthly Picks and Weekly Picks."""
    lines = body.splitlines()

    def find(predicate, start=0):
        for index in range(start, len(lines)):
            if predicate(lines[index]):
                return index
        return -1

    changes_at = find(lambda line: line.strip() == "Changes:")
    if changes_at < 0:
        return None
    monthly_at = find(
        lambda line: re.match(r"^Portfolio — .* · \d+ stocks$", line.strip()),
        changes_at + 1,
    )
    if monthly_at < 0:
        return None
    weekly_at = find(
        lambda line: re.match(
            r"^Weekly Watchlist — .* · \d+ stocks$", line.strip()
        ),
        monthly_at + 1,
    )
    if weekly_at < 0:
        return None
    footer_at = find(lambda line: line.startswith("Full details:"), weekly_at + 1)
    if footer_at < 0:
        return None
    changes = [line for line in lines[changes_at + 1 : monthly_at] if line.strip()]
    monthly = [line for line in lines[monthly_at:weekly_at] if line.strip()]
    weekly = [line for line in lines[weekly_at:footer_at] if line.strip()]
    footer = [line for line in lines[footer_at:] if line.strip()]

    changes_rows = []
    for line in changes:
        if line.startswith("  "):
            changes_rows.append(
                '<tr><td colspan="3" style="padding:4px 12px">'
                + html.escape(line.strip())
                + "</td></tr>"
            )
        else:
            changes_rows.append(
                '<tr><td colspan="3" style="padding:10px 12px;font-weight:700;background:#f8fafc">'
                + html.escape(line)
                + "</td></tr>"
            )
    changes_table = (
        '<table style="width:100%;border-collapse:collapse;font-family:Arial,sans-serif;font-size:14px;margin:0 0 18px 0">'
        '<tr><th colspan="3" style="text-align:left;padding:12px;background:#eef4f3;border-bottom:2px solid #0f172a;font-size:17px">What changed</th></tr>'
        + "".join(changes_rows)
        + "</table>"
    )
    footer_table = (
        '<table style="width:100%;border-collapse:collapse;font-family:Arial,sans-serif;font-size:12px;color:#64748b">'
        + "".join(
            '<tr><td style="padding:2px 12px">' + html.escape(line) + "</td></tr>"
            for line in footer
        )
        + "</table>"
    )
    rich = (
        "<html><body>"
        + changes_table
        + _picks_table("Monthly Picks", monthly)
        + _picks_table("Weekly Picks", weekly)
        + footer_table
        + '<p style="font-family:Arial,sans-serif;font-size:13px"><a href="'
        + html.escape(app_url)
        + '">Manage subscription</a></p></body></html>'
    )
    text = (
        "What changed\n"
        + "\n".join(changes)
        + "\n\nMonthly Picks\n"
        + "\n".join(monthly)
        + "\n\nWeekly Picks\n"
        + "\n".join(weekly)
        + "\n\n"
        + "\n".join(footer)
        + "\n\nManage subscription: "
        + app_url
    )
    compact_text = _compact_text(changes, monthly, weekly, footer, app_url)
    compact_html = _compact_html(changes, monthly, weekly, footer, app_url)
    return text, rich, compact_text, compact_html


def report_legacy_html(rendered_html, app_url):
    """Keep the previous report's visual style and split it into three sections."""
    markup = rendered_html or ""
    if "<table" not in markup:
        return None
    markup = re.sub(
        r'<div style="display:none;.*?</div>', "", markup, count=1, flags=re.S
    )
    markup = re.sub(r"<h1[^>]*>.*?</h1>", "", markup, flags=re.S)
    markup = re.sub(
        r'<div style="font-size:14px;line-height:1\.5;color:#334155;">.*?</div>',
        "",
        markup,
        flags=re.S,
    )
    markup = re.sub(r"Portfolio · (\d+ stocks)", r"Monthly Picks · \1", markup)
    markup = re.sub(
        r"Weekly Watchlist · (\d+ stocks)", r"Weekly Picks · \1", markup
    )
    link = (
        '<div style="margin:16px 0 0 0;font-size:13px;">'
        '<a href="'
        + html.escape(app_url)
        + '" style="color:#1f8a70;">Manage subscription</a></div>'
    )
    if app_url not in markup:
        pattern = re.compile(
            r'(<div style="margin:22px 0 0 0;padding:12px 0 0 0;border-top:1px solid [^"]*">.*?</div>)',
            re.S,
        )
        if pattern.search(markup):
            markup = pattern.sub(lambda match: match.group(1) + link, markup, count=1)
        else:
            markup = markup.replace("</body>", link + "</body>")
    return markup


def report_subject(delivery):
    source = delivery["source_key"] or ""
    if delivery["kind"] == "withdrawn":
        return "Quant GT · Report withdrawn"
    if source.startswith("picks:"):
        return "Quant GT · Portfolio update"
    if source.startswith("digest:"):
        return "Quant GT · Weekly Digest"
    return "Quant GT · Update"


def report_email_payload(
    service, user_id, content_id, number, base_url, layout="legacy"
):
    """Render one report exactly as subscribers receive it."""
    content = service.content(user_id, content_id, number)
    app_url = base_url.rstrip("/") + "/app"
    picks = report_picks_payload(content["body"], app_url)
    if picks:
        text, plain_html, compact_text, compact_html = picks
        if layout == "compact":
            text, rich = compact_text, compact_html
        else:
            rich = report_legacy_html(content["html"], app_url) or plain_html
    else:
        text = content["body"] + "\n\nManage subscription: " + app_url
        rich = report_email_html(content["html"], content["body"], app_url)
    files = [
        service.attachment(user_id, item["id"]) for item in content["attachments"]
    ]
    return text, rich, files


class Worker:
    def __init__(self, service, transport=None, queue="main"):
        self.s = service
        self.db = service.db
        self.cfg = service.settings
        self.transport = transport or Transport(self.cfg)
        self.queue = queue
        self.stopping = False
        self.last_heartbeat = 0.0

    def clean(self, value):
        text = str(value)
        for value in (
            self.cfg.secret,
            self.cfg.deepseek_key,
            self.cfg.smtp_password,
            self.cfg.stripe_key,
            self.cfg.stripe_webhook_secret,
            self.cfg.xorpay_secret,
        ):
            if value:
                text = text.replace(value, "[redacted]")
        return redact(text)

    def claim(self):
        now = time.time()
        with self.db.transaction() as db:
            paused = db.execute(
                "SELECT value FROM meta WHERE key='delivery_paused'"
            ).fetchone()
            # Migration/restore pause freezes every side effect, including account/login mail,
            # source publication, billing mutations and AI jobs, without spending retry attempts.
            if paused and paused["value"] == "1":
                return None
            stale = db.execute(
                "SELECT * FROM jobs WHERE state='running' AND lease_until<? AND queue=?",
                (now, self.queue),
            ).fetchall()
            for row in stale:
                payload = json.loads(row["payload"])
                if (
                    row["kind"] in ("deliver", "account_mail")
                    and "delivery_id" in payload
                    and self.cfg.mail_backend == "smtp"
                ):
                    d = db.execute(
                        "SELECT * FROM deliveries WHERE id=?", (payload["delivery_id"],)
                    ).fetchone()
                    if d and d["channel"] == "email" and d["state"] == "sending":
                        db.execute(
                            "UPDATE deliveries SET state='uncertain',error='Worker interrupted during SMTP' WHERE id=?",
                            (d["id"],),
                        )
                        db.execute(
                            "UPDATE jobs SET state='uncertain' WHERE id=?", (row["id"],)
                        )
                        continue
                db.execute(
                    "UPDATE jobs SET state='queued',lease_token=NULL,lease_until=NULL WHERE id=?",
                    (row["id"],),
                )
            # Source browsers and isolated AI verification share the small host's
            # heavy-work slot. The DB transaction makes cross-worker admission atomic.
            if self.queue in ("source", "ops") and db.execute(
                "SELECT 1 FROM jobs WHERE queue IN ('source','ops') AND state='running' AND lease_until>=? LIMIT 1", (now,)
            ).fetchone():
                return None
            row = db.execute(
                "SELECT * FROM jobs WHERE state='queued' AND queue=? AND available_at<=? ORDER BY CASE kind WHEN 'login_mail' THEN 0 ELSE 1 END,available_at LIMIT 1",
                (self.queue, now),
            ).fetchone()
            if not row:
                return None
            lease = uid()
            db.execute(
                "UPDATE jobs SET state='running',attempts=attempts+1,lease_until=?,lease_token=? WHERE id=?",
                (now + self.cfg.lease_seconds, lease, row["id"]),
            )
            result = dict(row)
            result["lease_token"] = lease
            result["attempts"] += 1
            return result

    def incident(self, kind, message, details=""):
        message = self.clean(message)
        details = self.clean(details)
        now = time.time()
        fingerprint = digest(kind + ":" + message)
        with self.db.transaction() as db:
            existing = db.execute(
                "SELECT * FROM incidents WHERE fingerprint=?", (fingerprint,)
            ).fetchone()
            if existing:
                if existing["state"] == "resolved":
                    # A later recurrence is a new bounded repair episode. Preserve
                    # the old incident/audit instead of spending its retry forever.
                    db.execute(
                        "UPDATE incidents SET fingerprint=? WHERE id=?",
                        (digest(fingerprint + ":" + existing["id"]), existing["id"]),
                    )
                else:
                    db.execute(
                        "UPDATE incidents SET count=count+1,state='open',updated_at=?,details=? WHERE id=?",
                        (now, redact(details), existing["id"]),
                    )
                    return existing["id"]
            iid = uid()
            db.execute(
                "INSERT INTO incidents(id,fingerprint,kind,summary,details,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (iid, fingerprint, kind, redact(message), redact(details), now, now),
            )
            if self.cfg.ops_ai and kind not in ("diagnose", "propose_parser"):
                enqueue(
                    db,
                    "diagnose",
                    {"incident_id": iid},
                    "diagnose:"
                    + iid
                    + ":"
                    + datetime.now(timezone.utc).date().isoformat(),
                    queue="ops",
                )
            return iid

    def run_once(self):
        if time.monotonic() - self.last_heartbeat >= 30:
            self.db.execute(
                "INSERT INTO heartbeats VALUES(?,?,?) ON CONFLICT(name) DO UPDATE SET updated_at=excluded.updated_at,details=excluded.details",
                ("worker:" + self.queue, time.time(), "{}"),
            )
            self.last_heartbeat = time.monotonic()
        job = self.claim()
        if not job:
            return False
        try:
            self.dispatch(job)
        except Exception as exc:
            retry = not isinstance(exc, (Problem, ValueError))
            uncertain = False
            if isinstance(exc, DeliveryError):
                retry, uncertain = exc.retry, exc.uncertain
            dead = not retry or job["attempts"] >= self.cfg.max_attempts
            state = "uncertain" if uncertain else ("dead" if dead else "queued")
            delay = min(3600, 15 * 2 ** job["attempts"]) + random.uniform(0, 5)
            with self.db.transaction() as db:
                db.execute(
                    "UPDATE jobs SET state=?,last_error=?,available_at=?,lease_until=NULL WHERE id=? AND lease_token=?",
                    (
                        state,
                        self.clean(exc),
                        time.time() + delay,
                        job["id"],
                        job["lease_token"],
                    ),
                )
                payload = json.loads(job["payload"])
                if job["kind"] in (
                    "deliver",
                    "account_mail",
                ) and "delivery_id" in payload:
                    did = payload["delivery_id"]
                    db.execute(
                        "UPDATE deliveries SET state=?,error=?,updated_at=? WHERE id=?",
                        (
                            "uncertain"
                            if uncertain
                            else ("failed" if dead else "retrying"),
                            self.clean(exc),
                            time.time(),
                            did,
                        ),
                    )
                if job["kind"] == "mcp_event_deliver":
                    db.execute(
                        "UPDATE mcp_event_deliveries SET state=?,last_error=?,updated_at=? "
                        "WHERE subscription_id=? AND event_id=?",
                        (
                            "failed" if dead else "retrying",
                            self.clean(exc),
                            time.time(),
                            payload.get("subscription_id"),
                            payload.get("event_id"),
                        ),
                    )
            if dead or uncertain:
                self.incident(job["kind"], str(exc), "job_id=" + job["id"])
        else:
            self.db.execute(
                "UPDATE jobs SET state='complete',lease_until=NULL,last_error=NULL WHERE id=? AND lease_token=?",
                (job["id"], job["lease_token"]),
            )
            self.db.execute(
                "UPDATE incidents SET state='resolved',updated_at=? WHERE state='open' AND details=?",
                (time.time(), "job_id=" + job["id"]),
            )
        return True

    def dispatch(self, job):
        p = json.loads(job["payload"])
        kind = job["kind"]
        if kind == "login_mail":
            if self.db.one(
                "SELECT email FROM suppressions WHERE email=?", (p["email"],)
            ):
                return
            code = decrypt(self.cfg.secret, p["encrypted_code"])
            current = self.db.one(
                "SELECT * FROM otp WHERE email=? AND expires_at>?",
                (p["email"], time.time()),
            )
            if not current or current["digest"] != mac(
                self.cfg.secret, p["email"] + ":" + code
            ):
                return
            self.transport.mail(
                p["email"],
                "Quant · 登录验证码 / Sign-in code",
                f"你的验证码是 {code}，10 分钟内有效。请勿将验证码交给他人。\n\nYour Quant sign-in code is {code}. It expires in 10 minutes. Do not share it with anyone.",
                identity=job["id"],
            )
        elif kind == "account_mail":
            self.deliver(p["delivery_id"])
        elif kind == "deliver":
            self.deliver(p["delivery_id"])
        elif kind == "purchase_reconcile":
            from .commerce import Commerce
            result = Commerce(self.s).reconcile(p["order_id"])
            if result.get("needs_review"):
                self.incident("payment_review", "Paid order requires a signed payment callback", "order_id=" + p["order_id"])
        elif kind == "webhook_test":
            b = self.db.one(
                "SELECT * FROM bindings WHERE id=? AND state='pending'",
                (p["binding_id"],),
            )
            if b:
                self.transport.webhook(
                    b["endpoint"],
                    {
                        "type": "connection.test",
                        "event_id": "test:" + b["id"],
                        "next": "Use your bearer token to GET /api/agent/test and /api/agent/test-file, then POST /api/agent/test/complete",
                    },
                    mac(self.cfg.secret, "hook:" + b["id"]),
                    "test:" + b["id"],
                )
                self.db.execute(
                    "UPDATE bindings SET test_hook=1 WHERE id=? AND state='pending'",
                    (b["id"],),
                )
        elif kind == "mcp_event_deliver":
            from .mcp_events import MCPEvents

            MCPEvents(self.s).deliver(p["subscription_id"], p["event_id"])
        elif kind.startswith("source:"):
            child_env = os.environ.copy()
            child_env["HADAN_DATA_DIR"] = str(self.cfg.data_dir)
            cmd = [sys.executable, "-m", "hadan.sources.jobs", kind.split(":", 1)[1]]
            if kind == "source:activate_parser":
                cmd += [p["candidate"], p["sha256"]]
            proc = subprocess.Popen(
                cmd,
                env=child_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            try:
                out, err = proc.communicate(timeout=self.cfg.source_timeout)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.communicate()
                if kind in (
                    "source:picks",
                    "source:weekly_digest",
                    "source:health_site",
                ):
                    from .sources.jobs import rollback_parser

                    rollback_parser(self.s)
                raise DeliveryError("Source task exceeded time limit")
            if proc.returncode:
                raise DeliveryError(redact(err.decode(errors="replace")[-1500:]))
        elif kind in ("billing_cancel", "billing_reconcile"):
            from .billing import Billing

            getattr(
                Billing(self.s), "cancel" if kind == "billing_cancel" else "reconcile"
            )(**p)
        elif kind in ("diagnose", "propose_parser"):
            child_env = os.environ.copy()
            child_env["HADAN_DATA_DIR"] = str(self.cfg.data_dir)
            # Models and isolated verification cannot hold the ops lease forever.
            # stdout is file-backed; only a bounded, redacted error tail enters logs.
            with tempfile.TemporaryFile(mode="w+b") as log:
                proc = subprocess.Popen(
                    [sys.executable, "-m", "hadan.ops", kind, p["incident_id"]],
                    env=child_env,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                try:
                    proc.communicate(
                        timeout=min(self.cfg.ops_timeout, self.cfg.lease_seconds - 5)
                    )
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    proc.communicate()
                    raise DeliveryError("Maintenance task exceeded time limit")
                if proc.returncode:
                    log.seek(0, 2)
                    log.seek(max(0, log.tell() - 1500))
                    raise DeliveryError(
                        self.clean(log.read().decode(errors="replace")),
                        retry=proc.returncode != 2,
                    )
        elif kind == "admin_summary":
            self.admin_summary(job["id"])
        else:
            raise ValueError("Unknown job kind: " + kind)

    def deliver(self, did):
        paused = self.db.one("SELECT value FROM meta WHERE key='delivery_paused'")
        if paused and paused["value"] == "1":
            raise DeliveryError("Delivery paused after restore or by operator")
        d = self.db.one(
            "SELECT d.*,e.product_id,e.content_id,e.version_id,e.kind,e.summary,c.withdrawn,c.current_version,c.source_key,v.number FROM deliveries d JOIN events e ON e.id=d.event_id LEFT JOIN contents c ON c.id=e.content_id LEFT JOIN versions v ON v.id=e.version_id WHERE d.id=?",
            (did,),
        )
        if not d or d["state"] in ("accepted", "skipped", "uncertain"):
            return
        if d["kind"].startswith("account:"):
            return self.deliver_account(d)
        s = self.s.entitled(d["user_id"], d["product_id"])
        skip = not s or (d["withdrawn"] and d["kind"] != "withdrawn")
        if d["number"] and d["number"] != d["current_version"]:
            skip = True
        b = None
        if d["channel"] == "email":
            user = self.db.one("SELECT * FROM users WHERE id=?", (d["user_id"],))
            skip = skip or not s["email_enabled"] if s else True
            skip = skip or bool(
                self.db.one(
                    "SELECT email FROM suppressions WHERE email=?", (user["email"],)
                )
            )
        else:
            b = self.db.one(
                "SELECT * FROM bindings WHERE id=? AND user_id=? AND state='active'",
                (d["binding_id"], d["user_id"]),
            )
            skip = skip or not b or not s["agent_enabled"] if s else True
        if skip:
            self.db.execute(
                "UPDATE deliveries SET state='skipped',updated_at=? WHERE id=?",
                (time.time(), did),
            )
            return
        # Linearization point: authorization and transition are serialized with revocations.
        # Once this commits, the attempt is in flight; network I/O never holds the DB writer.
        with self.db.transaction() as db:
            fresh = db.execute(
                "SELECT * FROM subscriptions WHERE id=? AND status='active' AND (expires_at IS NULL OR expires_at>?)",
                (s["id"], time.time()),
            ).fetchone()
            allowed = bool(
                fresh
                and fresh[
                    "email_enabled" if d["channel"] == "email" else "agent_enabled"
                ]
            )
            if d["channel"] == "webhook":
                allowed = allowed and bool(
                    db.execute(
                        "SELECT id FROM bindings WHERE id=? AND state='active' AND user_id=?",
                        (d["binding_id"], d["user_id"]),
                    ).fetchone()
                )
            else:
                allowed = (
                    allowed
                    and not db.execute(
                        "SELECT email FROM suppressions WHERE email=?", (user["email"],)
                    ).fetchone()
                )
            content = db.execute(
                "SELECT withdrawn,current_version FROM contents WHERE id=?",
                (d["content_id"],),
            ).fetchone()
            allowed = allowed and bool(
                content
                and (not content["withdrawn"] or d["kind"] == "withdrawn")
                and (not d["number"] or d["number"] == content["current_version"])
            )
            if not allowed:
                db.execute(
                    "UPDATE deliveries SET state='skipped',updated_at=? WHERE id=?",
                    (time.time(), did),
                )
                return
            db.execute(
                "UPDATE deliveries SET state='sending',attempts=attempts+1,updated_at=? WHERE id=?",
                (time.time(), did),
            )
            s = dict(fresh)
        if d["channel"] == "webhook":
            files = []
            if d["version_id"]:
                files = self.db.all(
                    "SELECT id,name,mime,size,sha256 FROM attachments WHERE version_id=?",
                    (d["version_id"],),
                )
            payload = {
                "event_id": d["event_id"],
                "type": d["kind"],
                "summary": d["summary"],
                "content_id": d["content_id"],
                "version": d["number"],
                "attachments": files,
            }
            provider = self.transport.webhook(
                b["endpoint"],
                payload,
                mac(self.cfg.secret, "hook:" + b["id"]),
                d["event_id"],
            )
        else:
            unsubscribe = (
                self.cfg.base_url
                + "/unsubscribe/"
                + action_token(self.cfg.secret, s["id"])
            )
            read_url = self.cfg.base_url + "/read/" + d["content_id"]
            app_url = self.cfg.base_url + "/app"
            rich = None
            files = []
            if d["kind"] == "withdrawn":
                text = d["summary"] + "\n\nThis content has been withdrawn.\n\nManage subscription: " + app_url
            else:
                # Report emails always carry the full rendered report and every
                # attachment, matching the previous product's email format.
                text, rich, files = report_email_payload(
                    self.s,
                    d["user_id"],
                    d["content_id"],
                    d["number"],
                    self.cfg.base_url,
                    layout=os.getenv("HADAN_EMAIL_LAYOUT", "compact"),
                )
            subject = report_subject(d)
            try:
                provider = self.transport.mail(
                    user["email"],
                    subject,
                    text,
                    identity=did,
                    html=rich,
                    attachments=files,
                    unsubscribe=unsubscribe,
                    headers={
                        "X-Quant-Event-ID": d["event_id"],
                        "X-Quant-Content-ID": d["content_id"] or "",
                        "X-Quant-Version": str(d["number"] or ""),
                    },
                )
            except MailTooLarge:
                # No SMTP operation has occurred. Preserve the update and every
                # private attachment without repeatedly attempting an oversized MIME.
                provider = self.transport.mail(
                    user["email"],
                    subject,
                    d["summary"]
                    + "\n\nThis email exceeds the single-message size limit. Read the full report and download all attachments: "
                    + read_url,
                    identity=did,
                    unsubscribe=unsubscribe,
                    headers={
                        "X-Quant-Event-ID": d["event_id"],
                        "X-Quant-Content-ID": d["content_id"] or "",
                        "X-Quant-Version": str(d["number"] or ""),
                    },
                )
        self.db.execute(
            "UPDATE deliveries SET state='accepted',provider_id=?,error=NULL,updated_at=? WHERE id=?",
            (provider, time.time(), did),
        )

    def deliver_account(self, d):
        with self.db.transaction() as db:
            sub = db.execute(
                "SELECT * FROM subscriptions WHERE user_id=? AND product_id=?",
                (d["user_id"], d["product_id"]),
            ).fetchone()
            user = db.execute(
                "SELECT email FROM users WHERE id=?", (d["user_id"],)
            ).fetchone()
            binding = db.execute(
                "SELECT * FROM bindings WHERE id=? AND user_id=? AND state='active'",
                (d["binding_id"], d["user_id"]),
            ).fetchone()
            allowed = bool(
                sub
                and sub["email_enabled" if d["channel"] == "email" else "agent_enabled"]
            )
            if d["channel"] == "webhook":
                allowed = allowed and bool(binding)
            else:
                allowed = (
                    allowed
                    and not db.execute(
                        "SELECT email FROM suppressions WHERE email=?", (user["email"],)
                    ).fetchone()
                )
            db.execute(
                "UPDATE deliveries SET state=?,attempts=attempts+1,updated_at=? WHERE id=?",
                ("sending" if allowed else "skipped", time.time(), d["id"]),
            )
            if not allowed:
                return
        if d["channel"] == "email":
            title, _, body = d["summary"].partition("\n")
            provider = self.transport.mail(
                user["email"],
                title,
                body + "\n" + self.cfg.base_url + "/app",
                identity=d["id"],
            )
        else:
            provider = self.transport.webhook(
                binding["endpoint"],
                {
                    "event_id": d["event_id"],
                    "type": d["kind"],
                    "summary": d["summary"],
                    "content_id": None,
                    "attachments": [],
                },
                mac(self.cfg.secret, "hook:" + binding["id"]),
                d["event_id"],
            )
        self.db.execute(
            "UPDATE deliveries SET state='accepted',provider_id=?,updated_at=? WHERE id=?",
            (provider, time.time(), d["id"]),
        )

    def admin_summary(self, identity):
        counts = self.db.all(
            "SELECT state,COUNT(*) count FROM deliveries GROUP BY state"
        )
        incidents = self.db.all(
            "SELECT kind,summary,count FROM incidents WHERE state='open' LIMIT 15"
        )
        source = self.db.all(
            "SELECT key,updated_at FROM source_state WHERE key IN ('job:official_mail','job:picks','job:weekly_digest','picks:last_success')"
        )
        expiring = self.db.all(
            "SELECT u.email,s.product_id,s.expires_at FROM subscriptions s JOIN users u ON u.id=s.user_id WHERE s.expires_at BETWEEN ? AND ? ORDER BY s.expires_at",
            (time.time(), time.time() + 14 * 86400),
        )
        from .health import check

        body = (
            "Hadan 运行日报\n"
            + json.dumps(
                {
                    "health": check(self.s),
                    "deliveries": counts,
                    "incidents": incidents,
                    "source_last_success": source,
                    "expiring_within_14_days": expiring,
                    "expiring_within_7_days": sum(
                        x["expires_at"] < time.time() + 7 * 86400 for x in expiring
                    ),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
            + self.cfg.base_url
            + "/admin"
        )
        for email in self.cfg.admin_emails:
            self.transport.mail(
                email, "Quant · 运行日报", body, identity=identity + digest(email)[:12]
            )

    def run(self):
        def stop(*_):
            self.stopping = True

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        next_schedule = 0
        while not self.stopping:
            if self.queue == "main" and time.time() >= next_schedule:
                from .schedule import tick

                tick(self.s)
                next_schedule = time.time() + 30
            if not self.run_once():
                time.sleep(1)
