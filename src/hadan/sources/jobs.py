from __future__ import annotations

import hashlib
import imaplib
import json
import os
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path

from hadan.config import Settings
from hadan.db import enqueue
from hadan.security import digest, redact
from hadan.service import Service


def state(s, key, default=None):
    row = s.db.one("SELECT value FROM source_state WHERE key=?", (key,))
    return json.loads(row["value"]) if row else default


def save(s, key, value):
    s.db.execute(
        "INSERT INTO source_state VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
        (key, json.dumps(value, ensure_ascii=False), time.time()),
    )


@contextmanager
def browser_session():
    from playwright.sync_api import sync_playwright
    from . import quantgt_browser as q

    email, password = os.getenv("QUANTGT_EMAIL"), os.getenv("QUANTGT_PASSWORD")
    if not email or not password:
        raise ValueError("Quant GT source credentials are not configured")
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        raise ValueError("Source browser must run as an unprivileged service user")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, chromium_sandbox=True,
                                    executable_path=os.getenv("HADAN_CHROMIUM_EXECUTABLE") or None)
        try:
            context = browser.new_context(
                viewport={"width": 1440, "height": 1200}, locale="en-US"
            )
            page = context.new_page()
            q.login(page, email, password)
            yield page
        finally:
            browser.close()


def capture_picks(page, *, capture_images=True):
    from . import quantgt_browser as q
    from hadan.compat.validation import validate_member_picks_data

    result = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "source": q.BASE,
        "auth_verified": True,
    }
    screenshots = []
    for mode, path in (("monthly", "quantgt-picks"), ("weekly", "weekly-picks")):
        if page.url != q.BASE + "/" + path:
            page.goto(q.BASE + "/" + path, wait_until="domcontentloaded", timeout=45000)
        q.wait_for_picks_content(page)
        q.assert_authenticated_page(page, mode)
        rows = q.wait_for_parsable_picks_rows(page, mode)
        pick_date = q.extract_pick_date(
            q.clean_text(page.locator("main").inner_text()), mode
        )
        kind = (
            "watchlist"
            if mode == "weekly"
            and (
                q.is_watchlist_page(page)
                or any(x.get("source_kind") == "watchlist" for x in rows)
            )
            else mode
        )
        if kind == "watchlist":
            api = q.fetch_watchlist_api_rows(page)
            rows = q.merge_watchlist_api_scores(rows, api)
            rows = q.attach_digest_reasons(page, rows, api)
            rows = q.expand_watchlist_and_attach_details(page, rows)
        else:
            rows = q.expand_and_attach_details(page, rows, mode)
        result[mode] = {
            "page": page.url,
            "pick_date": pick_date,
            "kind": kind,
            "rows": rows,
        }
        if capture_images:
            q.dismiss_screenshot_overlays(page)
            q._assert_screenshot_symbols_match(page, mode, rows)
            screenshots.append(
                {
                    "name": ("portfolio" if mode == "monthly" else "watchlist")
                    + ".png",
                    "data": page.screenshot(full_page=True),
                }
            )
    validate_member_picks_data(result)
    return result, screenshots


