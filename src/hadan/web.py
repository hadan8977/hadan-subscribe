from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import time
from pathlib import Path
from urllib.parse import urlsplit, urlencode

import bleach
from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .config import Settings
from .db import audit, enqueue, uid
from .mcp_events import CallbackError, MCPEvents
from .mcp_tools import TOOL_SCOPES, TOOLS, ToolArgumentError, validate_arguments
from .oauth import OAuth, OAuthError
from .security import digest, mac, token
from .service import Problem, Service
from .i18n import template_context, translate_error, get_language


def create_app(settings=None):
    s = Service(settings or Settings())
    cfg = s.settings
    oauth = OAuth(s)
    mcp_events = MCPEvents(s)
    diagnostic_logger = None
    if cfg.production:
        diagnostic_logger = logging.getLogger("hadan.oauth.diagnostics")
        if not diagnostic_logger.handlers:
            handler = logging.FileHandler(
                cfg.data_dir / "oauth-diagnostics.log", encoding="utf-8"
            )
            handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
            diagnostic_logger.addHandler(handler)
            diagnostic_logger.setLevel(logging.INFO)
            diagnostic_logger.propagate = False
    app = FastAPI(
        title="Quant", docs_url=None, redoc_url=None, openapi_url=None
    )
    app.state.service = s
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=[urlsplit(cfg.base_url).hostname]
        + ([] if cfg.production else ["localhost", "127.0.0.1", "testserver"]),
    )
    root = Path(__file__).parent
    app.mount("/static", StaticFiles(directory=root / "static"), name="static")
    templates = Jinja2Templates(directory=root / "templates", context_processors=[template_context])
    templates.env.globals["mail_sender"] = cfg.mail_sender
    templates.env.globals["support_contact"] = cfg.support_contact

    @app.exception_handler(Problem)
    async def problem_handler(request, exc):
        if request.url.path.startswith(("/read/", "/unsubscribe/")):
            return templates.TemplateResponse(
                request=request,
                name="message.html",
                context={
                    "message": translate_error(exc.message, request),
                    "heading": "Content unavailable" if get_language(request) == "en" else "内容暂不可用",
                    "eyebrow": "PLEASE CHECK",
                },
                status_code=exc.status,
            )
        body = {"error": translate_error(exc.message, request)}
        if getattr(exc, "data", None) is not None:
            body["data"] = exc.data
        return JSONResponse(body, status_code=exc.status)

    @app.exception_handler(OAuthError)
    async def oauth_handler(request, exc):
        if request.url.path == "/oauth/authorize" and request.method == "GET":
            return templates.TemplateResponse(
                request=request,
                name="oauth.html",
                context={
                    "oauth_error": translate_error(exc.description or exc.error, request),
                    "oauth": None,
                },
                status_code=exc.status,
            )
        if diagnostic_logger is not None:
            diagnostic_logger.info(
                json.dumps(
                    {
                        "oauth_error": exc.error,
                        "oauth_error_description": exc.description[:300],
                        "path": request.url.path,
                        "status": exc.status,
                    },
                    ensure_ascii=False,
                )
            )
        return JSONResponse(
            {"error": exc.error, "error_description": exc.description},
            status_code=exc.status,
            headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
        )

    @app.exception_handler(ValueError)
    async def value_handler(request, exc):
        if request.url.path.startswith(("/read/", "/unsubscribe/")):
            return templates.TemplateResponse(
                request=request,
                name="message.html",
                context={
                    "message": translate_error(str(exc)[:200], request),
                    "heading": "Link unavailable" if get_language(request) == "en" else "链接暂不可用",
                    "eyebrow": "PLEASE CHECK",
                },
                status_code=400,
            )
        return JSONResponse({"error": translate_error(str(exc)[:200], request)}, status_code=400)

    @app.middleware("http")
    async def boundaries(request, call_next):
        cors_path = (
            request.url.path in ("/mcp", "/mcp/")
            or request.url.path.startswith("/.well-known/oauth-")
            or request.url.path in ("/oauth/register", "/oauth/token", "/oauth/revoke")
        )
        if cors_path and request.method == "OPTIONS":
            return Response(
                status_code=204,
                headers={
                    "Access-Control-Allow-Origin": "*",
                    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                    "Access-Control-Allow-Headers": "Authorization, Content-Type, MCP-Protocol-Version, Mcp-Method, Mcp-Name",
                    "Access-Control-Max-Age": "600",
                },
            )
        if request.url.path == "/api/admin/publish" and request.method == "POST":
            try:
                user(request, write=True, admin=True)
            except Problem as exc:
                return JSONResponse({"error": translate_error(exc.message, request)}, status_code=exc.status)
        maximum = (
            24 * 1024 * 1024
            if request.url.path == "/api/admin/publish"
            else 1024 * 1024
        )
        try:
            length = int(request.headers.get("content-length", "0") or "0")
        except ValueError:
            return JSONResponse({"error": translate_error("Content-Length 无效", request)}, status_code=400)
        if length < 0:
            return JSONResponse({"error": translate_error("Content-Length 无效", request)}, status_code=400)
        if length > maximum:
            return JSONResponse({"error": translate_error("请求过大", request)}, status_code=413)
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            chunks = []
            size = 0
            async for chunk in request.stream():
                size += len(chunk)
                if size > maximum:
                    return JSONResponse({"error": translate_error("请求过大", request)}, status_code=413)
                chunks.append(chunk)
            request._body = b"".join(chunks)
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            origin = request.headers.get("origin")
            if (
                origin
                and origin != cfg.base_url
                and not request.url.path.startswith("/unsubscribe/")
                and not request.url.path.startswith(
                    ("/mcp", "/oauth/token", "/oauth/register", "/oauth/revoke")
                )
            ):
                return JSONResponse({"error": translate_error("请求来源不匹配", request)}, status_code=403)
        response = await call_next(request)
        if diagnostic_logger is not None and (
            cors_path or request.url.path.startswith("/oauth/authorize")
        ):
            diagnostic_logger.info(
                json.dumps(
                    {
                        "method": request.method,
                        "path": request.url.path,
                        "status": response.status_code,
                        "ua": request.headers.get("user-agent", "")[:200],
                        "origin": request.headers.get("origin", "")[:200],
                        "accept": request.headers.get("accept", "")[:200],
                        "content_type": request.headers.get("content-type", "")[:120],
                        "mcp_protocol_version": request.headers.get("mcp-protocol-version", "")[:40],
                        "mcp_method": getattr(request.state, "mcp_method", "") or request.headers.get("mcp-method", "")[:80],
                        "mcp_name": request.headers.get("mcp-name", "")[:80],
                        "auth": (
                            request.headers.get("authorization", "").split(" ", 1)[0][:20]
                            if request.headers.get("authorization")
                            else ""
                        ),
                    },
                    ensure_ascii=False,
                )
            )
        if cors_path:
            response.headers["Access-Control-Allow-Origin"] = "*"
            response.headers["Access-Control-Expose-Headers"] = "WWW-Authenticate, Mcp-Session-Id"
        response.headers["Content-Language"] = get_language(request)
        if request.query_params.get("lang") in ("en", "zh", "zh-CN"):
            response.set_cookie("quant_lang", get_language(request), max_age=365*86400, samesite="lax", secure=cfg.production)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self' https://chatgpt.com https://chat.openai.com"
        )
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=()"
        )
        response.headers["Cache-Control"] = (
            "no-store"
            if not request.url.path.startswith("/static/")
            else "public,max-age=3600"
        )
        if cfg.production:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    async def payload(request, *, any_content_type=False):
        if (
            not any_content_type
            and not request.headers.get("content-type", "").startswith("application/json")
        ):
            raise Problem("需要 JSON 请求", 415)
        raw = await request.body()
        if len(raw) > 24 * 1024 * 1024:
            raise Problem("请求过大", 413)
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise Problem("JSON 无效")
        if not isinstance(data, dict):
            raise Problem("需要 JSON 对象")
        return data

    async def oauth_payload(request):
        content_type = request.headers.get("content-type", "").split(";", 1)[0].strip()
        if content_type == "application/json":
            return await payload(request)
        if content_type in ("application/x-www-form-urlencoded", "multipart/form-data"):
            form = await request.form()
            return {key: str(value) for key, value in form.items()}
        raise OAuthError("invalid_request", "Unsupported content type", 415)

    def user(request, *, write=False, admin=False):
        session = request.cookies.get("hadan_session", "")
        result = s.session_user(session)
        if write and not hmac.compare_digest(
            request.headers.get("x-csrf-token", ""), mac(cfg.secret, "csrf:" + session)
        ):
            raise Problem("页面已过期，请刷新后重试", 403)
        if admin and not result["is_admin"]:
            raise Problem("需要管理员权限", 403)
        return result

    def agent(request, pending=False):
        header = request.headers.get("authorization", "")
        if not header.startswith("Bearer "):
            raise Problem("需要 Agent 凭证", 401)
        # The HTTPS agent API is for device credentials. OAuth clients use the
        # MCP endpoint, where scopes and binding checks are enforced together.
        return s.agent(header[7:], pending=pending)

    def mcp_binding(request, *, require_subscription=True, pending=False):
        header = request.headers.get("authorization", "")
        if not header.startswith("Bearer "):
            raise Problem("需要 Agent 凭证", 401)
        raw = header[7:]
        try:
            return s.agent(raw, pending=pending)
        except Problem as exc:
            if exc.status != 401:
                raise
            return s.oauth_identity(raw, require_subscription=require_subscription)

    def principal(request):
        if request.headers.get("authorization"):
            b = agent(request)
            return b["user_id"], True
        return user(request)["id"], False

    def require_agent_product(user_id, product_id):
        sub = s.entitled(user_id, product_id)
        if not sub or not sub["agent_enabled"]:
            raise Problem("该内容的 Agent 交付已关闭", 403)

    @app.get("/health/live")
    def live():
        return {"status": "ok"}

    @app.get("/health/ready")
    def ready():
        try:
            s.db.one("SELECT 1")
            return {"status": "ready"}
        except Exception:
            return JSONResponse({"status": "unavailable"}, 503)

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request):
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={"development": not cfg.production},
        )

    @app.get("/app", response_class=HTMLResponse)
    def dashboard(request: Request):
        return templates.TemplateResponse(
            request=request,
            name="app.html",
            context={"development": not cfg.production},
        )

    @app.get("/terms", response_class=HTMLResponse)
    def terms_page(request: Request):
        return templates.TemplateResponse(request=request, name="terms.html", context={})

    @app.get("/admin", response_class=HTMLResponse)
    def admin_page(request: Request):
        try:
            user(request, admin=True)
        except Problem:
            return RedirectResponse("/app", 303)
        return templates.TemplateResponse(
            request=request, name="admin.html", context={}
        )

    @app.get("/read/{cid}", response_class=HTMLResponse)
    def read(request: Request, cid: str, version: int | None = None):
        if version is not None and not 1 <= version <= 1_000_000_000:
            raise Problem("内容版本无效", 400)
        try:
            u = user(request)
        except Problem:
            target = "/read/" + cid + ("?version=" + str(version) if version is not None else "")
            return RedirectResponse("/app?" + urlencode({"next": target, "lang": get_language(request)}), 303)
        c = s.content(u["id"], cid, version)
        history = s.db.all(
            "SELECT number,changes,created_at FROM versions WHERE content_id=? ORDER BY number DESC",
            (cid,),
        )
        return templates.TemplateResponse(
            request=request,
            name="read.html",
            context={"content": c, "history": history},
        )

    @app.post("/api/auth/request")
    async def auth_request(request: Request):
        p = await payload(request)
        return s.request_code(str(p.get("email", "")), request.client.host)

    @app.post("/api/auth/verify")
    async def auth_verify(request: Request):
        p = await payload(request)
        session = s.verify_code(
            str(p.get("email", "")), str(p.get("code", "")), request.client.host
        )
        me = s.session_user(session)
        result = {"ok": True}
        active = s.db.one(
            "SELECT id FROM bindings WHERE user_id=? AND state='active'", (me["id"],)
        )
        # Email verification is the owner's consent. On a first connection,
        # finish the waiting OAuth/pairing authorization without a second
        # button. Replacing an existing active connection still asks.
        oauth_request_id = p.get("oauth_request_id")
        if isinstance(oauth_request_id, str) and oauth_request_id:
            try:
                authorization = oauth.request(oauth_request_id)
                if active or not oauth.auto_approve_allowed(authorization["redirect_uri"]):
                    result["oauth_requires_approval"] = True
                else:
                    result["oauth_redirect"] = oauth.approve(oauth_request_id, me["id"])
            except OAuthError as exc:
                result["oauth_error"] = exc.description or exc.error
        pair_code = p.get("pair_code")
        if isinstance(pair_code, str) and pair_code:
            if active:
                result["pair_requires_approval"] = True
            else:
                try:
                    result["pair_binding_id"] = s.approve_pair(me["id"], pair_code)["binding_id"]
                except Problem as exc:
                    result["pair_error"] = exc.message
        response = JSONResponse(result)
        response.set_cookie(
            "hadan_session",
            session,
            httponly=True,
            secure=cfg.production,
            samesite="lax",
            max_age=cfg.session_seconds,
            path="/",
        )
        return response

    @app.post("/api/auth/logout")
    def logout(request: Request):
        user(request, write=True)
        s.db.execute(
            "DELETE FROM sessions WHERE digest=?",
            (digest(request.cookies["hadan_session"]),),
        )
        response = JSONResponse({"ok": True})
        response.delete_cookie("hadan_session")
        return response

    @app.get("/api/me")
    def me(request: Request):
        u = user(request)
        return {
            "user": u,
            "csrf": mac(cfg.secret, "csrf:" + request.cookies["hadan_session"]),
            "subscriptions": s.subscriptions(u["id"]),
            "bindings": s.db.all(
                "SELECT id,name,mode,state,test_ok,created_at,last_seen FROM bindings WHERE user_id=? ORDER BY created_at DESC",
                (u["id"],),
            ),
            "products": s.db.all(
                "SELECT id,name,description,paid FROM products WHERE active=1"
            ),
        }

    @app.post("/api/subscriptions")
    async def subscribe(request: Request):
        u = user(request, write=True)
        p = await payload(request)
        return s.subscribe(u["id"], str(p.get("product_id", "")))

    @app.patch("/api/subscriptions/{sid}")
    async def prefs(request: Request, sid: str):
        u = user(request, write=True)
        s.preferences(u["id"], sid, await payload(request))
        return {"ok": True}

    @app.post("/api/subscriptions/{sid}/cancel")
    def cancel(request: Request, sid: str):
        return s.cancel_subscription(user(request, write=True)["id"], sid)

    @app.get("/unsubscribe/{capability}", response_class=HTMLResponse)
    def unsubscribe_page(request: Request, capability: str):
        from .security import action_id

        action_id(cfg.secret, capability)
        return templates.TemplateResponse(
            request=request, name="unsubscribe.html", context={"capability": capability}
        )

    @app.post("/unsubscribe/{capability}")
    async def unsubscribe(request: Request, capability: str):
        # RFC 8058 endpoint; intentionally no session/CSRF, only a single-action capability.
        body = await request.body()
        if body not in (b"List-Unsubscribe=One-Click", b"confirm=1"):
            raise Problem("请确认退订")
        result = s.unsubscribe_link(capability)
        if body == b"confirm=1":
            return templates.TemplateResponse(
                request=request,
                name="message.html",
                context={"message": result["message"]},
            )
        return result

    @app.post("/api/bindings")
    async def binding_create(request: Request):
        u = user(request, write=True)
        p = await payload(request)
        return await run_in_threadpool(
            s.new_binding,
            u["id"],
            p.get("name", ""),
            p.get("mode", "poll"),
            p.get("endpoint"),
        )

    @app.post("/api/bindings/{bid}/activate")
    def binding_activate(request: Request, bid: str):
        s.activate(user(request, write=True)["id"], bid)
        return {"ok": True}

    @app.delete("/api/bindings/{bid}")
    def binding_revoke(request: Request, bid: str):
        s.revoke(user(request, write=True)["id"], bid)
        return {"ok": True}

    @app.post("/api/agent/pair")
    async def pair_start(request: Request):
        p = await payload(request)
        return s.pair(
            p.get("name", "Personal Agent"),
            p.get("mode", "poll"),
            None,
            request.client.host,
        )

    @app.post("/api/pair/approve")
    async def pair_approve(request: Request):
        u = user(request, write=True)
        p = await payload(request)
        return s.approve_pair(u["id"], str(p.get("code", "")))

    @app.get("/api/pair/details")
    def pair_details(request: Request, code: str = ""):
        u = user(request)
        # Invalid guesses consume the budget even when lookup subsequently fails.
        with s.db.transaction() as db:
            s._limit(db, "pair-details:" + u["id"], 30, 600)
        if not re.fullmatch(r"[0-9A-Fa-f]{8}", code):
            raise Problem("配对码已过期或已使用", 409)
        row = s.db.one(
            "SELECT name,expires_at FROM pairings WHERE code=? AND expires_at>? AND binding_id IS NULL",
            (code.upper(), time.time()),
        )
        if not row:
            raise Problem("配对码已过期或已使用", 409)
        return {"name": row["name"], "expires_at": row["expires_at"]}

    @app.post("/api/agent/pair/status")
    async def pair_status(request: Request):
        p = await payload(request)
        row = s.db.one(
            "SELECT * FROM pairings WHERE id=? AND secret_digest=? AND expires_at>?",
            (p.get("pair_id"), digest(str(p.get("device_secret", ""))), time.time()),
        )
        if not row:
            raise Problem("配对请求不存在或已过期", 404)
        return {
            "state": "approved" if row["binding_id"] else "pending",
            "binding_id": row["binding_id"],
            "credential": "Use device_secret as your Bearer token after approval",
        }

    @app.get("/api/agent/test")
    def agent_test(request: Request):
        b = agent(request, pending=True)
        s.db.execute("UPDATE bindings SET test_read=1 WHERE id=?", (b["id"],))
        return {
            "title": "Connection test",
            "body": "Download the test attachment and confirm it before this connection can be enabled.",
            "attachment": "/api/agent/test-file",
        }

    @app.get("/api/agent/status")
    def agent_status(request: Request):
        # A device secret has no binding until the verified owner approves it.
        b = agent(request, pending=True)
        sub = s.db.one(
            "SELECT status,expires_at,email_enabled,agent_enabled FROM subscriptions WHERE user_id=? AND product_id='quantgt'",
            (b["user_id"],),
        )
        entitled = bool(sub and sub["status"] == "active" and
                        (sub["expires_at"] is None or sub["expires_at"] > time.time()))
        state = sub["status"] if sub else "none"
        if state == "active" and not entitled:
            state = "expired"
        return {
            "binding": {"state": b["state"], "mode": b["mode"]},
            "subscription": {
                "status": state,
                "expires_at": sub["expires_at"] if sub else None,
                "email_enabled": bool(sub and sub["email_enabled"]),
                "agent_enabled": bool(sub and sub["agent_enabled"]),
            },
            "entitled": entitled,
            "ready": bool(entitled and b["state"] == "active" and sub["agent_enabled"]),
        }

    @app.get("/api/agent/test-file")
    def agent_test_file(request: Request):
        b = agent(request, pending=True)
        s.db.execute("UPDATE bindings SET test_download=1 WHERE id=?", (b["id"],))
        return Response(
            b"Hadan private attachment test\n",
            media_type="text/plain",
            headers={
                "Content-Disposition": 'attachment; filename="connection-test.txt"'
            },
        )

    @app.post("/api/agent/test/complete")
    def agent_test_complete(request: Request):
        b = agent(request, pending=True)
        if (
            not b["test_read"]
            or not b["test_download"]
            or (b["mode"] == "webhook" and not b["test_hook"])
        ):
            raise Problem("正文或附件测试尚未完成", 409)
        s.db.execute("UPDATE bindings SET test_ok=1 WHERE id=?", (b["id"],))
        s.activate(b["user_id"], b["id"])
        return {"state": "active", "message": "Connection active; the previous default connection was revoked."}

    @app.get("/api/agent/schedule")
    def agent_schedule(request: Request):
        mcp_binding(request, require_subscription=False)
        from .schedule_tool import upcoming_scans

        return upcoming_scans()

    @app.get("/api/events")
    def events(
        request: Request,
        after: int = 0,
        limit: int = 50,
        pending: bool = False,
        newest: bool = False,
        before: int | None = None,
        include_webhook: bool = False,
    ):
        user_id, is_agent = principal(request)
        binding_id = agent(request)["id"] if request.headers.get("authorization") else None
        rows = s.events(
            user_id,
            after,
            limit,
            pending,
            agent=is_agent,
            newest=newest,
            before=before,
            binding_id=binding_id,
            include_webhook=include_webhook,
        )
        return {
            "events": rows,
            "next_cursor": max((r["seq"] for r in rows), default=after),
            "next_before": rows[-1]["seq"] if rows else None,
        }

    @app.get("/api/library")
    def library(
        request: Request,
        before: float | None = None,
        before_id: str | None = None,
        limit: int = 50,
    ):
        user_id, is_agent = principal(request)
        params = [user_id, time.time()]
        if before is not None and before_id:
            cursor = " AND (v.created_at<? OR (v.created_at=? AND c.id<?))"
            params.extend((before, before, before_id))
        else:
            cursor = " AND v.created_at<?"
            params.append(before if before is not None else time.time() + 1)
        params.append(min(max(limit, 1), 100))
        rows = s.db.all(
            "SELECT c.id,c.product_id,v.title,v.number version,v.created_at FROM contents c JOIN versions v ON v.content_id=c.id AND v.number=c.current_version JOIN subscriptions sub ON sub.product_id=c.product_id WHERE sub.user_id=? AND sub.status='active' AND (sub.expires_at IS NULL OR sub.expires_at>?) AND c.withdrawn=0 AND c.source_key NOT LIKE 'test:%'"
            + cursor
            + (" AND sub.agent_enabled=1" if is_agent else "")
            + " ORDER BY v.created_at DESC,c.id DESC LIMIT ?",
            params,
        )
        return {
            "contents": rows,
            "next_before": rows[-1]["created_at"] if rows else None,
            "next_before_id": rows[-1]["id"] if rows else None,
        }

    @app.get("/api/content/{cid}")
    def content(request: Request, cid: str, version: int | None = None):
        user_id, is_agent = principal(request)
        c = s.content(user_id, cid, version)
        if is_agent:
            require_agent_product(user_id, c["product_id"])
        c.pop("html", None)
        return c

    @app.get("/api/files/{aid}")
    def file(request: Request, aid: str):
        user_id, is_agent = principal(request)
        a, path = s.attachment(user_id, aid)
        if is_agent:
            require_agent_product(
                user_id, s.content(user_id, a["content_id"])["product_id"]
            )
        return FileResponse(
            path,
            media_type="application/octet-stream",
            filename=a["name"],
            content_disposition_type="attachment",
        )

    @app.post("/api/events/{event_id}/ack")
    def ack(request: Request, event_id: str):
        return s.ack(agent(request), event_id)

    @app.get("/agent", response_class=HTMLResponse)
    def agent_guide(request: Request):
        return templates.TemplateResponse(
            request=request, name="agent.html", context={"base_url": cfg.base_url}
        )

    @app.get("/llms.txt")
    def agent_discovery():
        return Response("# Quant\n\nPrivate portfolio, watchlist and weekly subscriptions for people and their agents.\n\n"
                        f"- [Subscribe]({cfg.base_url}/app?flow=subscribe): email verification and access.\n"
                        f"- [Agent Skill]({cfg.base_url}/skill.md): read first to subscribe or connect an existing agent; includes language and interaction rules.\n"
                        f"- [Agent Prompt]({cfg.base_url}/agent/prompt): copyable English prompt for the complete subscription flow.\n"
                        f"- [Protocol]({cfg.base_url}/agent/instructions): OAuth, MCP Events, pairing, reports, files and acknowledgements.\n",
                        media_type="text/plain", headers={"Cache-Control": "no-store"})

    @app.get("/agent/prompt")
    def agent_prompt():
        return Response(
            (root / "static" / "AGENT_PROMPT.md")
            .read_text()
            .replace("{{BASE_URL}}", cfg.base_url)
            .replace("{{MAIL_SENDER}}", cfg.mail_sender),
            media_type="text/markdown",
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/agent/instructions")
    def instructions():
        return Response(
            (root / "static" / "agent-instructions.txt")
            .read_text()
            .replace("{{BASE_URL}}", cfg.base_url)
            .replace("{{MAIL_SENDER}}", cfg.mail_sender),
            media_type="text/plain",
        )

    @app.get("/skill.md")
    def agent_skill():
        return Response(
            (root / "static" / "SKILL.md").read_text().replace("{{BASE_URL}}", cfg.base_url).replace("{{MAIL_SENDER}}", cfg.mail_sender),
            media_type="text/markdown",
        )

    @app.get("/.well-known/oauth-protected-resource")
    def oauth_protected_resource():
        return JSONResponse(oauth.protected_resource())

    @app.get("/.well-known/oauth-protected-resource/mcp")
    @app.get("/.well-known/oauth-protected-resource/mcp/")
    @app.get("/mcp/.well-known/oauth-protected-resource")
    @app.get("/mcp/.well-known/oauth-protected-resource/")
    def oauth_protected_resource_mcp():
        return JSONResponse(oauth.protected_resource(cfg.base_url.rstrip("/") + "/mcp"))

    @app.get("/.well-known/oauth-authorization-server")
    @app.get("/.well-known/oauth-authorization-server/mcp")
    @app.get("/.well-known/oauth-authorization-server/mcp/")
    @app.get("/mcp/.well-known/oauth-authorization-server")
    @app.get("/mcp/.well-known/oauth-authorization-server/")
    def oauth_metadata(request: Request):
        result = oauth.metadata()
        user_agent = request.headers.get("user-agent", "").lower()
        if any(marker in user_agent for marker in ("aiohttp", "openai-mcp", "chatgpt")):
            # ChatGPT's connector validator checks OIDC-shaped metadata even
            # for an OAuth-only server. These fields are compatibility hints;
            # no ID token is issued and userinfo is the identity source.
            result.update(
                {
                    "subject_types_supported": ["public"],
                    "id_token_signing_alg_values_supported": ["RS256"],
                    "userinfo_endpoint": cfg.base_url.rstrip("/") + "/oauth/userinfo",
                    "jwks_uri": cfg.base_url.rstrip("/") + "/.well-known/jwks.json",
                    "claims_supported": ["sub", "email", "email_verified"],
                }
            )
        return JSONResponse(result)

    @app.get("/oauth/userinfo")
    def oauth_userinfo(request: Request):
        header = request.headers.get("authorization", "")
        if header.startswith("Bearer "):
            try:
                binding = s.oauth_identity(header[7:], require_subscription=False)
            except Problem:
                binding = None
            if binding:
                user = s.db.one("SELECT email FROM users WHERE id=?", (binding["user_id"],))
                if user:
                    return {
                        "sub": binding["user_id"],
                        "email": user["email"],
                        "email_verified": True,
                    }
        # A 401 here is interpreted by some connectors as a failed login.
        return {"sub": "anonymous"}

    @app.get("/.well-known/jwks.json")
    def oauth_jwks():
        return {"keys": []}

    @app.post("/oauth/register")
    async def oauth_register(request: Request):
        data = await payload(request)
        if diagnostic_logger is not None:
            diagnostic_logger.info(
                json.dumps(
                    {
                        "dcr_request": {
                            "client_name": str(data.get("client_name", ""))[:120],
                            "redirect_uris": data.get("redirect_uris"),
                            "token_endpoint_auth_method": data.get("token_endpoint_auth_method"),
                            "grant_types": data.get("grant_types"),
                            "response_types": data.get("response_types"),
                            "scope": data.get("scope"),
                        }
                    },
                    ensure_ascii=False,
                )
            )
        result = oauth.register(
            data, request.client.host if request.client else "unknown"
        )
        if diagnostic_logger is not None:
            diagnostic_logger.info(
                json.dumps(
                    {
                        "dcr_response": {
                            key: result.get(key)
                            for key in (
                                "client_id",
                                "client_name",
                                "redirect_uris",
                                "token_endpoint_auth_method",
                                "grant_types",
                                "response_types",
                                "client_id_issued_at",
                            )
                        }
                    },
                    ensure_ascii=False,
                )
            )
        return JSONResponse(
            result,
            status_code=201,
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/oauth/authorize", response_class=HTMLResponse)
    def oauth_authorize(request: Request):
        request_id = request.query_params.get("request_id")
        if not request_id:
            request_id = oauth.start(
                dict(request.query_params),
                request.client.host if request.client else "unknown",
            )
            return RedirectResponse(
                "/oauth/authorize?" + urlencode({"request_id": request_id}), 303
            )
        row = oauth.request(request_id)
        session = request.cookies.get("hadan_session", "")
        try:
            current = s.session_user(session)
        except Problem:
            current = None
        active = (
            s.db.one(
                "SELECT name FROM bindings WHERE user_id=? AND state='active'",
                (current["id"],),
            )
            if current
            else None
        )
        # A signed-in owner already proved mailbox ownership. On a first
        # connection, finish the authorization without a second consent click.
        # Replacing an existing active connection, or an explicit manual link,
        # still shows the review page.
        if current and not active and request.query_params.get("manual") != "1" and oauth.auto_approve_allowed(row["redirect_uri"]):
            try:
                return RedirectResponse(oauth.approve(request_id, current["id"]), 303)
            except OAuthError:
                pass
        return templates.TemplateResponse(
            request=request,
            name="oauth.html",
            context={
                "oauth_request": row,
                "client_name": row["client_name"],
                "redirect_host": urlsplit(row["redirect_uri"]).hostname,
                "scopes": row["scope"].split(),
                "csrf": mac(cfg.secret, "csrf:" + session) if current else "",
                "signed_in": current,
                "current_binding": active,
                "oauth_error": None,
            },
        )

    @app.post("/oauth/authorize/decision")
    async def oauth_decision(request: Request):
        form = await request.form()
        request_id = str(form.get("request_id", ""))
        decision = str(form.get("decision", ""))
        session = request.cookies.get("hadan_session", "")
        current = user(request)
        supplied = str(form.get("csrf_token", ""))
        if not hmac.compare_digest(
            supplied, mac(cfg.secret, "csrf:" + session)
        ):
            raise Problem("页面已过期，请刷新后重试", 403)
        if decision == "deny":
            target = oauth.deny(request_id)
        elif decision == "approve":
            target = oauth.approve(request_id, current["id"])
        else:
            raise OAuthError("invalid_request", "decision is required")
        if request.headers.get("accept", "").startswith("application/json"):
            return JSONResponse(
                {"ok": True, "redirect": target},
                headers={"Cache-Control": "no-store"},
            )
        return RedirectResponse(target, 303)

    @app.post("/oauth/token")
    async def oauth_token(request: Request):
        data = await oauth_payload(request)
        authorization = request.headers.get("authorization", "")
        if not data.get("client_id") and authorization.startswith("Basic "):
            try:
                decoded = base64.b64decode(authorization[6:]).decode("utf-8")
                client_id, _, client_secret = decoded.partition(":")
                data["client_id"] = client_id
                data["client_secret"] = client_secret
            except (ValueError, UnicodeDecodeError):
                raise OAuthError("invalid_client", "Malformed client authentication", 401) from None
        result = oauth.token(data)
        if diagnostic_logger is not None:
            diagnostic_logger.info(
                json.dumps(
                    {
                        "oauth_token_issued": {
                            "grant_type": data.get("grant_type"),
                            "client_id": data.get("client_id"),
                            "scope": result.get("scope"),
                            "refresh_issued": bool(result.get("refresh_token")),
                            "expires_in": result.get("expires_in"),
                        }
                    },
                    ensure_ascii=False,
                )
            )
        return JSONResponse(
            result,
            headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
        )

    @app.post("/oauth/revoke")
    async def oauth_revoke(request: Request):
        data = await oauth_payload(request)
        oauth.revoke(data)
        return JSONResponse({}, headers={"Cache-Control": "no-store"})

    MCP_CHALLENGE = (
        'Bearer resource_metadata="'
        + cfg.base_url.rstrip("/")
        + '/.well-known/oauth-protected-resource/mcp"'
    )
    MCP_PUBLIC_METHODS = {"server/discover", "initialize", "ping", "tools/list", "events/list"}
    MCP_TOOL_CHALLENGE = (
        'Bearer resource_metadata="'
        + cfg.base_url.rstrip("/")
        + '/.well-known/oauth-protected-resource/mcp", error="insufficient_scope", '
        'error_description="Sign in to Quant to continue"'
    )

    def mcp_tool_challenge(identifier):
        # OpenAI's linking UI needs per-tool securitySchemes plus this tool-level
        # challenge. A bare transport 401 is not enough on some clients.
        return {
            "jsonrpc": "2.0",
            "id": identifier,
            "result": {
                "content": [
                    {"type": "text", "text": "Authentication required. Sign in to Quant to continue."}
                ],
                "isError": True,
                "_meta": {"mcp/www_authenticate": [MCP_TOOL_CHALLENGE]},
            },
        }

    def mcp_unauthorized():
        return JSONResponse(
            {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32001, "message": "Unauthorized"},
            },
            status_code=401,
            headers={"WWW-Authenticate": MCP_CHALLENGE},
        )

    BUSINESS_NEXT_ACTION = {
        "subscription_required": (
            "The owner's subscription is inactive or expired. Call "
            "get_subscription_status, present the live plan and terms, and ask "
            "the owner to confirm a checkout; do not reconnect another account."
        ),
        "agent_delivery_disabled": (
            "Agent delivery is off for this account. Ask the owner to enable it "
            "at " + cfg.base_url.rstrip("/") + "/app?view=connections."
        ),
        "access_denied": (
            "This account is not allowed to use the service. Do not switch "
            "accounts or bypass the refusal."
        ),
    }

    def mcp_tool_error(identifier, exc):
        """Return a business refusal without pretending the login expired."""
        code = getattr(exc, "code", None) or {
            400: "invalid_request",
            402: "payment_required",
            403: "forbidden",
            404: "not_found",
            409: "conflict",
            410: "gone",
            429: "rate_limited",
            503: "temporarily_unavailable",
        }.get(getattr(exc, "status", 400), "request_failed")
        text = translate_error(getattr(exc, "message", str(exc)), "en")
        next_action = BUSINESS_NEXT_ACTION.get(code)
        if next_action:
            text += " " + next_action
        result = {
            "isError": True,
            "content": [{"type": "text", "text": text}],
            "_meta": {
                "quant/error": {
                    "code": code,
                    "status": getattr(exc, "status", 400),
                    "next_action": next_action,
                }
            },
        }
        return {"jsonrpc": "2.0", "id": identifier, "result": result}

    def mcp_invalid_params(identifier, exc):
        return {
            "jsonrpc": "2.0",
            "id": identifier,
            "error": {
                "code": -32602,
                "message": "Invalid params",
                "data": {
                    "code": "invalid_params",
                    "field": getattr(exc, "field", None),
                    "message": getattr(exc, "message", str(exc)),
                },
            },
        }

    def mcp_unknown_tool(identifier):
        return {
            "jsonrpc": "2.0",
            "id": identifier,
            "error": {"code": -32601, "message": "Unknown tool"},
        }

    @app.get("/mcp")
    @app.get("/mcp/")
    def mcp_probe(request: Request):
        # ChatGPT probes the MCP URL with an anonymous GET before OAuth. A
        # 405 here makes connector validation fail before the consent page.
        # This is a closed, empty SSE probe and exposes no data.
        authorization = request.headers.get("authorization", "")
        if authorization:
            if not authorization.startswith("Bearer "):
                return mcp_unauthorized()
            try:
                mcp_binding(request, require_subscription=False)
            except Problem as exc:
                if exc.status == 401:
                    return mcp_unauthorized()
                raise
        return Response(
            b": quant\n\n",
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-store",
            },
        )

    @app.post("/mcp")
    @app.post("/mcp/")
    async def mcp(request: Request):
        # ChatGPT's connector validator makes an empty POST probe with
        # application/octet-stream before MCP JSON-RPC. It expects 204 for
        # that probe, not a JSON-RPC result. Actual MCP bodies are accepted
        # as JSON regardless of content type.
        raw = await request.body()
        content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type == "application/octet-stream" and not raw:
            return Response(status_code=204)
        p = await payload(request, any_content_type=True)
        method = p.get("method")
        if not isinstance(method, str) or not method:
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": p.get("id"),
                    "error": {"code": -32600, "message": "Invalid Request"},
                },
                status_code=400,
            )
        request.state.mcp_method = method
        if diagnostic_logger is not None and isinstance(method, str) and "event" in method.lower():
            diagnostic_logger.info(json.dumps({
                "mcp_event_method_received": {
                    "method": method,
                    "id_present": p.get("id") is not None,
                    "params_type": type(p.get("params")).__name__,
                }
            }, ensure_ascii=False))
        identifier = p.get("id")
        if identifier is None:
            return Response(status_code=202)
        params_obj = p.get("params") if isinstance(p.get("params"), dict) else {}
        b = None
        requested_tool = params_obj.get("name") if method == "tools/call" else None
        checkout_tool = requested_tool in (
            "prepare_checkout",
            "get_checkout_status",
            "get_checkout_qr",
        )
        can_read_without_subscription = (
            method in MCP_PUBLIC_METHODS
            or requested_tool
            in (
                "get_subscription_status",
                "get_scan_schedule",
                # Account notices remain readable/acknowledgeable after expiry;
                # Service.events/ack still filter to account:* and require the
                # account's own receipt and agent-enabled preference.
                "list_events",
                "ack_event",
            )
            or checkout_tool
        )
        authorization = request.headers.get("authorization", "")
        if authorization:
            if not authorization.startswith("Bearer "):
                return mcp_unauthorized()
            try:
                b = mcp_binding(
                    request,
                    require_subscription=not can_read_without_subscription,
                    pending=checkout_tool,
                )
            except Problem as exc:
                if diagnostic_logger is not None and method == "events/subscribe":
                    diagnostic_logger.info(json.dumps({"mcp_event_subscribe_auth_error": {"status": exc.status, "message": exc.message}}, ensure_ascii=False))
                if exc.status == 401:
                    return mcp_unauthorized()
                if method == "tools/call":
                    return mcp_tool_error(identifier, exc)
                return {
                    "jsonrpc": "2.0",
                    "id": identifier,
                    "error": {
                        "code": -32000,
                        "message": translate_error(exc.message, "en"),
                    },
                }
        elif method not in MCP_PUBLIC_METHODS:
            if method == "tools/call":
                return mcp_tool_challenge(identifier)
            return mcp_unauthorized()
        if b is not None and b.get("oauth_scope") is not None:
            scopes = set((b.get("oauth_scope") or "").split())
            if method == "tools/call":
                required = TOOL_SCOPES.get(requested_tool, "quant.read")
                if required not in scopes:
                    return mcp_tool_challenge(identifier)
            elif method in ("events/subscribe", "events/unsubscribe") and "quant.read" not in scopes:
                if diagnostic_logger is not None and method == "events/subscribe":
                    diagnostic_logger.info(json.dumps({"mcp_event_subscribe_scope_error": {"scope": b.get("oauth_scope")}}, ensure_ascii=False))
                return mcp_tool_challenge(identifier)
        tools = TOOLS

        def ok(value):
            return {
                "jsonrpc": "2.0",
                "id": identifier,
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(value, ensure_ascii=False),
                        }
                    ]
                },
            }

        def ok_image(value, *, field="qr_image"):
            # Keep the base64 payload in an MCP image block, not in the text
            # JSON, so the model can display it without dumping it into context.
            value = dict(value)
            data = value.pop(field, None)
            content = [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}]
            if data:
                content.append(
                    {
                        "type": "image",
                        "data": data.split(",", 1)[1],
                        "mimeType": "image/png",
                    }
                )
            return {
                "jsonrpc": "2.0",
                "id": identifier,
                "result": {"content": content},
            }

        try:
            if method == "server/discover":
                result = mcp_events.discover()
            elif method == "initialize":
                init_params = params_obj
                requested = (
                    init_params.get("protocolVersion")
                    or (init_params.get("_meta") or {}).get(
                        "io.modelcontextprotocol/protocolVersion"
                    )
                    or request.headers.get("mcp-protocol-version")
                )
                supported = {
                    "2026-07-28",
                    "2025-11-25",
                    "2025-06-18",
                    "2025-03-26",
                    "2024-11-05",
                }
                result = {
                    "protocolVersion": requested if requested in supported else "2025-06-18",
                    "capabilities": {"tools": {}, "events": {}},
                    "serverInfo": {"name": "hadan-subscriptions", "version": "0.2.0"},
                    "instructions": mcp_events.discover()["instructions"],
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": tools}
            elif method == "events/list":
                result = mcp_events.list(b)
            elif method == "events/subscribe":
                if diagnostic_logger is not None:
                    diagnostic_logger.info(json.dumps({"mcp_event_subscribe_branch": {"has_b": b is not None}}, ensure_ascii=False))
                sub_params = p.get("params") if isinstance(p.get("params"), dict) else {}
                delivery = sub_params.get("delivery") if isinstance(sub_params.get("delivery"), dict) else {}
                callback = delivery.get("url")
                secret = delivery.get("secret")
                callback_host = ""
                callback_digest = ""
                if isinstance(callback, str):
                    try:
                        callback_host = urlsplit(callback).hostname or ""
                    except ValueError:
                        callback_host = ""
                    callback_digest = hashlib.sha256(callback.encode()).hexdigest()[:12]
                attempt = {
                    "user_id": b["user_id"],
                    "binding_id": b["id"],
                    "callback_host": callback_host,
                    "callback_digest": callback_digest,
                    "delivery_keys": sorted(delivery),
                    "secret_prefix": secret[:6] if isinstance(secret, str) else None,
                    "secret_length": len(secret) if isinstance(secret, str) else None,
                    "ttl_ms": sub_params.get("ttlMs"),
                    "arguments": sub_params.get("arguments"),
                }
                if diagnostic_logger is not None:
                    diagnostic_logger.info(json.dumps({"mcp_event_subscribe_attempt": attempt}, ensure_ascii=False))
                try:
                    result = await run_in_threadpool(
                        mcp_events.subscribe, b, p.get("params") or {}
                    )
                    if diagnostic_logger is not None:
                        diagnostic_logger.info(json.dumps({"mcp_event_subscribe_ok": {"id": result.get("id")}}, ensure_ascii=False))
                except CallbackError as exc:
                    if diagnostic_logger is not None:
                        diagnostic_logger.info(json.dumps({"mcp_event_subscribe_error": {"kind": "callback", "reason": exc.reason, "message": str(exc)}}, ensure_ascii=False))
                    return {
                        "jsonrpc": "2.0",
                        "id": identifier,
                        "error": {
                            "code": -32015,
                            "message": "CallbackEndpointError",
                            "data": {"reason": exc.reason},
                        },
                    }
                except Problem as exc:
                    if diagnostic_logger is not None:
                        diagnostic_logger.info(json.dumps({"mcp_event_subscribe_error": {"kind": "problem", "status": exc.status, "message": exc.message}}, ensure_ascii=False))
                    return {
                        "jsonrpc": "2.0",
                        "id": identifier,
                        "error": {
                            "code": -32602,
                            "message": translate_error(exc.message, "en"),
                        },
                    }
            elif method == "events/unsubscribe":
                try:
                    result = mcp_events.unsubscribe(b, p.get("params") or {})
                except Problem as exc:
                    return {
                        "jsonrpc": "2.0",
                        "id": identifier,
                        "error": {
                            "code": -32602,
                            "message": translate_error(exc.message, "en"),
                        },
                    }
            elif method == "tools/call":
                params = params_obj
                name = params.get("name")
                if not isinstance(name, str) or name not in TOOL_SCOPES:
                    return mcp_unknown_tool(identifier)
                try:
                    a = validate_arguments(name, params.get("arguments", {}))
                except ToolArgumentError as exc:
                    return mcp_invalid_params(identifier, exc)
                if name == "get_subscription_status":
                    from .commerce import Commerce
                    from .terms import TERMS_VERSION

                    sub = s.db.one(
                        "SELECT status,expires_at,email_enabled,agent_enabled "
                        "FROM subscriptions WHERE user_id=? AND product_id='quantgt'",
                        (b["user_id"],),
                    )
                    state = sub["status"] if sub else "none"
                    if (
                        state == "active"
                        and sub["expires_at"] is not None
                        and sub["expires_at"] <= time.time()
                    ):
                        state = "expired"
                    options = Commerce(s).options()
                    value = {
                        "subscription": (
                            {
                                "status": state,
                                "expires_at": sub["expires_at"] or None,
                                "email_enabled": bool(sub["email_enabled"]),
                                "agent_enabled": bool(sub["agent_enabled"]),
                            }
                            if sub
                            else {"status": "none", "expires_at": None, "email_enabled": False, "agent_enabled": False}
                        ),
                        "subscribe_url": cfg.base_url.rstrip("/") + "/app?flow=subscribe",
                        "plans": options["plans"],
                        "payment_methods": [
                            item["id"]
                            for item in options["methods"]
                            if item["enabled"] and item["id"] in ("wechat", "alipay")
                        ],
                        "terms_version": TERMS_VERSION,
                        "terms_url": cfg.base_url.rstrip("/") + "/terms",
                        "mail_sender": cfg.mail_sender,
                        "support_contact": cfg.support_contact,
                        "payment_by": "owner",
                        "automatic_renewal": False,
                    }
                elif name == "get_scan_schedule":
                    from .schedule_tool import upcoming_scans

                    value = upcoming_scans()
                elif name == "prepare_checkout":
                    from .agent_checkout import AgentCheckout

                    checkout = AgentCheckout(s)
                    if a:
                        return ok_image(
                            await run_in_threadpool(
                                checkout.start,
                                b,
                                a,
                                request.client.host if request.client else "unknown",
                            )
                        )
                    return ok_image(await run_in_threadpool(checkout.prepare, b))
                elif name == "get_checkout_status":
                    from .agent_checkout import AgentCheckout

                    value = await run_in_threadpool(AgentCheckout(s).status, b)
                elif name == "get_checkout_qr":
                    from .agent_checkout import AgentCheckout

                    checkout = AgentCheckout(s)
                    value = await run_in_threadpool(checkout.status, b)
                    if not value.get("qr_available"):
                        raise Problem("付款二维码尚未生成", 409)
                    raw = await run_in_threadpool(checkout.qr, b)
                    metadata = {
                        key: value.get(key)
                        for key in (
                            "id",
                            "status",
                            "amount_minor",
                            "currency",
                            "qr_expires_at",
                            "payment_by",
                        )
                    }
                    return {
                        "jsonrpc": "2.0",
                        "id": identifier,
                        "result": {
                            "content": [
                                {
                                    "type": "text",
                                    "text": json.dumps(metadata, ensure_ascii=False),
                                },
                                {
                                    "type": "image",
                                    "data": base64.b64encode(raw).decode("ascii"),
                                    "mimeType": "image/png",
                                },
                            ]
                        },
                    }
                elif name == "list_events":
                    value = {
                        "events": s.events(
                            b["user_id"],
                            int(a.get("after", 0)),
                            pending=True,
                            agent=True,
                            binding_id=b["id"],
                            include_webhook=bool(a.get("include_webhook", False)),
                        )
                    }
                elif name == "read_content":
                    value = s.content(b["user_id"], a["content_id"], a.get("version"))
                    require_agent_product(b["user_id"], value["product_id"])
                    value.pop("html", None)
                elif name == "ack_event":
                    value = s.ack(b, a["event_id"])
                elif name == "read_attachment":
                    attachment, path = s.attachment(b["user_id"], a["attachment_id"])
                    require_agent_product(
                        b["user_id"],
                        s.content(b["user_id"], attachment["content_id"])["product_id"],
                    )
                    return {
                        "jsonrpc": "2.0",
                        "id": identifier,
                        "result": {
                            "content": [
                                {
                                    "type": "resource",
                                    "resource": {
                                        "uri": "hadan://attachments/" + attachment["id"],
                                        "mimeType": attachment["mime"],
                                        "blob": base64.b64encode(path.read_bytes()).decode(),
                                    },
                                }
                            ]
                        },
                    }
                else:
                    raise Problem("unknown tool")
                return ok(value)
            else:
                return {
                    "jsonrpc": "2.0",
                    "id": identifier,
                    "error": {"code": -32601, "message": "Method not found"},
                }
        except Problem as exc:
            return mcp_tool_error(identifier, exc)
        return {"jsonrpc": "2.0", "id": identifier, "result": result}

    @app.get("/api/admin/members")
    def admin_members(request: Request, q: str = "", status: str = "all", limit: int = 100):
        from .admin_members import list_members

        user(request, admin=True)
        return list_members(s, q, status, limit)

    @app.post("/api/admin/members/preview")
    async def admin_member_preview(request: Request):
        from .admin_members import preview_add, preview_extend, preview_remove

        u = user(request, write=True, admin=True)
        p = await payload(request)
        action = p.get("action")
        if action == "add":
            return preview_add(s, p.get("email"), p.get("days"), p.get("until"), p.get("email_consent", False), p.get("note"))
        if action == "extend":
            return preview_extend(s, p.get("email"), p.get("days"), p.get("until"))
        if action == "remove":
            from .security import email_address
            try:
                target = email_address(p.get("email"))
            except ValueError:
                raise Problem("邮箱格式无效", 400) from None
            if target == u["email"].lower():
                raise Problem("不能移除当前管理员账号", 400)
            return preview_remove(s, target)
        raise Problem("未知成员操作", 400)

    @app.post("/api/admin/members")
    async def admin_member_write(request: Request):
        from .admin_members import add, extend, remove

        u = user(request, write=True, admin=True)
        p = await payload(request)
        action, email = p.get("action"), p.get("email")
        if p.get("confirm") is not True:
            raise Problem("请先预览并确认操作", 400)
        actor = "admin:" + u["id"]
        if action == "add":
            return await run_in_threadpool(
                add, s, email, p.get("days"), p.get("until"),
                p.get("email_consent", False), p.get("note"), actor,
            )
        if action == "extend":
            return await run_in_threadpool(
                extend, s, email, p.get("days"), p.get("until"), actor
            )
        if action == "remove":
            from .security import email_address
            try:
                normalized = email_address(email)
            except ValueError:
                raise Problem("邮箱格式无效", 400) from None
            if normalized == u["email"].lower():
                raise Problem("不能移除当前管理员账号", 400)
            if p.get("confirm_email") != normalized:
                raise Problem("确认邮箱与目标邮箱不一致", 400)
            return await run_in_threadpool(remove, s, normalized, actor)
        raise Problem("未知成员操作", 400)

    @app.get("/api/admin/status")
    def admin_status(request: Request):
        from .commerce import Commerce
        user(request, admin=True)
        return {
            "jobs": s.db.all(
                "SELECT kind,state,COUNT(*) count FROM jobs GROUP BY kind,state"
            ),
            "deliveries": s.db.all(
                "SELECT state,COUNT(*) count FROM deliveries GROUP BY state"
            ),
            "heartbeats": s.db.all("SELECT * FROM heartbeats"),
            "incidents": s.db.all(
                "SELECT * FROM incidents ORDER BY updated_at DESC LIMIT 50"
            ),
            "recent": s.db.all(
                "SELECT e.id,e.content_id,e.kind,e.summary,e.created_at,c.withdrawn FROM events e LEFT JOIN contents c ON c.id=e.content_id ORDER BY e.seq DESC LIMIT 20"
            ),
            "readiness": {
                "mail": cfg.mail_backend,
                "ai": cfg.ops_ai,
                "payments": bool(cfg.stripe_key) or Commerce(s).provider_ready(),
                "production": cfg.production,
            },
            "members": s.db.all(
                "SELECT s.id,u.email,p.name,s.expires_at,s.email_enabled,s.agent_enabled FROM subscriptions s JOIN users u ON u.id=s.user_id JOIN products p ON p.id=s.product_id ORDER BY s.created_at DESC LIMIT 200"
            ),
            "orders": s.db.all("SELECT id,plan_id,method,amount_minor,currency,status,created_at,paid_at FROM purchase_orders ORDER BY created_at DESC LIMIT 50"),
        }

    @app.post("/api/admin/publish")
    async def admin_publish(request: Request):
        u = user(request, write=True, admin=True)
        p = await payload(request)
        files = []
        for f in p.get("files", []):
            try:
                data = base64.b64decode(f["base64"], validate=True)
            except (ValueError, KeyError):
                raise Problem("附件编码无效")
            files.append({"name": str(f["name"]), "data": data})
        return await run_in_threadpool(
            s.publish,
            str(p.get("product_id", "")),
            str(p.get("source_key") or uid()),
            p.get("title", ""),
            p.get("body", ""),
            files=files,
            changes=str(p.get("changes", "")),
            actor=u["id"],
        )

    @app.post("/api/admin/content/{cid}/withdraw")
    async def admin_withdraw(request: Request, cid: str):
        u = user(request, write=True, admin=True)
        p = await payload(request)
        return s.withdraw(cid, p.get("reason", "内容已撤回"), u["id"])

    @app.post("/api/admin/replay/{eid}")
    async def replay(request: Request, eid: str):
        user(request, write=True, admin=True)
        p = await payload(request)
        return s.replay(eid, p.get("user_id"), apply=p.get("apply") is True)

    @app.post("/api/admin/jobs")
    async def admin_job(request: Request):
        u = user(request, write=True, admin=True)
        p = await payload(request)
        kind = p.get("kind")
        if kind not in (
            "picks",
            "weekly_digest",
            "official_mail",
            "health_site",
            "health",
            "daily_admin_status",
        ):
            raise Problem("任务类型无效")
        with s.db.transaction() as db:
            jid = enqueue(
                db,
                "admin_summary" if kind == "daily_admin_status" else "source:" + kind,
                {},
                "manual:" + uid(),
                queue="main" if kind == "daily_admin_status" else "source",
            )
            audit(db, u["id"], "run_job", jid, {"kind": kind})
        return {"job_id": jid}

    @app.post("/api/checkout")
    async def checkout(request: Request):
        from .billing import Billing
        from .terms import begin_agreement

        u = user(request, write=True)
        p = await payload(request)
        agreement_id = begin_agreement(s, u["id"], "checkout", p)
        return await run_in_threadpool(
            Billing(s).checkout, u["id"], p.get("product_id"), agreement_id=agreement_id
        )

    @app.get("/api/purchase-options")
    def purchase_options():
        from .commerce import Commerce
        from .terms import TERMS_VERSION

        options = Commerce(s).options()
        return {
            **options,
            "terms_version": TERMS_VERSION,
            "terms_url": cfg.base_url.rstrip("/") + "/terms",
            "mail_sender": cfg.mail_sender,
            "support_contact": cfg.support_contact,
            "agent_payment_methods": [
                item["id"]
                for item in options["methods"]
                if item["enabled"] and item["id"] in ("wechat", "alipay")
            ],
        }

    @app.post("/api/redeem")
    async def redeem(request: Request):
        from .commerce import Commerce
        u = user(request, write=True)
        p = await payload(request)
        return await run_in_threadpool(Commerce(s).redeem, u["id"], p.get("code", ""))

    @app.post("/api/purchase")
    async def purchase(request: Request):
        from .commerce import Commerce
        from .terms import begin_agreement
        u = user(request, write=True)
        p = await payload(request)
        quote_id = p.get("quote_id")
        if quote_id is not None:
            plan = next(
                (item for item in Commerce(s).plans() if item["id"] == p.get("plan_id")),
                None,
            )
            if (
                not isinstance(quote_id, str)
                or not plan
                or not hmac.compare_digest(quote_id, plan["quote_id"])
            ):
                raise Problem(
                    "报价已更新，请重新确认",
                    409,
                    code="quote_changed",
                    data={"quote_changed": True},
                )
        agreement_id = begin_agreement(s, u["id"], "purchase", p)
        return await run_in_threadpool(Commerce(s).create_order, u["id"], p.get("plan_id"), p.get("method"), agreement_id=agreement_id,
                                       payer_ip=request.client.host)

    @app.get("/api/orders")
    def purchase_orders(request: Request):
        """Owner-visible recent orders used to recover an uncertain POST/refresh."""
        from .commerce import Commerce

        owner = user(request)
        rows = s.db.all(
            "SELECT id FROM purchase_orders WHERE user_id=? ORDER BY created_at DESC LIMIT 10",
            (owner["id"],),
        )
        orders = []
        for row in rows:
            try:
                orders.append(Commerce(s).order(owner["id"], row["id"]))
            except Problem:
                continue
        return {"orders": orders}

    @app.get("/api/orders/{order_id}")
    def purchase_status(request: Request, order_id: str):
        from .commerce import Commerce
        return Commerce(s).order(user(request)["id"], order_id)

    @app.get("/api/orders/{order_id}/qr")
    def purchase_qr(request: Request, order_id: str):
        from .commerce import Commerce
        raw = Commerce(s).qr(user(request)["id"], order_id)
        return Response(raw, media_type="image/png")

    @app.post("/api/orders/{order_id}/reconcile")
    def purchase_reconcile(request: Request, order_id: str):
        from .commerce import Commerce

        owner = user(request, write=True)
        if not s.db.one(
            "SELECT id FROM purchase_orders WHERE id=? AND user_id=?",
            (order_id, owner["id"]),
        ):
            raise Problem("付款订单不存在", 404)
        return Commerce(s).reconcile(order_id)

    @app.post("/api/bindings/{bid}/checkout-permission")
    async def checkout_permission(request: Request, bid: str):
        from .agent_checkout import AgentCheckout
        u = user(request, write=True)
        return AgentCheckout(s).authorize(u["id"], bid, await payload(request), request.client.host)

    @app.post("/api/checkout-permissions/{permission_id}/prepare")
    async def checkout_permission_prepare(request: Request, permission_id: str):
        from .agent_checkout import AgentCheckout

        u = user(request, write=True)
        return await run_in_threadpool(
            AgentCheckout(s).prepare_for_owner, u["id"], permission_id
        )

    @app.post("/api/agent/checkout")
    async def agent_prepare_checkout(request: Request):
        from .agent_checkout import AgentCheckout

        b = agent(request, pending=True)
        p = await payload(request)
        checkout = AgentCheckout(s)
        if p:
            return await run_in_threadpool(
                checkout.start,
                b,
                p,
                request.client.host if request.client else "unknown",
            )
        return await run_in_threadpool(checkout.prepare, b)

    @app.get("/api/agent/checkout")
    def agent_checkout_status(request: Request):
        from .agent_checkout import AgentCheckout
        return AgentCheckout(s).status(agent(request, pending=True))

    @app.get("/api/agent/checkout/qr")
    def agent_checkout_qr(request: Request):
        from .agent_checkout import AgentCheckout
        raw = AgentCheckout(s).qr(agent(request, pending=True))
        return Response(raw, media_type="image/png")

    @app.post("/webhooks/xorpay")
    async def xorpay_hook(request: Request):
        from .commerce import Commerce
        from urllib.parse import parse_qs
        if not request.headers.get("content-type", "").startswith("application/x-www-form-urlencoded"):
            raise Problem("付款通知无效", 415)
        try:
            values = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True, max_num_fields=30)
        except (UnicodeError, ValueError):
            raise Problem("付款通知无效", 400)
        if any(len(value) != 1 for value in values.values()):
            raise Problem("付款通知无效", 400)
        await run_in_threadpool(Commerce(s).notify, {k:v[0] for k,v in values.items()})
        return Response("ok", media_type="text/plain")

    @app.post("/webhooks/xiaodou")
    async def xiaodou_hook(request: Request):
        from .xiaodou import Xiaodou
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise Problem("付款通知无效", 415)
        # Duplicate security headers must never collapse into an ambiguous identity.
        for name in ("signature", "key-id", "timestamp", "nonce", "event-id", "event-type"):
            if len(request.headers.getlist("x-xd-webhook-" + name)) != 1:
                raise Problem("付款通知无效", 400)
        await run_in_threadpool(Xiaodou(s).notify, await request.body(), request.headers)
        return Response("success", media_type="text/plain")

    @app.post("/webhooks/ses")
    async def ses_hook(request: Request):
        from .ses_feedback import SesFeedback
        content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type not in ("application/json", "text/plain"):
            raise Problem("反馈通知无效", 415)
        raw = await request.body()
        result = await run_in_threadpool(SesFeedback(s).handle, raw, request.headers)
        return JSONResponse(result)

    @app.post("/webhooks/directmail")
    async def directmail_hook(request: Request):
        from .directmail_feedback import DirectMailFeedback
        content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise Problem("反馈通知无效", 415)
        raw = await request.body()
        result = await run_in_threadpool(
            DirectMailFeedback(s).handle, raw, request.headers
        )
        return JSONResponse(result)

    @app.post("/api/billing/portal")
    def portal(request: Request):
        from .billing import Billing

        return Billing(s).portal(user(request, write=True)["id"])

    @app.post("/webhooks/stripe")
    async def payment_hook(request: Request):
        from .billing import Billing

        return Billing(s).webhook(
            await request.body(), request.headers.get("stripe-signature", "")
        )

    return app
