"""Local browser acceptance; no real email, model, payment or production data.

Run: PYTHONPATH=src .venv/bin/python tests/journeys/browser_smoke.py
Uses the existing Chromium installation; does not install or download a browser.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from pathlib import Path

import httpx
import uvicorn
from playwright.sync_api import (
    Error as PlaywrightError,
    TimeoutError as PlaywrightTimeoutError,
    expect,
    sync_playwright,
)

from hadan.config import Settings
from hadan.migration import import_archive
from hadan.security import action_token
from hadan.web import create_app
from hadan.worker import Worker


ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "test-results" / "ux"


def select_chromium(playwright):
    """Prefer an explicit executable, then this Playwright version, then existing hosts."""
    explicit = os.environ.get("HADAN_TEST_CHROMIUM")
    if explicit:
        path = Path(explicit).expanduser().resolve()
        if not path.is_file() or not os.access(path, os.X_OK):
            raise RuntimeError(
                "HADAN_TEST_CHROMIUM must name an existing executable Chromium/Chrome binary"
            )
        return path, "HADAN_TEST_CHROMIUM"
    expected = Path(playwright.chromium.executable_path)
    if expected.is_file() and os.access(expected, os.X_OK):
        return expected, "playwright"
    for command in (
        "chromium",
        "chromium-browser",
        "google-chrome",
        "google-chrome-stable",
    ):
        installed = shutil.which(command)
        if installed:
            return Path(installed), "host-path"
    caches = [Path.home() / ".cache" / "ms-playwright", expected.parents[2]]
    configured_cache = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if configured_cache and configured_cache != "0":
        caches.insert(0, Path(configured_cache).expanduser())
    for cache in dict.fromkeys(caches):
        candidates = []
        for pattern in (
            "chromium-*/chrome-linux*/chrome",
            "chromium-*/chrome-win*/chrome.exe",
            "chromium-*/chrome-mac*/Chromium.app/Contents/MacOS/Chromium",
        ):
            candidates.extend(cache.glob(pattern))
        for candidate in sorted(candidates, reverse=True):
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return candidate, "existing-host-cache"
    raise RuntimeError(
        "No Chromium executable is installed. Run the project Python with '-m playwright install chromium', "
        "or configure HADAN_TEST_CHROMIUM; see tests/journeys/README.md."
    )


def run():
    import csv
    from hadan.commerce import Commerce

    OUT.mkdir(parents=True, exist_ok=True)
    report = {
        "checks": [],
        "layouts": [],
        "page_errors": [],
        "findings": [],
        "screenshots": [],
        "started_at": datetime.now(timezone.utc).isoformat(),
        "commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
    }
    with tempfile.TemporaryDirectory(prefix="quant-browser-review-") as temp:
        # This host can allocate low ephemeral ports (for example IRC's 6669),
        # which Chromium correctly refuses. Keep that browser protection enabled.
        for _ in range(100):
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.bind(("127.0.0.1", 0))
            if sock.getsockname()[1] > 10240:
                break
            sock.close()
        else:
            raise RuntimeError("No browser-safe ephemeral loopback port was allocated")
        base = f"http://127.0.0.1:{sock.getsockname()[1]}"
        cfg = Settings(
            data_dir=Path(temp) / "data", base_url=base, production=False,
            secret="local-browser-test-only-" * 4, admin_emails=("owner@example.test",),
            allowed_emails=(), mail_backend="file", mail_from="Quant <notify@example.test>",
            smtp_host="", smtp_user="", smtp_password="", stripe_key="", stripe_webhook_secret="",
            xorpay_enabled=False, xorpay_aid="", xorpay_secret="", xianyu_url="", payment_plans=[],
            deepseek_key="", ops_ai=False,
        )
        app = create_app(cfg)
        service = app.state.service
        service.seed()
        # Exercise the real code path for access; there is no test-only free bypass in the UI.
        service.db.execute("UPDATE products SET paid=1 WHERE id='quantgt'")
        code_file = Path(temp) / "access-codes.csv"
        Commerce(service).issue_codes(days=30, count=4, output=code_file)
        with code_file.open() as stream:
            codes = [row["code"] for row in csv.DictReader(stream)]
        baseline = service.publish(
            "quantgt", "browser-baseline", "历史基线报告", "监控建立基线时保留的正文。",
            files=[{"name": "baseline.txt", "data": b"baseline private attachment"}], baseline=True,
        )
        import_dir = Path(temp) / "archive-source"
        import_dir.mkdir()
        historical_bytes = b"imported private attachment"
        (import_dir / "historical.txt").write_bytes(historical_bytes)
        manifest = Path(temp) / "archive-manifest.json"
        manifest.write_text(json.dumps({"root": str(import_dir), "items": [{
            "id": "browser-import", "product_id": "quantgt", "title": "迁入的历史报告",
            "body": "从审核过的历史清单迁入。", "files": [{"path": "historical.txt", "sha256": hashlib.sha256(historical_bytes).hexdigest()}],
        }]}, ensure_ascii=False))
        imported = import_archive(service, manifest, apply=True)["items"][0]
        assert not service.db.all("SELECT * FROM receipts")
        assert not service.db.all("SELECT * FROM deliveries")
        server = uvicorn.Server(uvicorn.Config(app, log_level="critical", access_log=False, lifespan="off"))
        server_thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        server_thread.start()

        def check(name):
            report["checks"].append(name)

        def screenshot(page, name):
            masks = [page.locator(sel) for sel in ("#secret-value", "#code", "#redeem-code")
                     if page.locator(sel).count() and page.locator(sel).is_visible()]
            path = OUT / (name + ".png")
            page.screenshot(path=str(path), full_page=True, mask=masks)
            report["screenshots"].append(path.name)

        def layout(page, name):
            values = page.evaluate("""() => ({width:innerWidth,scroll:document.documentElement.scrollWidth,body:document.body.scrollWidth,height:innerHeight})""")
            width = page.viewport_size["width"]
            report["layouts"].append({"name": name, "configured_width": width, **values})
            if values["scroll"] > width + 1:
                report["findings"].append({"id": "layout-" + name, "severity": "P2", "summary": f"Viewport overflow: {values['scroll'] - width} px"})

        def context(browser, **kwargs):
            kwargs.setdefault("locale", "zh-CN")
            value = browser.new_context(**kwargs)
            # The entire test stays on a disposable loopback service.
            value.route("**/*", lambda route: route.continue_() if route.request.url.startswith(base + "/") else route.abort())
            value.on("page", lambda page: page.on("pageerror", lambda exc: report["page_errors"].append(str(exc)[:300])))
            return value

        def drain():
            worker = Worker(service)
            for _ in range(80):
                if not worker.run_once():
                    return
            raise AssertionError("Local worker did not become idle")

        def switch(page, language):
            before = page.url.split("?", 1)[0]
            page.locator(f'[data-language="{language}"]').click()
            expect(page.locator("html")).to_have_attribute("lang", language)
            assert page.url.split("?", 1)[0] == before

        def login(page, email, *, language="zh-CN", test_switch=False):
            page.goto(base + "/app?lang=" + language)
            expect(page.locator("#login")).to_be_visible()
            page.locator("#email").fill(email)
            page.locator("#request-code").click()
            expect(page.locator("#code-fields")).to_be_visible()
            drain()
            candidates = []
            for path in (cfg.data_dir / "mail").glob("*.eml"):
                message = BytesParser(policy=policy.default).parsebytes(path.read_bytes())
                body = message.get_body(preferencelist=("plain",))
                found = re.search(r"(?:验证码是 |code is )(\d{6})", body.get_content() if body else "")
                if message["To"] == email and found:
                    candidates.append((path.stat().st_mtime_ns, found.group(1)))
            code = max(candidates)[1]
            if test_switch:
                page.locator("#code").fill("123")
                switch(page, "en")
                expect(page.locator("#email")).to_have_value(email)
                expect(page.locator("#code")).to_have_value("123")
                expect(page.locator("#resend-code")).to_contain_text("Resend")
                assert email not in page.url and "123" not in page.url
                page.locator("#code").fill("111111" if code != "111111" else "222222")
                page.locator('#login-form button[type="submit"]').click()
                expect(page.locator("#login-status")).to_contain_text("code")
                assert not re.search(r"[\u3400-\u9fff]", page.locator("#login-status").inner_text())
                screenshot(page, "desktop-login-error-en")
                switch(page, "zh-CN")
                expect(page.locator("#login-status")).to_contain_text("验证码")
                check("OTP entry and email survive in-place locale switching; invalid-code feedback is bilingual")
            page.locator("#code").fill(code)
            page.locator('#login-form button[type="submit"]').click()
            expect(page.locator("#account")).to_be_visible()
            expect(page.locator("#account-email")).to_have_text(email)
            assert not page.evaluate("localStorage.length || sessionStorage.length")

        def tab(page, name):
            page.locator(f'[data-tab="{name}"]').click()
            expect(page.locator("#view-" + name)).to_be_visible()

        def sub(email="reader@example.test"):
            return service.db.one("SELECT s.* FROM subscriptions s JOIN users u ON u.id=s.user_id WHERE u.email=? AND s.product_id='quantgt'", (email,))

        def redeem(page, code):
            tab(page, "subscriptions")
            page.locator("#redeem-code").fill(code)
            page.locator('#redeem-form button[type="submit"]').click()
            expect(page.locator("#redeem-status")).to_have_class(re.compile("success"))
            expect(page.locator("#redeem-code")).to_have_value("")
            assert code not in page.url and not page.evaluate("localStorage.length || sessionStorage.length")

        def agent_client(token):
            return httpx.Client(base_url=base, headers={"Authorization": "Bearer " + token}, trust_env=False, timeout=20)

        def activate(client):
            assert client.get("/api/agent/test").status_code == 200
            assert client.get("/api/agent/test-file").status_code == 200
            assert client.post("/api/agent/test/complete").status_code == 200

        try:
            deadline = time.monotonic() + 10
            while not server.started and server_thread.is_alive() and time.monotonic() < deadline:
                time.sleep(0.02)
            assert server.started
            with sync_playwright() as pw:
                executable, selection = select_chromium(pw)
                report.update(browser_selection=selection, browser_executable=str(executable))
                browser = pw.chromium.launch(executable_path=str(executable), headless=True,
                    chromium_sandbox=not (hasattr(os, "geteuid") and os.geteuid() == 0))
                try:
                    report["browser_version"] = browser.version
                    desktop = context(browser, viewport={"width": 1365, "height": 900}, device_scale_factor=1)
                    reader = desktop.new_page()
                    reader.goto(base + "/?lang=zh-CN")
                    expect(reader.locator("h1")).to_have_text("Quant 更新送达邮箱与 Agent")
                    visible = reader.locator("body").inner_text()
                    assert not re.search(r"Hadan|Quant\s*GT|quantgt|来信|newsletter", visible, re.I)
                    expect(reader.locator("[data-subscribe-entry]")).to_have_count(1)
                    layout(reader, "desktop-home-cn"); screenshot(reader, "desktop-home-cn")
                    switch(reader, "en")
                    expect(reader.locator("h1")).to_have_text("Quant updates.For you and your agent.")
                    layout(reader, "desktop-home-en"); screenshot(reader, "desktop-home-en")
                    colors = reader.evaluate("""() => {const s=getComputedStyle(document.documentElement);return [s.getPropertyValue('--muted').trim(),s.getPropertyValue('--paper').trim()] }""")
                    def luminance(color):
                        values = [int(color[index:index+2], 16) / 255 for index in (1, 3, 5)]
                        values = [value/12.92 if value <= .04045 else ((value+.055)/1.055)**2.4 for value in values]
                        return sum(value*weight for value, weight in zip(values, (.2126, .7152, .0722)))
                    lum = sorted(luminance(color) for color in colors)
                    report["primary_muted_contrast"] = round((lum[1]+.05)/(lum[0]+.05), 4)
                    assert report["primary_muted_contrast"] >= 4.5
                    check("Bilingual Quant-only homepage offers one subscription entry point without invented performance or testimonials")

                    mobile = context(browser, viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True, device_scale_factor=1)
                    phone = mobile.new_page()
                    for language in ("zh-CN", "en"):
                        for path, name in (("/", "home"), ("/agent", "agent-guide"), ("/app", "login")):
                            phone.goto(base + path + "?lang=" + language)
                            layout(phone, "mobile-" + name + "-" + language)
                            screenshot(phone, "mobile-" + name + "-" + language)
                    phone.set_viewport_size({"width": 320, "height": 740})
                    phone.goto(base + "/?lang=en")
                    layout(phone, "small-mobile-home-en")
                    screenshot(phone, "small-mobile-home-en")
                    phone.set_viewport_size({"width": 390, "height": 844})
                    check("Home, agent instructions and login work in both languages on 390 px and home on 320 px")

                    login(reader, "reader@example.test", test_switch=True)
                    tab(reader, "subscriptions")
                    expect(reader.locator('[data-subscribe="quantgt"]')).to_have_count(0)
                    reader.locator("#purchase-section summary").click()
                    expect(reader.locator("#purchase-options")).to_contain_text("没有可购买")
                    expect(reader.locator("#purchase-form")).to_be_hidden()
                    assert not re.search(r"[¥￥$]\s*\d|\d+\s*CNY", reader.locator("#view-subscriptions").inner_text())
                    expect(reader.locator("#purchase-options")).not_to_contain_text("USDT")
                    layout(reader, "desktop-subscription-unconfigured"); screenshot(reader, "desktop-subscription-unconfigured")
                    switch(reader, "en")
                    reader.locator("#redeem-code").fill("NOT-A-VALID-CODE")
                    reader.locator('#redeem-form button[type="submit"]').click()
                    expect(reader.locator("#redeem-status")).to_contain_text("invalid")
                    expect(reader.locator("#redeem-status")).to_have_class(re.compile("error"))
                    switch(reader, "zh-CN")
                    expect(reader.locator("#redeem-status")).to_contain_text("无效")
                    redeem(reader, codes[0])
                    first_expiry = sub()["expires_at"]
                    reader.locator("#redeem-code").fill(codes[0])
                    reader.locator('#redeem-form button[type="submit"]').click()
                    expect(reader.locator("#redeem-status")).to_have_class(re.compile("error"))
                    reader.locator("#redeem-code").fill("")
                    assert sub()["expires_at"] == first_expiry
                    check("Unconfigured payments show no price or checkout; real single-use redemption grants access and rejects reuse")

                    # Settings must be usable before a delayed report fetch finishes.
                    held = []
                    reader.route("**/api/events?*", lambda route: held.append(route))
                    reader.reload(wait_until="domcontentloaded")
                    expect(reader.locator("#account")).to_be_visible(timeout=2000)
                    tab(reader, "connections")
                    with reader.expect_response(lambda response: "/api/subscriptions/" in response.url and response.request.method == "PATCH", timeout=2000):
                        reader.locator('[data-delivery-pref="email_enabled"]').click()
                    assert not sub()["email_enabled"]
                    for route in held:
                        try: route.continue_()
                        except PlaywrightError as exc:
                            if "already handled" not in str(exc): raise
                    reader.unroute("**/api/events?*")
                    with reader.expect_response(lambda response: "/api/subscriptions/" in response.url and response.request.method == "PATCH", timeout=2000):
                        reader.locator('[data-delivery-pref="email_enabled"]').click()
                    expect(reader.locator('[data-delivery-pref="email_enabled"]')).to_have_attribute("aria-pressed","true")
                    assert sub()["email_enabled"] == 1
                    expect(reader.locator("#bindings-list")).to_contain_text("未连接")
                    layout(reader, "desktop-delivery-cn"); screenshot(reader, "desktop-delivery-cn")
                    check("Delivery preferences remain interactive while feed loading is delayed and save independent email options")

                    tab(reader, "updates")
                    expect(reader.locator("#updates-list")).to_contain_text("历史基线报告")
                    expect(reader.locator("#updates-list")).not_to_contain_text("Agent 待处理")
                    for archive, title, body, expected_bytes in (
                        (baseline, "历史基线报告", "监控建立基线", b"baseline private attachment"),
                        (imported, "迁入的历史报告", "审核过的历史清单", historical_bytes),
                    ):
                        reader.locator("#updates-list .entry").filter(has_text=title).locator('a[href^="/read/"]').click()
                        expect(reader.locator("h1")).to_have_text(title)
                        expect(reader.locator(".prose")).to_contain_text(body)
                        with reader.expect_download() as saved:
                            reader.locator("a.attachment").click()
                        assert Path(saved.value.path()).read_bytes() == expected_bytes
                        reader.goto(base + "/app?lang=zh-CN")
                        expect(reader.locator("#updates-list")).to_contain_text(title)
                    assert all(event["content_id"] is None for event in reader.request.get(base + "/api/events").json()["events"])
                    check("Imported and baseline reports remain private downloadable archives without content notifications or fake Agent-pending status")

                    admin_context = context(browser, viewport={"width": 1365, "height": 900}, device_scale_factor=1)
                    admin = admin_context.new_page()
                    login(admin, "owner@example.test", language="en")
                    admin.locator("#admin-link").click()
                    expect(admin.locator("h1")).to_have_text("Service overview")
                    expect(admin.locator("#publish-product option")).to_have_count(1)
                    admin.locator("#source-key").fill("browser-report")
                    admin.locator("#title").fill("本周 Portfolio 更新")
                    admin.locator("#body").fill("本周组合调整说明。\n正文和两个附件均仅向订阅者开放。")
                    file_data = {"portfolio.csv": b"symbol,weight\nEXAMPLE,0.1\n", "说明.txt": "附件说明：长期保存。".encode()}
                    admin.locator("#files").set_input_files([{"name":name,"mimeType":"text/plain","buffer":value} for name,value in file_data.items()])
                    admin.locator('#publish-form button[type="submit"]').click()
                    expect(admin.locator("#publish-result")).to_contain_text("Version 1 saved")
                    item = service.db.one("SELECT * FROM contents WHERE source_key='browser-report'")
                    layout(admin, "desktop-admin-en"); screenshot(admin, "desktop-admin-en")
                    switch(admin, "zh-CN")
                    expect(admin.locator("h1")).to_have_text("运行概况")
                    # Typed original content is never translated or discarded by the language switch.
                    assert "两个附件" in admin.locator("#body").input_value()
                    admin.set_viewport_size({"width":390,"height":844})
                    layout(admin, "mobile-admin-cn"); screenshot(admin, "mobile-admin-cn")
                    admin.set_viewport_size({"width":1365,"height":900})
                    check("Bilingual administrator form publishes original-language body and two files, with one Quant product")
                    # The simplified UI no longer exposes email-format controls;
                    # exercise the still-supported full-content email directly.
                    service.preferences(sub()["user_id"], sub()["id"], {"email_format": "full"})
                    drain()
                    mails = [BytesParser(policy=policy.default).parsebytes(p.read_bytes()) for p in (cfg.data_dir/"mail").glob("*.eml")]
                    content_mail = [m for m in mails if m["To"] == "reader@example.test" and m["Subject"] == "本周 Portfolio 更新"]
                    assert len(content_mail) == 1 and len(list(content_mail[0].iter_attachments())) == 2
                    check("Local full-content email contains both original attachments")

                    reader.goto(base + "/app?lang=en")
                    expect(reader.locator("#updates-list")).to_contain_text("本周 Portfolio 更新")
                    reader.locator("#updates-list .entry").filter(has_text="本周 Portfolio 更新").locator('a[href^="/read/"]').click()
                    expect(reader.locator(".prose")).to_contain_text("两个附件")
                    expect(reader.locator("h2")).to_contain_text("Attachments")
                    for link in reader.locator("a.attachment").all():
                        with reader.expect_download() as saved: link.click()
                        assert Path(saved.value.path()).read_bytes() == file_data[saved.value.suggested_filename]
                    layout(reader,"desktop-content-en"); screenshot(reader,"desktop-content-en")
                    assert not re.search(r"Hadan|Quant\s*GT", reader.locator("header").inner_text())
                    mobile_user = context(browser, viewport={"width":390,"height":844},is_mobile=True,has_touch=True,device_scale_factor=1,storage_state=desktop.storage_state())
                    mobile_reader = mobile_user.new_page()
                    mobile_reader.goto(base + "/read/" + item["id"] + "?lang=zh-CN")
                    layout(mobile_reader,"mobile-content-cn"); screenshot(mobile_reader,"mobile-content-cn")
                    check("Reader downloads both exact files while English interface preserves Chinese report and source content")

                    admin.locator("#body").fill("修订后的组合说明。\n已更正一处配置数据。")
                    admin.locator("#changes").fill("更正一处配置数据")
                    admin.locator('#publish-form button[type="submit"]').click()
                    expect(admin.locator("#publish-result")).to_contain_text("版本 2")
                    reader.reload()
                    expect(reader.locator(".notice")).to_contain_text("更正一处配置数据")
                    reader.locator("details summary").click()
                    reader.locator('a[href$="?version=1"]').click()
                    expect(reader.locator(".prose")).to_contain_text("本周组合调整说明")
                    before_version = reader.url
                    switch(reader, "zh-CN")
                    assert "version=1" in reader.url and reader.url.split("?",1)[0] == before_version.split("?",1)[0]
                    expect(reader.locator(".prose")).to_contain_text("本周组合调整说明")
                    check("Revision explanation and old version stay accessible; switching language preserves version route")

                    reader.goto(base + "/app?view=connections&lang=en")
                    expect(reader.locator("#view-connections")).to_be_visible()
                    with httpx.Client(base_url=base,trust_env=False,timeout=20) as device:
                        initial_pair = device.post("/api/agent/pair",json={"name":"Reading assistant","mode":"poll"}).json()
                    reader.goto(initial_pair["verification_uri"] + "&manual=1")
                    expect(reader.locator("#pair-agent-name")).to_have_text("Reading assistant")
                    reader.locator("#approve-pair").click()
                    credential = initial_pair["device_secret"]
                    with agent_client(credential) as agent: activate(agent)
                    reader.reload()
                    expect(reader.locator("#bindings-list")).to_contain_text("Connected")
                    check("Agent pairing is link-based; owner approves once and actual body/file testing activates it")

                    with service.db.transaction() as db:
                        notice_id = service.account_notice(db,sub()["user_id"],"quantgt","payment_failed","续费提醒","本次续费未完成，请前往订阅管理核对。","browser-account-notice")
                        service.account_notice(db,sub()["user_id"],"quantgt","payment_verifying","付款确认中","请勿重复付款。","browser-payment-verifying")
                        service.account_notice(db,sub()["user_id"],"quantgt","payment_review","付款待核对","请联系管理员，勿重复付款。","browser-payment-review")
                    reader.goto(base + "/app?lang=en")
                    notice_entry = reader.locator("#updates-list .entry").filter(has_text="Renewal unsuccessful")
                    expect(notice_entry).to_have_count(1)
                    expect(notice_entry).to_contain_text("Awaiting agent processing")
                    expect(reader.locator("#updates-list")).to_contain_text("Verifying payment")
                    expect(reader.locator("#updates-list")).to_contain_text("Payment confirmed; access needs review")
                    assert notice_entry.locator("a").count() == 0
                    assert "/read/null" not in reader.content() and "/read/undefined" not in reader.content()
                    screenshot(reader,"desktop-account-notice-en")
                    # Replace the default agent only after a real pairing and test sequence.
                    with httpx.Client(base_url=base,trust_env=False,timeout=20) as device:
                        pair = device.post("/api/agent/pair",json={"name":"Replacement assistant","mode":"poll"}).json()
                        reader.goto(pair["verification_uri"] + "&manual=1")
                        expect(reader.locator("#pair-agent-name")).to_have_text("Replacement assistant")
                        reader.locator('#pair-form button[type="submit"]').click()
                        expect(reader.locator("#toast")).to_contain_text("Authorization saved")
                        assert device.post("/api/agent/pair/status",json={"pair_id":pair["pair_id"],"device_secret":pair["device_secret"]}).json()["state"] == "approved"
                    with agent_client(credential) as old_agent:
                        assert old_agent.get("/api/events?pending=true").status_code == 200
                    replacement = pair["device_secret"]
                    with agent_client(replacement) as agent:
                        activate(agent)
                        pending = agent.get("/api/events?pending=true").json()["events"]
                        assert notice_id in {event["id"] for event in pending}
                        assert agent.post(f"/api/events/{notice_id}/ack").json() == {"state":"processed"}
                    with agent_client(credential) as old_agent:
                        assert old_agent.get("/api/events").status_code == 401
                    reader.goto(base + "/app?lang=en")
                    expect(reader.locator("#updates-list .entry").filter(has_text="Renewal unsuccessful")).to_contain_text("Agent confirmed processing")
                    check("Replacing an agent preserves pending events, revokes the old credential only after testing and distinguishes processing from receipt")

                    other_context = context(browser,viewport={"width":1365,"height":900})
                    other = other_context.new_page()
                    login(other,"observer@example.test")
                    redeem(other,codes[1])
                    assert notice_id not in {event["id"] for event in other.request.get(base+"/api/events").json()["events"]}
                    assert reader.request.post(base+f"/api/events/{notice_id}/ack").status == 401
                    check("Account notices remain private to their owner; human sessions cannot forge Agent processing")



                    unsubscribe = base+"/unsubscribe/"+action_token(cfg.secret,sub()["id"])
                    phone.goto(unsubscribe+"?lang=en")
                    expect(phone.locator("h1")).to_have_text("Turn off Quant email")
                    assert sub()["email_enabled"] == 1
                    switch(phone,"zh-CN")
                    phone.locator('form button[type="submit"]').click()
                    expect(phone.locator("main")).to_contain_text("内容邮件已关闭")
                    switch(phone,"en")
                    expect(phone.locator("main")).to_contain_text("Email is off")
                    assert sub()["email_enabled"] == 0 and sub()["agent_enabled"] == 1
                    layout(phone,"mobile-unsubscribe-en"); screenshot(phone,"mobile-unsubscribe-en")
                    check("Bilingual anonymous email unsubscribe confirms intent and preserves agent delivery and paid access")

                    mobile_reader.goto(base+"/app?view=subscriptions&lang=en")
                    expect(mobile_reader.locator("#view-subscriptions")).to_be_visible()
                    tab(mobile_reader, "connections")
                    mobile_reader.once("dialog",lambda dialog:dialog.accept())
                    if sub()["email_enabled"]:
                        with mobile_reader.expect_response(lambda response: "/api/subscriptions/" in response.url and response.request.method == "PATCH", timeout=2000):
                            mobile_reader.locator('[data-delivery-pref="email_enabled"]').click()
                    if sub()["agent_enabled"]:
                        with mobile_reader.expect_response(lambda response: "/api/subscriptions/" in response.url and response.request.method == "PATCH", timeout=2000):
                            mobile_reader.locator('[data-delivery-pref="agent_enabled"]').click()
                    expect(mobile_reader.locator("#toast")).to_contain_text("saved")
                    assert not sub()["email_enabled"] and not sub()["agent_enabled"]
                    assert mobile_reader.request.get(base+"/read/"+item["id"]).status == 200
                    redeem(mobile_reader,codes[2])
                    assert sub()["expires_at"] > first_expiry and not sub()["email_enabled"] and not sub()["agent_enabled"]
                    layout(mobile_reader,"mobile-renewed-en"); screenshot(mobile_reader,"mobile-renewed-en")
                    tab(mobile_reader,"connections")
                    expect(mobile_reader.locator('[data-delivery-pref="email_enabled"]')).to_have_attribute("aria-pressed","false")
                    expect(mobile_reader.locator('[data-delivery-pref="agent_enabled"]')).to_have_attribute("aria-pressed","false")
                    layout(mobile_reader,"mobile-delivery-en"); screenshot(mobile_reader,"mobile-delivery-en")
                    check("Both delivery channels can be turned off while paid reading remains; access extension does not reset opt-outs")

                    long_file = service.publish("quantgt","long-filename","长文件名阅读测试","附件名称应在小屏完整显示。",files=[{"name":"weekly-"+"Portfolio_"*13+".csv","data":b"a,b\n1,2\n"}])
                    mobile_reader.goto(base+"/read/"+long_file["id"])
                    layout(mobile_reader,"mobile-long-filename"); screenshot(mobile_reader,"mobile-long-filename")
                    mobile_reader.set_viewport_size({"width":320,"height":740})
                    layout(mobile_reader,"small-mobile-long-filename"); screenshot(mobile_reader,"small-mobile-long-filename")
                    check("Long attachment filenames wrap at 390 px and 320 px")
                    service.withdraw(item["id"],"这份内容已撤回，请等待更正","test-admin")
                    withdrawn_response = reader.goto(base+"/read/"+item["id"]+"?lang=en")
                    assert withdrawn_response.status == 410 and "text/html" in withdrawn_response.headers["content-type"]
                    expect(reader.locator("main")).to_contain_text("withdrawn")
                    expect(reader.locator("h1")).to_have_text("Content unavailable")
                    switch(reader,"zh-CN")
                    expect(reader.locator("main")).to_contain_text("已撤回")
                    screenshot(reader,"desktop-withdrawn-cn")
                    invalid = phone.goto(base+"/unsubscribe/invalid-link?lang=en")
                    assert invalid.status == 400 and "text/html" in invalid.headers["content-type"]
                    expect(phone.locator("main")).to_contain_text("invalid")
                    screenshot(phone,"mobile-invalid-unsubscribe-en")
                    check("Withdrawn reports and invalid unsubscribe URLs give bilingual recoverable HTML errors")

                    # Real admin withdrawal/replay remains useful within the single Quant product.
                    admin.goto(base+"/admin?lang=en")
                    expect(admin.locator("#publish-form")).to_be_visible()
                    admin.locator("#source-key").fill("browser-admin-operations")
                    admin.locator("#title").fill("服务通知：后台撤回测试")
                    admin.locator("#body").fill("这是一条用于验证后台撤回操作的本地通知。")
                    admin.locator("#files").set_input_files([])
                    admin.locator('#publish-form button[type="submit"]').click()
                    expect(admin.locator("#publish-result")).to_contain_text("Version 1")
                    operational = service.db.one("SELECT * FROM contents WHERE source_key='browser-admin-operations'")
                    withdraw = admin.locator(f'[data-withdraw="{operational["id"]}"]')
                    admin.once("dialog",lambda dialog:dialog.accept(""))
                    withdraw.click()
                    expect(admin.locator("#toast")).to_contain_text("withdrawal reason")
                    assert not service.db.one("SELECT withdrawn FROM contents WHERE id=?",(operational["id"],))["withdrawn"]
                    admin.once("dialog",lambda dialog:dialog.accept("资料已更正，需要撤回旧版。"))
                    withdraw.click()
                    expect(admin.locator("#toast")).to_contain_text("Content withdrawn")
                    expect(admin.locator(f'[data-withdraw="{operational["id"]}"]')).to_have_count(0)
                    draft = service.publish("quantgt","browser-replay-draft","待补投的历史通知","需要明确操作后补投的本地历史正文。",baseline=True)
                    admin.locator("#refresh-admin").click()
                    replay = admin.locator(f'[data-replay="{draft["event_id"]}"]')
                    expect(replay).to_have_count(1)
                    previews=[]
                    def dismiss(dialog):
                        previews.append(dialog.message); dialog.dismiss()
                    admin.once("dialog",dismiss)
                    replay.click()
                    expect(replay).to_be_enabled()
                    assert previews and "accounts" in previews[0]
                    assert not service.db.all("SELECT * FROM receipts WHERE event_id=?",(draft["event_id"],))
                    admin.once("dialog",lambda dialog:dialog.accept())
                    replay.click()
                    expect(admin.locator("#toast")).to_contain_text("Replay reviewed")
                    assert service.db.all("SELECT * FROM receipts WHERE event_id=?",(draft["event_id"],))
                    screenshot(admin,"desktop-admin-replay-en")
                    check("Single-product admin withdrawal validates reasons, and replay previews before explicitly queuing eligible recipients")

                    # Paginate real source reports; account notices keep localized labels.
                    service.db.execute("UPDATE subscriptions SET email_enabled=0,agent_enabled=1 WHERE user_id=?",(sub()["user_id"],))
                    earlier=service.publish("quantgt","paging-old-event","较早的事件分页标记","较早的事件正文。")
                    for index in range(110):
                        service.publish("quantgt",f"paging-event-{index}",f"历史更新 {index:03d}","用于验证事件分页。")
                    with service.db.transaction() as db:
                        newest_notice=service.account_notice(db,sub()["user_id"],"quantgt","payment_failed","最新续费通知：分页末端","这条通知必须立即出现在最近更新。","browser-paging-last-notice")
                    for index in range(51):
                        service.publish("quantgt",f"paging-archive-{index}","归档分页最早标记" if index==0 else f"迁入归档 {index:03d}","可通过归档分页读到的历史正文。",baseline=True)
                    reader.goto(base+"/app?lang=en")
                    expect(reader.locator("#events-list")).to_contain_text("Renewal unsuccessful")
                    expect(reader.locator("#events-list")).not_to_contain_text("较早的事件分页标记")
                    reader.locator("#load-older-events").click()
                    expect(reader.locator("#events-list .entry").filter(has_text="较早的事件分页标记")).to_have_count(1)
                    expect(reader.locator("#archive-list")).not_to_contain_text("归档分页最早标记")
                    reader.locator("#load-more-archive").click()
                    target=reader.locator("#archive-list .entry").filter(has_text="归档分页最早标记")
                    expect(target).to_have_count(1)
                    target.locator('a[href^="/read/"]').click()
                    expect(reader.locator(".prose")).to_contain_text("可通过归档分页读到")
                    assert reader.request.get(base+"/api/events?newest=true&limit=1").json()["events"][0]["id"]==newest_notice
                    ascending=reader.request.get(base+"/api/events?limit=100").json()["events"]
                    assert [e["seq"] for e in ascending]==sorted(e["seq"] for e in ascending)
                    check("More than 100 events and 50 archived reports paginate without duplicate or missing records; default Agent order remains ascending")

                    # Test the visual checkout contract using declared synthetic responses only.
                    # No actual price/provider is configured and no external payment is made.
                    reader.route("**/api/purchase-options",lambda route:route.fulfill(json={
                        "plans":[{"id":"test-only","title":"Synthetic test fixture","days":30,"amount_minor":100,"currency":"CNY","quote_id":"quote-test-only"}],
                        "terms_version":"2026-10-28", "methods":[{"id":"code","enabled":True},{"id":"xianyu","enabled":True,"url":"https://www.goofish.com/item?id=test-only"},{"id":"wechat","enabled":True},{"id":"alipay","enabled":False},{"id":"usdt","enabled":False}],
                    }))
                    order={"id":"test-order","status":"pending","paid":False,"amount_minor":100,"currency":"CNY","expires_at":time.time()+1800,"qr_url":"/static/favicon.svg","pay_url":"https://example.test/pay"}
                    purchase_calls=[]
                    def create_order(route):
                        purchase_calls.append(route.request.post_data_json); route.fulfill(json=order)
                    reader.route("**/api/purchase",create_order)
                    order_state={"status":"verifying","paid":False}
                    reader.route("**/api/orders/test-order",lambda route:route.fulfill(json={**order,**order_state}))
                    reader.goto(base+"/app?view=subscriptions&lang=en")
                    expect(reader.locator("#purchase-section")).to_be_visible()
                    reader.locator("#purchase-section summary").click()
                    expect(reader.locator('#purchase-options a[href^="https://www.goofish.com/"]')).to_have_count(1)
                    expect(reader.locator("#purchase-methods input")).to_have_count(1)
                    expect(reader.locator("#purchase-plan")).to_contain_text("30 days")
                    reader.locator("#accept-terms").check()
                    reader.locator("#purchase-submit").click()
                    expect(reader.locator("#purchase-order")).to_contain_text("Awaiting payment confirmation")
                    expect(reader.locator("#purchase-order img")).to_be_visible()
                    expect(reader.locator('#purchase-order .order-actions a[href="https://example.test/pay"]')).to_be_visible()
                    assert purchase_calls == [{"plan_id":"test-only","method":"wechat","accept_terms":True,"terms_version":"2026-10-28","quote_id":"quote-test-only"}]
                    assert "order=test-order" in reader.url
                    reader.reload(wait_until="domcontentloaded")
                    expect(reader.locator("#purchase-order")).to_contain_text("being verified")
                    expect(reader.locator("#purchase-order img")).to_have_count(0)
                    expect(reader.locator("#purchase-submit")).to_be_disabled()
                    reader.locator("#check-payment").click()
                    expect(reader.locator("#purchase-order")).to_contain_text("do not pay again")
                    expect(reader.locator("#purchase-order img")).to_have_count(0)
                    expect(reader.locator("#purchase-order a")).to_have_count(0)
                    order_state.update(status="review",paid=False)
                    reader.locator("#check-payment").click()
                    expect(reader.locator("#purchase-order")).to_contain_text("access needs review")
                    expect(reader.locator("#purchase-submit")).to_be_disabled()
                    switch(reader,"zh-CN")
                    expect(reader.locator("#purchase-order")).to_contain_text("人工核对")
                    switch(reader,"en")
                    order_state.update(status="paid",paid=True)
                    reader.locator("#check-payment").click()
                    expect(reader.locator("#purchase-order")).to_contain_text("Payment confirmed")
                    expect(reader.locator("#purchase-submit")).to_be_enabled()
                    check("Synthetic checkout renders configured methods, separates Xianyu codes, blocks repayment during verification/review and refreshes paid state; no live payment")
                    assert not report["page_errors"], "Browser emitted JavaScript exceptions"
                    check("No JavaScript page exceptions during all exercised journeys")
                finally:
                    browser.close()
        except Exception as exc:
            report["fatal"]=type(exc).__name__+": "+str(exc)[:1200]
            raise
        finally:
            server.should_exit=True
            server_thread.join(timeout=10)
            sock.close()
            report["owned_server_stopped"]=not server_thread.is_alive()
            (OUT/"report.json").write_text(json.dumps(report,ensure_ascii=False,indent=2))
            print(json.dumps({"checks":len(report["checks"]),"layouts":len(report["layouts"]),"findings":report["findings"],"page_errors":report["page_errors"],"owned_server_stopped":report["owned_server_stopped"],"report":str(OUT/"report.json")},ensure_ascii=False))
            assert not server_thread.is_alive(), "Owned test server did not stop"
            if "fatal" not in report:
                assert not [finding for finding in report["findings"] if finding["severity"] in ("P1","P2")], "Unresolved layout findings; see report.json"


if __name__ == "__main__":
    run()