def accept_picks(s, data, screenshots=(), *, baseline=False):
    from hadan.compat.validation import validate_member_picks_data
    from hadan.compat.diff import compare
    from hadan.compat import picks_excel, picks_email

    validate_member_picks_data(data)
    previous = state(s, "picks:accepted")
    change = compare(previous, data) if previous else None
    changed = bool(change and change.get("changed"))
    if previous and not changed:
        # Observation freshness advances; publication reference stays at the last published state.
        save(s, "picks:last_success", time.time())
        return {"unchanged": True}
    identity = digest(
        json.dumps(
            [
                previous.get("_publication_id") if previous else None,
                data["monthly"].get("pick_date"),
                data["weekly"].get("pick_date"),
                change or "initial",
            ],
            sort_keys=True,
        )
    )
    existing = s.db.one(
        "SELECT id,current_version FROM contents WHERE product_id='quantgt' AND source_key=?",
        ("picks:" + identity,),
    )
    if existing:
        recovered = dict(data)
        recovered["_publication_id"] = existing["id"]
        save(s, "picks:accepted", recovered)
        save(s, "picks:last_success", time.time())
        return {"id": existing["id"], "unchanged": True, "recovered": True}
    with tempfile.TemporaryDirectory(prefix="hadan-report-") as directory:
        path = Path(directory) / "quantgt_picks_report.xlsx"
        picks_excel.write_report(path, data, diff=change, previous=previous)
        files = [{"name": path.name, "data": path.read_bytes()}, *screenshots]
        body = picks_email.build_text(data, diff=change, previous=previous)
        rich = picks_email.build_html(data, diff=change, previous=previous)
        title = picks_email.build_subject(change, data)
        # Stable semantic identity excludes capture timestamps and market-noise fields.
        published = s.publish(
            "quantgt",
            "picks:" + identity,
            title,
            body,
            html=rich,
            files=files,
            baseline=baseline or previous is None,
        )
    accepted = dict(data)
    accepted["_publication_id"] = published["id"]
    save(s, "picks:accepted", accepted)
    save(s, "picks:last_success", time.time())
    return published


def picks(s):
    from . import quantgt_browser as q
    from hadan.compat.diff import compare

    with browser_session() as page:
        data, shots = capture_picks(page, capture_images=False)
        previous = state(s, "picks:accepted")
        if not previous or compare(previous, data)["changed"]:
            for mode, path in (
                ("monthly", "quantgt-picks"),
                ("weekly", "weekly-picks"),
            ):
                page.goto(
                    q.BASE + "/" + path, wait_until="domcontentloaded", timeout=45000
                )
                q.wait_for_parsable_picks_rows(page, mode)
                q.assert_authenticated_page(page, mode)
                q.dismiss_screenshot_overlays(page)
                q._assert_screenshot_symbols_match(page, mode, data[mode]["rows"])
                shots.append(
                    {
                        "name": ("portfolio" if mode == "monthly" else "watchlist")
                        + ".png",
                        "data": page.screenshot(full_page=True),
                    }
                )
    return accept_picks(s, data, shots)


def weekly_digest(s):
    from hadan.compat import digest as renderer

    with browser_session() as page:
        payload = page.evaluate(
            """async () => {const r=await fetch('/api/proxy/api/weekly-digest/latest',{credentials:'same-origin'}); if(!r.ok) throw new Error('digest HTTP '+r.status); return r.json()}"""
        )
    renderer.validate(payload)
    age = (
        datetime.now(timezone.utc).date() - date.fromisoformat(payload["week_start"])
    ).days
    if age > 9 or age < -2:
        raise ValueError("Digest outside freshness window")
    previous = state(s, "digest:accepted")
    if previous == payload:
        return {"unchanged": True}
    result = s.publish(
        "quantgt",
        "digest:" + payload["week_start"],
        renderer.build_subject(payload),
        renderer.build_text(payload),
        html=renderer.build_html(payload),
        baseline=previous is None,
        changes="源站周报修订"
        if previous
        and previous.get("week_start") == payload["week_start"]
        and previous != payload
        else "",
    )
    save(s, "digest:accepted", payload)
    return result


def authenticated_sender(raw, sender):
    """Return an aligned original-signature proof (falsey on verification failure)."""
    from .inbound import verify_signature
    return verify_signature(raw, sender)


def quarantine(s, identity, reason, raw=None):
    from hadan.worker import Worker

    marker = digest(identity)
    save(s, "quarantine:" + marker, {"reason": reason, "at": time.time()})
    if raw is not None and os.getenv("STORE_QUARANTINE_RAW", "0") == "1":
        existing = sorted(
            (s.settings.data_dir / "source").glob("quarantine-*.eml"),
            key=lambda p: p.stat().st_mtime,
        )
        while len(existing) >= 5:
            existing.pop(0).unlink()
        path = s.settings.data_dir / "source" / ("quarantine-" + marker + ".eml")
        path.write_bytes(raw)
        path.chmod(0o600)
    Worker(s).incident("official_mail_quarantine", reason, "message=" + marker)
    return {"quarantined": True}


