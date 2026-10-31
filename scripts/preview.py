"""Read-only Paseo preview. Renders public templates without opening the app or its data."""
from __future__ import annotations

import json
import encodings.idna
import encodings.latin_1
import os
import pwd
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from jinja2 import Environment, FileSystemLoader, select_autoescape
from starlette.requests import Request
from hadan.i18n import get_language, template_context


def build_pages():
    templates = Environment(
        loader=FileSystemLoader([ROOT / "src/hadan/templates", ROOT / "scripts/preview-templates"]),
        autoescape=select_autoescape(["html"]),
    )
    pages = {}
    notes = {
        "zh-CN": "交互演示 · 未实际订阅、付款或绑定",
        "en": "Interactive demo · No real subscription, payment or connection",
    }
    demo_words = {
        "demo_chat_title": ("Agent 会怎样帮你订阅", "How your agent subscribes"),
        "demo_chat_note": ("模拟对话，可切换接收能力和对话语言。", "A simulated conversation. Try different capabilities and languages."),
        "demo_capability": ("Agent 的能力", "Agent capabilities"),
        "demo_auto": ("支持后台任务", "Background tasks available"),
        "demo_ondemand": ("只能按需执行", "On-demand execution only"),
        "demo_browse": ("只能浏览网页", "Web browsing only"),
        "demo_language": ("对话语言", "Conversation language"),
        "demo_follow": ("跟随页面语言", "Follow page language"),
        "demo_restart": ("重新开始", "Restart"),
        "demo_toolbar": ("交互预览", "Interactive preview"),
        "demo_email_path": ("邮箱流程", "Email flow"),
        "demo_agent_path": ("Agent 流程", "Agent flow"),
        "demo_pages": ("页面总览", "Pages"),
        "demo_codes": ("模拟验证码 123456 · 开通码 QUANT-DEMO · Agent 配对码 DEADBEEF", "Demo email code 123456 · Access code QUANT-DEMO · Agent pairing code DEADBEEF"),
    }
    gallery_names = {
        "zh-CN": "查看其他页面截图",
        "en": "View other page screenshots",
    }
    gallery = templates.from_string('''{% extends "base.html" %}{% block body %}
    <section class="reader"><h1>{{gallery_title}}</h1><p>{{gallery_note}}</p>
    {% for name,label in shots %}<h2>{{label}}</h2><a href="/preview-assets/{{name}}.png">
    <img src="/preview-assets/{{name}}.png" alt="{{label}}" width="100%" loading="lazy"></a>{% endfor %}
    </section>{% endblock %}''')
    shot_labels = [
        ("preview-desktop-home", "中文首页", "Chinese homepage"),
        ("preview-desktop-home-en", "英文首页", "English homepage"),
        ("preview-mobile-home", "手机首页", "Mobile homepage"),
        ("preview-subscription-delivery", "选择邮箱或 Agent", "Choose email or agent"),
        ("preview-agent-authorization", "确认 Agent 授权", "Authorize an agent"),
        ("preview-agent-conversation", "Agent 对话演示", "Agent conversation demo"),
        ("preview-desktop-content", "报告与附件", "Report and attachments"),
        ("preview-desktop-admin", "管理后台", "Admin dashboard"),
    ]
    available_shots = [
        (name, zh, en) for name, zh, en in shot_labels
        if (ROOT / "docs/assets" / (name + ".png")).is_file()
    ]
    for lang in ("zh-CN", "en"):
        for path, name in (("/", "index.html"), ("/app", "app.html"), ("/agent", "agent.html"), ("/terms", "terms.html"), ("/preview", None), ("/preview/agent", "agent-demo.html"), ("/read/00000000000000000000000000000001", "report-demo.html")):
            request = Request({"type": "http", "scheme": "http", "path": path,
                "query_string": ("lang=" + lang).encode(), "headers": [],
                "server": ("preview.invalid", 80)})
            context = template_context(request)
            context.update(development=False, products=[], base_url="", support_contact="support@example.invalid",
                gallery_title=gallery_names[lang],
                gallery_note=notes[lang] + ("" if available_shots else (" · 未随仓库提供历史截图。" if lang == "zh-CN" else " · Historical screenshots are not included in this checkout.")),
                shots=[(n, zh if lang == "zh-CN" else en) for n, zh, en in available_shots])
            context["ui_strings"] = {key: {**strings, "preview_note": notes[key],
                "preview_gallery": gallery_names[key], **{name: pair[0 if key == "zh-CN" else 1] for name, pair in demo_words.items()}} for key, strings in context["ui_strings"].items()}
            original_translate = context["t"]
            def preview_t(key, lang=lang, original=original_translate, **values):
                text = demo_words[key][0 if lang == "zh-CN" else 1] if key in demo_words else original(key)
                return text.format(**values) if values else text
            context["t"] = preview_t
            html = (templates.get_template(name) if name else gallery).render(**context)
            html = html.replace('<body data-page=', '<body data-preview="true" data-page=', 1)
            html = html.replace('</head>', '<link rel="stylesheet" href="/preview-assets/preview.css"></head>', 1)
            toolbar = '<aside class="demo-toolbar"><span data-i18n="demo_toolbar">' + context["t"]("demo_toolbar") + '</span><nav><a href="/app?flow=subscribe" data-i18n="demo_email_path">' + context["t"]("demo_email_path") + '</a><a href="/preview/agent" data-i18n="demo_agent_path">' + context["t"]("demo_agent_path") + '</a><a href="/preview" data-i18n="demo_pages">' + context["t"]("demo_pages") + '</a></nav></aside>'
            html = html.replace('<header class="top">', toolbar + '<header class="top">', 1)
            if path != "/":
                banner = '<aside class="demo-status" role="note"><strong data-i18n="preview_note">' + notes[lang] + '</strong>'
                if path == "/app":
                    banner += '<p data-i18n="demo_codes">' + context["t"]("demo_codes") + '</p>'
                html = html.replace('<main>', '<main>' + banner + '</aside>', 1)
            if path == "/app":
                html = html.replace('<script src="/static/app.js" defer>', '<script src="/preview-assets/app-simulator.js" defer></script><script src="/static/app.js" defer>', 1)
            if path == "/preview/agent":
                html = html.replace('</head>', '<script src="/preview-assets/chat.js" defer></script></head>', 1)
            pages[(path, lang)] = html.encode()
    assets = {}
    for name, mime in (("style.css", "text/css"), ("quant-latin.woff2", "font/woff2"), ("quant-cjk-regular.woff2", "font/woff2"), ("quant-cjk-medium.woff2", "font/woff2"), ("landing.css", "text/css"), ("app.js", "text/javascript"), ("quant-sample.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"), ("quant-sample.png", "image/png"), ("quant-sample-preview.png", "image/png"), ("quant-sample-mobile.png", "image/png")):
        assets["/static/" + name] = ((ROOT / "src/hadan/static" / name).read_bytes(), mime)
    for route, filename, mime in (("/preview-assets/app-simulator.js", "preview-app.js", "text/javascript"), ("/preview-assets/chat.js", "preview-chat.js", "text/javascript"), ("/preview-assets/preview.css", "preview.css", "text/css")):
        assets[route] = ((ROOT / "scripts" / filename).read_bytes(), mime)
    preview_rule = "\n\nEnvironment: READ-ONLY PREVIEW. This origin cannot complete email verification, pairing, subscriptions or payments. Explain that briefly in the user's language and stop before API writes. Do not substitute a different service origin.\n\n"
    for route, filename, mime in (("/skill.md", "SKILL.md", "text/markdown"), ("/agent/instructions", "agent-instructions.txt", "text/plain")):
        document = (ROOT / "src/hadan/static" / filename).read_text().replace("{{BASE_URL}}", "")
        if document.startswith("---\n"):
            boundary = document.index("\n---", 4) + 4
            document = document[:boundary] + preview_rule + document[boundary:]
        else:
            document = preview_rule + document
        assets[route] = (document.encode(), mime)
    assets["/llms.txt"] = ((preview_rule + "# Quant\n\n- [Agent Skill](/skill.md): public onboarding instructions.\n- [Human flow demo](/app?flow=subscribe)\n- [Agent conversation demo](/preview/agent)\n").encode(), "text/plain")
    for name, _, _ in available_shots:
        path = ROOT / "docs/assets" / (name + ".png")
        if path.is_file():
            assets["/preview-assets/" + name + ".png"] = (path.read_bytes(), "image/png")
    return pages, assets


