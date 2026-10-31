"""Focused browser regression for private deep links and resolved payment notices.

PYTHONPATH=src .venv/bin/python tests/journeys/browser_i18n_edges.py
Temporary loopback server, file mail, and synthetic checkout responses only.
"""
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from pathlib import Path
from urllib.parse import parse_qs, urlsplit, urlencode

import uvicorn
from playwright.sync_api import expect, sync_playwright

from browser_smoke import select_chromium
from hadan.config import Settings
from hadan.db import uid
from hadan.oauth import OAuth
from hadan.web import create_app
from hadan.worker import Worker


def run():
    results = {
        "checks": [],
        "page_errors": [],
        "started_at": datetime.now(timezone.utc).isoformat(),
        "commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=Path(__file__).resolve().parents[2], text=True
        ).strip(),
    }
    with tempfile.TemporaryDirectory(prefix="quant-ui-edges-") as temp:
        for _ in range(100):
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            if sock.getsockname()[1] > 10240:
                break
            sock.close()
        else:
            raise RuntimeError("No browser-safe loopback port available")
        origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
        cfg = Settings(data_dir=Path(temp)/"state",base_url=origin,production=False,
                       secret="disposable-browser-secret-"*4,admin_emails=(),allowed_emails=(),
                       mail_backend="file",mail_from="Quant <notify@example.test>",smtp_host="",smtp_user="",smtp_password="",
                       stripe_key="",stripe_webhook_secret="",xorpay_enabled=False,xorpay_aid="",xorpay_secret="",xianyu_url="",payment_plans=[],deepseek_key="",ops_ai=False)
        app = create_app(cfg)
        service = app.state.service
        service.seed()
        user_id = uid()
        service.db.execute("INSERT INTO users VALUES(?,?,?)",(user_id,"edges@example.test",time.time()))
        service.subscribe(user_id,"quantgt")
        item = service.publish("quantgt","private-version-edge","Private version test","Original version body",baseline=True)
        service.publish("quantgt","private-version-edge","Private version test","Updated version body",changes="Corrected synthetic example",baseline=True)
        with service.db.transaction() as db:
            for kind, body, key in [
                ("payment_verifying","支付平台报告已支付，正在核对完整付款凭证。请勿重复付款；如长时间未开通，请联系管理员。","verify"),
                ("payment_review","已确认收到付款，但当前权益发生变化，需要管理员核对。请勿重复付款。","review"),
                ("payment_resolved","管理员已完成付款核对。处理结果：权益已确认。","fulfilled"),
                ("payment_resolved","管理员已完成付款核对。处理结果：已在支付平台退款。","refunded"),
            ]:
                service.account_notice(db,user_id,"quantgt",kind,"Quant · 付款核对",body,"edge:"+key)
        server = uvicorn.Server(uvicorn.Config(app,log_level="critical",access_log=False,lifespan="off"))
        thread = threading.Thread(target=server.run,kwargs={"sockets":[sock]},daemon=True)
        thread.start()
        def login(page):
            page.locator("#email").fill("edges@example.test")
            page.locator("#request-code").click()
            expect(page.locator("#code-fields")).to_be_visible()
            for _ in range(30):
                if not Worker(service).run_once():
                    break
            messages=[]
            for path in (cfg.data_dir/"mail").glob("*.eml"):
                message=BytesParser(policy=policy.default).parsebytes(path.read_bytes())
                part=message.get_body(preferencelist=("plain",))
                code=re.search(r"验证码是 (\d{6})",part.get_content() if part else "")
                if code: messages.append((path.stat().st_mtime_ns,code.group(1)))
            page.locator("#code").fill(max(messages)[1])
            page.locator('#login-form button[type="submit"]').click()
        try:
            deadline=time.monotonic()+10
            while not server.started and thread.is_alive() and time.monotonic()<deadline:
                time.sleep(.02)
            assert server.started
            with sync_playwright() as pw:
                executable,_=select_chromium(pw)
                browser=pw.chromium.launch(executable_path=str(executable),headless=True,chromium_sandbox=not(hasattr(os,"geteuid") and os.geteuid()==0))
                try:
                    context=browser.new_context(locale="en",viewport={"width":1280,"height":900})
                    context.route("**/*",lambda route:route.continue_() if route.request.url.startswith(origin+"/") else route.abort())
                    page=context.new_page()
                    page.on("pageerror",lambda error:results["page_errors"].append(str(error)))
                    page.goto(origin+"/read/"+item["id"]+"?version=1&lang=en")
                    assert parse_qs(urlsplit(page.url).query)["next"]==["/read/"+item["id"]+"?version=1"]
                    login(page)
                    expect(page.locator(".prose")).to_have_text("Original version body")
                    expect(page.locator("main")).to_contain_text("earlier version")
                    assert parse_qs(urlsplit(page.url).query)["version"]==["1"]
                    page.locator('[data-language="zh-CN"]').click()
                    expect(page.locator("main")).to_contain_text("较早版本")
                    assert parse_qs(urlsplit(page.url).query)["version"]==["1"]
                    results["checks"].append("Anonymous private-version deep link survives OTP login and language switch")
                    page.goto(origin+"/app?lang=en")
                    expect(page.locator("#updates-list")).to_contain_text("Verifying payment")
                    expect(page.locator("#updates-list")).to_contain_text("access needs review")
                    expect(page.locator("#updates-list")).to_contain_text("Your access has been confirmed.")
                    expect(page.locator("#updates-list")).to_contain_text("A refund was issued through the payment provider.")
                    page.locator('[data-language="zh-CN"]').click()
                    expect(page.locator("#updates-list")).to_contain_text("权益已确认")
                    expect(page.locator("#updates-list")).to_contain_text("已在支付平台退款")
                    results["checks"].append("Payment verifying, review and both resolved outcomes preserve accurate bilingual meaning")
                    service.db.execute("UPDATE subscriptions SET expires_at=? WHERE user_id=?", (time.time()+86400, user_id))
                    page.route("**/api/purchase-options",lambda route:route.fulfill(json={"terms_version":"2026-10-28","methods":[{"id":"wechat","enabled":True}],"plans":[{"id":"test-only","days":30,"amount_minor":100,"currency":"CNY"}]}))
                    order={"id":"closed-test","status":"pending","paid":False,"expires_at":time.time()+60,"amount_minor":100,"currency":"CNY","qr_url":"/static/favicon.svg"}
                    page.route("**/api/purchase",lambda route:route.fulfill(json=order))
                    page.route("**/api/orders/closed-test",lambda route:route.fulfill(json={**order,"status":"closed","qr_url":None}))
                    page.goto(origin+"/app?view=subscriptions&lang=en")
                    expect(page.locator("#account")).to_be_visible()
                    page.locator("#purchase-section summary").click()
                    page.locator("#accept-terms").check()
                    page.locator("#purchase-submit").click()
                    expect(page.locator("#purchase-order")).to_contain_text("Awaiting payment confirmation")
                    page.locator("#check-payment").click()
                    expect(page.locator("#purchase-order")).to_contain_text("Order review is complete")
                    expect(page.locator("#purchase-order img")).to_have_count(0)
                    expect(page.locator("#check-payment")).to_have_count(0)
                    expect(page.locator("#purchase-submit")).to_be_enabled()
                    results["checks"].append("Closed order shows terminal outcome guidance without QR or stale payment polling")

                    # A broken QR image must expose the payment link instead of a blank card.
                    broken = {
                        "id": "broken-qr",
                        "status": "pending",
                        "paid": False,
                        "expires_at": time.time() + 600,
                        "amount_minor": 100,
                        "currency": "CNY",
                        "qr_url": "/api/orders/broken-qr/qr",
                        "pay_url": "https://example.test/pay",
                    }
                    page.unroute("**/api/purchase")
                    page.route("**/api/purchase", lambda route: route.fulfill(json=broken))
                    page.route("**/api/orders/broken-qr/qr", lambda route: route.fulfill(status=404, body=b""))
                    page.goto(origin + "/app?view=subscriptions&lang=en")
                    page.locator("#purchase-section summary").click()
                    page.locator("#accept-terms").check()
                    page.locator("#purchase-submit").click()
                    expect(page.locator("#purchase-order .qr-fallback")).to_be_visible()
                    expect(page.locator("#purchase-order .qr-fallback")).to_contain_text("QR code is unavailable")
                    expect(page.locator('#purchase-order .qr-fallback a[href="https://example.test/pay"]')).to_be_visible()
                    results["checks"].append("Failed QR image degrades to the payment link without hiding the order")

                    # Lifetime access and a fixed offer already covered by the
                    # account keep the order card available but hide new purchase.
                    service.db.execute(
                        "UPDATE subscriptions SET expires_at=NULL WHERE user_id=?", (user_id,)
                    )
                    page.unroute("**/api/purchase-options")
                    page.route("**/api/purchase-options", lambda route: route.fulfill(json={
                        "terms_version": "2026-10-28",
                        "methods": [{"id": "wechat", "enabled": True}],
                        "plans": [{"id": "test-only", "days": 30, "amount_minor": 100, "currency": "CNY"}],
                    }))
                    page.goto(origin + "/app?view=subscriptions&lang=en")
                    expect(page.locator("#purchase-form")).to_be_hidden()
                    expect(page.locator("#purchase-options")).to_contain_text("already covers")
                    service.db.execute(
                        "UPDATE subscriptions SET expires_at=? WHERE user_id=?",
                        (time.time() + 86400, user_id),
                    )
                    page.unroute("**/api/purchase-options")
                    page.route("**/api/purchase-options", lambda route: route.fulfill(json={
                        "terms_version": "2026-10-28",
                        "methods": [{"id": "wechat", "enabled": True}],
                        "plans": [{"id": "fixed", "access_until": time.time() + 3600, "days": 0, "amount_minor": 100, "currency": "CNY"}],
                    }))
                    page.goto(origin + "/app?view=subscriptions&lang=en")
                    expect(page.locator("#purchase-form")).to_be_hidden()
                    expect(page.locator("#purchase-options")).to_contain_text("already covers")
                    results["checks"].append("Lifetime and already-covered fixed offers hide duplicate purchase while access remains readable")
                    # OAuth page static and dynamic status must switch language without losing input.
                    oauth = OAuth(service)
                    client_id = "ui-edge-client"
                    redirect_uri = "https://chatgpt.com/connector/oauth/ui-edge"
                    service.db.execute(
                        "INSERT INTO oauth_clients VALUES(?,?,?,?,?,?)",
                        (client_id, "UI edge client", json.dumps([redirect_uri]), "none", time.time(), time.time()),
                    )
                    request_id = oauth.start(
                        {
                            "response_type": "code",
                            "client_id": client_id,
                            "redirect_uri": redirect_uri,
                            "code_challenge": "A" * 43,
                            "code_challenge_method": "S256",
                            "resource": origin,
                            "scope": "quant.read offline_access",
                            "state": "ui-edge",
                        },
                        "127.0.0.1",
                    )
                    anonymous = browser.new_context(locale="en", viewport={"width": 1280, "height": 900})
                    anonymous.route("**/*", lambda route: route.continue_() if route.request.url.startswith(origin + "/") else route.abort())
                    oauth_page = anonymous.new_page()
                    oauth_page.on("pageerror", lambda error: results["page_errors"].append(str(error)))
                    oauth_page.goto(origin + "/oauth/authorize?request_id=" + request_id + "&lang=en")
                    expect(oauth_page.locator("h1")).to_have_text("Connect an agent")
                    expect(oauth_page.locator("main")).to_contain_text("Read your Quant reports")
                    oauth_page.locator("#oauth-email").fill("oauth-edge@example.test")
                    oauth_page.locator("#oauth-login-form button[type=submit]").click()
                    expect(oauth_page.locator("#oauth-status")).to_contain_text("Code sent to oauth-edge@example.test")
                    oauth_page.locator('[data-language="zh-CN"]').click()
                    expect(oauth_page.locator("h1")).to_have_text("连接 Agent")
                    expect(oauth_page.locator("#oauth-status")).to_contain_text("验证码已发送至 oauth-edge@example.test")
                    assert oauth_page.locator("#oauth-email").input_value() == "oauth-edge@example.test"
                    anonymous.close()
                    results["checks"].append("OAuth static text and dynamic code-sent status switch language while preserving the entered email")
                    page.locator("#logout").click()
                    expect(page.locator("#login")).to_be_visible()
                    page.goto(origin+"/app?"+urlencode({"next":"https://untrusted.example.test/read/"+item["id"],"lang":"en"}))
                    login(page)
                    expect(page.locator("#account")).to_be_visible()
                    assert urlsplit(page.url).netloc==urlsplit(origin).netloc and urlsplit(page.url).path=="/app"
                    results["checks"].append("Untrusted next URL is ignored after OTP login")
                    assert not results["page_errors"]
                finally:
                    browser.close()
        finally:
            server.should_exit=True
            thread.join(timeout=10)
            sock.close()
            results["owned_server_stopped"]=not thread.is_alive()
            assert not thread.is_alive()
            output=Path(__file__).resolve().parents[2]/"test-results"/"ux"/"edge-report.json"
            output.parent.mkdir(parents=True,exist_ok=True)
            output.write_text(json.dumps(results,ensure_ascii=False,indent=2))
            print(json.dumps(results,ensure_ascii=False))


if __name__=="__main__":
    run()