def ingest_message(s, raw, source_identity):
    from .inbound import InboundRejected, body_and_files, entity, select_messages
    from hadan.db import audit

    domains = os.getenv("OFFICIAL_SENDER_DOMAINS", "quantgt.io").split(",")
    curators = os.getenv("INBOUND_CURATORS", "").split(",")
    try:
        selected = select_messages(
            raw, domains, curators, os.getenv("INBOUND_ADDRESS", ""),
            original_verifier=authenticated_sender,
        )
        # Validate every selected message before publishing any part of a bundle.
        prepared = [(item, *body_and_files(item.raw)) for item in selected]
    except (InboundRejected, UnicodeError) as exc:
        return quarantine(s, source_identity + ":" + hashlib.sha256(raw).hexdigest(), str(exc), raw)
    if not prepared:
        return {"ignored": True}
    results = []
    for item, body, files in prepared:
        message, _ = entity(item.raw)
        subject = str(message.get("Subject", "Official update")).replace("\r", " ").replace("\n", " ")[:210]
        if item.mode == "curated":
            body = "管理员转发（未独立核验官方原文）\n整理人：" + item.sender + "\n\n" + body
            title = "管理员转发 · " + subject
            prefix = "curated:"
        else:
            title = "Quant GT · " + subject
            prefix = "official:"
        source_key = prefix + item.identity
        result = s.publish("quantgt", source_key, title, body, files=files)
        provenance = {
            "mode": "official_verified" if item.mode == "official" else "owner_curated",
            "sender": item.sender,
            "signing_domain": item.proof.domain if item.proof else None,
            "message_sha256": hashlib.sha256(item.raw).hexdigest(),
            "envelope_sha256": hashlib.sha256(raw).hexdigest(),
            "mailbox_identity_sha256": digest(source_identity),
        }
        save(s, "inbound:" + item.identity, provenance)
        event_id = result.get("event_id")
        if not event_id:
            existing = s.db.one("SELECT id FROM events WHERE content_id=? ORDER BY seq DESC LIMIT 1", (result["id"],))
            event_id = existing["id"] if existing else None
        if event_id:
            with s.db.transaction() as db:
                if not result["unchanged"]:
                    audit(db, "inbound", "official_verified" if item.mode == "official" else "owner_curated", result["id"], provenance)
                # Preserve recovery after publication commits but scheduling fails.
                if os.getenv("INBOUND_TRIGGER_PICKS", "1") == "1":
                    enqueue(db, "source:picks", {}, "official-trigger:" + event_id, queue="source")
        results.append(result)
    if len(results) == 1:
        return results[0]
    return {"processed": len(results), "messages": results}


@contextmanager
def inbound_mailbox():
    """Dedicated TLS connection; selection and all body reads are read-only."""
    import ssl
    host = os.getenv("IMAP_HOST", "")
    username = os.getenv("IMAP_USERNAME", "")
    password = os.getenv("IMAP_PASSWORD", "")
    if not all((host, username, password)):
        raise ValueError("Dedicated IMAP credentials are not configured")
    client = imaplib.IMAP4_SSL(host, int(os.getenv("IMAP_PORT", "993")), ssl_context=ssl.create_default_context(), timeout=20)
    try:
        client.login(username, password)
        mailbox = os.getenv("IMAP_MAILBOX", "INBOX")
        typ, _ = client.select(mailbox, readonly=True)
        if typ != "OK":
            raise RuntimeError("IMAP mailbox not available")
        validity = _imap_number(client, "UIDVALIDITY")
        identity = host + ":" + username + ":" + mailbox + ":" + str(validity)
        yield client, identity, "imap:" + digest(identity), validity
    finally:
        try:
            client.logout()
        except Exception:
            pass


def _imap_number(client, name):
    import re
    response = client.response(name)
    parts = response[1] if response and len(response) > 1 else []
    values = [x for x in (parts or []) if isinstance(x, bytes)]
    match = re.fullmatch(rb"\s*(\d+)\s*", b" ".join(values))
    if not match or int(match[1]) < 1:
        raise RuntimeError("IMAP " + name + " is unavailable; refusing an implicit historical import")
    return int(match[1])