def main():
    port = int(os.environ.get("PASEO_PORT", "8098"))
    if not 1024 <= port <= 65535:
        raise ValueError("Preview requires an unprivileged port")
    pages, assets = build_pages()
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def log_message(self, *args):
            pass  # Do not retain browser input, query strings or email addresses.

        def send(self, body, mime="text/html", status=200):
            self.send_response(status)
            self.send_header("Content-Type", mime + ("; charset=utf-8" if mime.startswith("text/") else ""))
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Robots-Tag", "noindex, nofollow")
            self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'; connect-src 'none'; form-action 'none'")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            if self.command != "HEAD":
                self.wfile.write(body)

        def do_GET(self):
            parsed = urlsplit(self.path)
            request = Request({"type": "http", "scheme": "http", "path": parsed.path,
                "query_string": parsed.query.encode(), "headers": [
                    (name.lower().encode(), value.encode()) for name, value in self.headers.items()]})
            lang = get_language(request)
            if parsed.path in assets:
                return self.send(*assets[parsed.path])
            if (parsed.path, lang) in pages:
                return self.send(pages[(parsed.path, lang)])
            if parsed.path == "/api/me":
                return self.send(b'{"error":"Preview only"}', "application/json", 401)
            return self.send(b"Page not available in this preview", "text/plain", 404)

        do_HEAD = do_GET

        def do_POST(self):
            # No request bodies are read, stored, forwarded, or acted upon.
            message = ("Read-only preview. Sign-in, email and payments are not enabled."
                if self.headers.get("Accept-Language", "").lower().startswith("en")
                else "这里只提供页面预览，登录、发信和付款尚未开放。")
            self.send(json.dumps({"error": message}).encode(), "application/json", 503)

        do_PUT = do_POST
        do_PATCH = do_POST
        do_DELETE = do_POST

    class PreviewServer(ThreadingHTTPServer):
        slots = threading.BoundedSemaphore(8)

        def process_request(self, request, client_address):
            if not self.slots.acquire(blocking=False):
                self.shutdown_request(request)
                return
            try:
                super().process_request(request, client_address)
            except BaseException:
                self.slots.release()
                raise

        def process_request_thread(self, request, client_address):
            try:
                super().process_request_thread(request, client_address)
            finally:
                self.slots.release()

    server = PreviewServer(("127.0.0.1", port), Handler)
    # All responses and codecs are loaded before dropping access to the root-owned checkout.
    # No private configuration or database is loaded.
    if os.geteuid() == 0:
        account = pwd.getpwnam("nobody")
        os.chdir("/tmp")
        os.setgroups([])
        os.setgid(account.pw_gid)
        os.setuid(account.pw_uid)
    print(f"Quant read-only preview: http://127.0.0.1:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