def _initial_checkpoint(s, client, key, mode):
    current = state(s, key, None)
    if current is not None:
        return int(current), False
    if mode not in ("latest", "all"):
        raise ValueError("IMAP_START_MODE must be latest or all")
    checkpoint = _imap_number(client, "UIDNEXT") - 1 if mode == "latest" else 0
    # A CLI cutover and a worker may race; never reset a cursor established by
    # the other process while this connection reads UIDNEXT.
    with s.db.transaction() as db:
        created = db.execute(
            "INSERT INTO source_state VALUES(?,?,?) ON CONFLICT(key) DO NOTHING",
            (key, json.dumps(checkpoint), time.time()),
        ).rowcount
        row = db.execute("SELECT value FROM source_state WHERE key=?", (key,)).fetchone()
    return int(json.loads(row["value"])), bool(created)


def cutover_mailbox(s):
    """Initialize a fresh mailbox cursor at UIDNEXT-1 without searching/fetching mail.

    Existing cursor state is preserved. The returned checkpoint contains no
    hostname, username, password or message content and is suitable for CLI output.
    """
    with inbound_mailbox() as (client, _, key, validity):
        checkpoint, initialized = _initial_checkpoint(s, client, key, "latest")
        return {"initialized": initialized, "baseline_uid": checkpoint, "uidvalidity": validity, "state_key": key}


def official_mail(s):
    import re
    with inbound_mailbox() as (client, identity, key, validity):
        mode = os.getenv("IMAP_START_MODE", "latest").strip().lower()
        if mode not in ("latest", "all"):
            raise ValueError("IMAP_START_MODE must be latest or all")
        last, initialized = _initial_checkpoint(s, client, key, mode)
        if initialized and mode == "latest":
            return {"processed": 0, "initialized": True, "baseline_uid": last, "uidvalidity": validity}
        criteria = os.getenv("IMAP_SEARCH", "ALL").strip()
        if not criteria or len(criteria) > 1024 or any(x in criteria for x in ("\r", "\n", "\x00")):
            raise ValueError("Invalid IMAP_SEARCH criteria")
        typ, values = client.uid("search", None, "UID", str(last + 1) + ":*", criteria)
        if typ != "OK":
            raise RuntimeError("IMAP UID search failed")
        uids = sorted(int(x) for x in values[0].split() if int(x) > last)[:50]
        for mid in uids:
            typ, parts = client.uid("fetch", str(mid), "(RFC822.SIZE)")
            if typ != "OK":
                raise RuntimeError("IMAP size fetch failed")
            size = re.search(rb"RFC822.SIZE (\d+)", b" ".join(x for x in parts if isinstance(x, bytes)))
            if not size or int(size[1]) > 20 * 1024 * 1024:
                quarantine(s, identity + ":" + str(mid), "Inbound message size invalid or too large")
                save(s, key, mid)
                continue
            typ, parts = client.uid("fetch", str(mid), "(BODY.PEEK[])")
            raw = next((x[1] for x in parts if isinstance(x, tuple)), None)
            if typ != "OK" or not raw:
                raise RuntimeError("IMAP fetch failed")
            try:
                ingest_message(s, raw, identity)
            except (ValueError, UnicodeError):
                quarantine(s, identity + ":" + str(mid), "Unparseable inbound message", raw)
            save(s, key, mid)
        return {"processed": len(uids)}


def health_site(s):
    from hadan.compat.site_diff import diff

    pages = [
        ("dashboard", "quantgt-picks"),
        ("monthly", "quantgt-picks"),
        ("weekly", "weekly-picks"),
        ("tradingview_indicator", "tradingview-indicator"),
        ("chart_gt", "chart-gt"),
        ("ai_winners", "who-is-winning-ai"),
        ("rrg", "rrg"),
        ("market_tools", "market-tools"),
        ("study_guide", "learn"),
        ("live_update", "notifications"),
        ("track_record", "performance"),
        ("weekly_digest", "weekly-digest"),
        ("research", "research"),
    ]
    previous = state(s, "site:accepted", {"pages": {}})
    snapshot = {"pages": {}, "capture_mode": "fresh_login_member"}
    errors = []
    with browser_session() as page:
        for name, path in pages:
            try:
                page.goto(
                    "https://quantgt.io/" + path,
                    wait_until="domcontentloaded",
                    timeout=15000,
                )
                page.wait_for_function(
                    "() => (document.querySelector('main')?.innerText || '').length > 40",
                    timeout=8000,
                )
                captured = page.evaluate(
                    """() => {const texts=s=>[...document.querySelectorAll(s)].filter(e=>e.getClientRects().length).map(e=>(e.innerText||'').trim()).filter(Boolean);return {title:document.title,headings:texts('h1,h2,h3'),nav:texts('nav a,aside a'),buttons:texts('button'),links:[...document.querySelectorAll('a[href]')].map(e=>({text:(e.innerText||'').trim(),href:e.href}))}}"""
                )
                snapshot["pages"][name] = captured
            except Exception as exc:
                errors.append(name + ":" + type(exc).__name__)
                if name in previous["pages"]:
                    snapshot["pages"][name] = previous["pages"][name]
    old_for_diff = {
        **previous,
        "pages": [{**v, "name": k} for k, v in previous["pages"].items()],
    }
    new_for_diff = {
        **snapshot,
        "pages": [{**v, "name": k} for k, v in snapshot["pages"].items()],
    }
    changes = diff(old_for_diff, new_for_diff) if previous.get("pages") else {}
    save(s, "site:accepted", snapshot)
    from hadan.worker import Worker

    if changes:
        Worker(s).incident(
            "site_structure",
            "源站结构发生变化",
            json.dumps(changes, ensure_ascii=False),
        )
    if errors:
        raise RuntimeError("Partial site scan: " + ",".join(errors))
    health(s)
    return {"changed": bool(changes)}


def health(s):
    from hadan.worker import Worker

    latest = state(s, "picks:last_success", 0)
    if os.getenv("QUANTGT_ENABLED") == "1" and time.time() - latest > 30 * 3600:
        Worker(s).incident("source_stale", "内容源长时间没有成功采集")
    return {"last_success": latest}


def install_parser_text(text):
    """Execute exactly the decoded/validated text, not loader-reinterpreted bytes."""
    import types
    import hadan.compat

    module = types.ModuleType("hadan.compat.scrape_parse")
    exec(compile(text, "<validated-parser>", "exec"), module.__dict__)
    sys.modules[module.__name__] = module
    hadan.compat.scrape_parse = module
    # Fresh source processes normally have not imported this browser module yet.
    sys.modules.pop("hadan.sources.quantgt_browser", None)
    import hadan.sources

    if hasattr(hadan.sources, "quantgt_browser"):
        delattr(hadan.sources, "quantgt_browser")


def load_active_parser(s):
    from hadan.ops import literal_only

    path = s.settings.data_dir / "source/parser_active.py"
    meta_path = path.with_suffix(".json")
    if not path.exists():
        return False
    original = Path(__file__).parents[1] / "compat/scrape_parse.py"
    base = original.read_text(encoding="utf-8")
    candidate = path.read_text(encoding="utf-8")
    meta = json.loads(meta_path.read_text())
    if (
        meta.get("base_sha256") != digest(base)
        or meta.get("sha256") != digest(candidate)
        or not literal_only(base, candidate)
    ):
        raise ValueError("Active parser failed independent policy verification")
    install_parser_text(candidate)
    return True


def activate_parser(s, candidate, expected_sha):
    from hadan.ops import literal_only

    root = (s.settings.data_dir / "ops").resolve()
    candidate = Path(candidate).resolve()
    if candidate.parent != root or not candidate.name.startswith("candidate-"):
        raise ValueError("Candidate outside maintenance directory")
    report = json.loads((candidate / "verification.json").read_text())
    relative = "src/hadan/compat/scrape_parse.py"
    text = (candidate / relative).read_text(encoding="utf-8")
    base = (Path(__file__).parents[1] / "compat/scrape_parse.py").read_text(
        encoding="utf-8"
    )
    if (
        not report.get("passed")
        or report.get("tests", 0) < 40
        or report.get("files") != [relative]
        or report.get("base_hashes", {}).get(relative) != digest(base)
        or report.get("candidate_hashes", {}).get(relative) != digest(text)
        or digest(text) != expected_sha
        or not literal_only(base, text)
    ):
        raise ValueError("Candidate approval, base or AST policy mismatch")
    install_parser_text(text)
    # Credentials stay in the source process; the model only receives a pass/fail record.
    with browser_session() as page:
        data, _ = capture_picks(page)
    from hadan.compat.validation import validate_member_picks_data

    validate_member_picks_data(data)
    prior = state(s, "picks:accepted")
    for name in ("monthly", "weekly"):
        symbols = [row["symbol"] for row in data[name]["rows"]]
        if len(symbols) != len(set(symbols)):
            raise ValueError("Parser canary returned duplicate symbols")
    if prior:
        for name in ("monthly", "weekly"):
            if prior[name].get("pick_date") not in (None, "", "Unknown") and data[
                name
            ].get("pick_date") in (None, "", "Unknown"):
                raise ValueError("Parser canary regressed a known edition date")
            if prior[name].get("pick_date") == data[name].get("pick_date"):
                old = {x["symbol"] for x in prior[name]["rows"]}
                new = {x["symbol"] for x in data[name]["rows"]}
                if len(old & new) < 0.8 * len(old):
                    raise ValueError(
                        "Parser canary changed too many symbols within same edition"
                    )
    destination = s.settings.data_dir / "source/parser_active.py"
    meta = destination.with_suffix(".json")
    if destination.exists():
        import shutil

        shutil.copy2(destination, destination.with_name("parser_previous.py"))
        shutil.copy2(meta, meta.with_name("parser_previous.json"))
    temp = destination.with_suffix(".tmp")
    temp.write_text(text, encoding="utf-8")
    temp.chmod(0o600)
    temp_meta = meta.with_suffix(".tmp-meta")
    temp_meta.write_text(
        json.dumps(
            {
                "base_sha256": digest(base),
                "sha256": digest(text),
                "activated_at": time.time(),
            }
        )
    )
    temp_meta.chmod(0o600)
    os.replace(temp_meta, meta)
    os.replace(temp, destination)
    from hadan.db import audit

    with s.db.transaction() as db:
        audit(
            db,
            "source-verifier",
            "parser_activated",
            candidate.name,
            {"sha256": digest(text)},
        )
        enqueue(
            db,
            "source:picks",
            {},
            "parser-first-observation:" + digest(text),
            queue="source",
        )
    return {"activated": True, "sha256": digest(text)}


def rollback_parser(s):
    path = s.settings.data_dir / "source/parser_active.py"
    previous = path.with_name("parser_previous.py")
    if not path.exists():
        return False
    if previous.exists():
        os.replace(previous, path)
        os.replace(path.with_name("parser_previous.json"), path.with_suffix(".json"))
    else:
        path.unlink()
        path.with_suffix(".json").unlink(missing_ok=True)
    from hadan.db import audit

    with s.db.transaction() as db:
        audit(db, "source-verifier", "parser_rolled_back", "source")
    return True


def main():
    from dotenv import load_dotenv

    load_dotenv(override=False)
    # Source jobs publish content but never send mail. They run in their own
    # process, so they must opt out of the SMTP preflight explicitly.
    s = Service(Settings(smtp_required=False))
    kind = sys.argv[1]
    if kind not in {
        "picks",
        "weekly_digest",
        "official_mail",
        "health_site",
        "health",
        "activate_parser",
    }:
        raise SystemExit("Unknown source job")
    active = False
    uses_parser = kind in {"picks", "weekly_digest", "health_site"}
    had_override = (
        uses_parser and (s.settings.data_dir / "source/parser_active.py").exists()
    )
    try:
        if kind in {"picks", "weekly_digest", "health_site"}:
            active = load_active_parser(s)
        result = (
            activate_parser(s, sys.argv[2], sys.argv[3])
            if kind == "activate_parser"
            else globals()[kind](s)
        )
        save(s, "job:" + kind, {"last_success": time.time(), "result": result})
        print(json.dumps(result, ensure_ascii=False))
    except Exception as exc:
        if active or had_override:
            rollback_parser(s)
        print(redact(type(exc).__name__ + ": " + str(exc)), file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
