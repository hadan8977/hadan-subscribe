from __future__ import annotations

import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import time
import fcntl
from contextlib import contextmanager
from functools import wraps
from pathlib import Path

from .config import Settings
from .db import Database, audit, enqueue, uid
from .security import (
    action_id,
    digest,
    email_address,
    encrypt,
    mac,
    token,
    webhook_target,
)


@contextmanager
def artifact_lock(directory):
    path = Path(directory) / ".artifacts.lock"
    with path.open("a+") as handle:
        path.chmod(0o600)
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def guard_artifacts(function):
    @wraps(function)
    def wrapped(self, *args, **kwargs):
        with artifact_lock(self.settings.data_dir):
            return function(self, *args, **kwargs)

    return wrapped


class Problem(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.message, self.status = message, status


class Service:
    def __init__(self, settings: Settings):
        self.settings = settings
        settings.initialize()
        self.db = Database(settings.db_path)
        self.db.initialize()

    def seed(self):
        with self.db.transaction() as db:
            for pid, name, description in [
                (
                    "quantgt",
                    "Quant",
                    "持仓、观察名单和周报。正文与附件，通过邮箱或已有 Agent 接收。",
                ),
                ("notes", "Hadan 来信", "产品动态与服务说明，和你的 Agent 一起接收。"),
            ]:
                db.execute(
                    "INSERT OR IGNORE INTO products(id,name,description,paid) VALUES(?,?,?,?)",
                    (
                        pid,
                        name,
                        description,
                        int(self.settings.production and pid == "quantgt"),
                    ),
                )

    @staticmethod
    def _limit(db, key, limit, seconds):
        now = time.time()
        row = db.execute("SELECT * FROM rate_limits WHERE key=?", (key,)).fetchone()
        if row and now - row["start"] < seconds:
            if row["count"] >= limit:
                raise Problem("操作太频繁，请稍后重试", 429)
            db.execute("UPDATE rate_limits SET count=count+1 WHERE key=?", (key,))
        else:
            db.execute("INSERT OR REPLACE INTO rate_limits VALUES(?,?,1)", (key, now))

    def _email_allowed(self, email):
        if not self.settings.allowed_emails:
            return True
        if email in self.settings.allowed_emails:
            return True
        return bool(
            self.db.one("SELECT email FROM access_allowlist WHERE email=?", (email,))
        )

    def request_code(self, email: str, ip: str):
        email = email_address(email)
        if not self._email_allowed(email):
            raise Problem("当前仅向受邀账号开放", 403)
        if self.db.one("SELECT email FROM suppressions WHERE email=?", (email,)):
            raise Problem("该邮箱暂时无法接收验证码，请联系管理员", 409)
        now, code = time.time(), f"{secrets.randbelow(1000000):06d}"
        with self.db.transaction() as db:
            self._limit(db, "otp-email:" + digest(email), 3, 600)
            self._limit(db, "otp-ip:" + digest(ip), 12, 600)
            self._limit(
                db, "otp-global", int(os.getenv("OTP_HOURLY_BUDGET", "60")), 3600
            )
            db.execute(
                "INSERT OR REPLACE INTO otp VALUES(?,?,?,0)",
                (email, mac(self.settings.secret, email + ":" + code), now + 600),
            )
            enqueue(
                db,
                "login_mail",
                {"email": email, "encrypted_code": encrypt(self.settings.secret, code)},
                "otp:" + uid(),
            )
        return {"message": "验证码已安排发送，10 分钟内有效。请检查收件箱和垃圾邮件。"}

    def verify_code(self, email, code, ip):
        email = email_address(email)
        if not self._email_allowed(email):
            raise Problem("当前仅向受邀账号开放", 403)
        now, failure, session = time.time(), False, token()
        with self.db.transaction() as db:
            self._limit(db, "verify-ip:" + digest(ip), 30, 600)
            row = db.execute("SELECT * FROM otp WHERE email=?", (email,)).fetchone()
            if not row or row["expires_at"] < now or row["attempts"] >= 5:
                failure = True
            elif not hmac.compare_digest(
                row["digest"], mac(self.settings.secret, email + ":" + str(code))
            ):
                db.execute("UPDATE otp SET attempts=attempts+1 WHERE email=?", (email,))
                failure = True
            else:
                db.execute("DELETE FROM otp WHERE email=?", (email,))
                user = db.execute(
                    "SELECT * FROM users WHERE email=?", (email,)
                ).fetchone()
                user_id = user["id"] if user else uid()
                if not user:
                    db.execute("INSERT INTO users VALUES(?,?,?)", (user_id, email, now))
                db.execute(
                    "INSERT INTO sessions VALUES(?,?,?)",
                    (digest(session), user_id, now + self.settings.session_seconds),
                )
                audit(db, user_id, "login", user_id)
        if failure:
            raise Problem("验证码错误、已过期或已使用，请重新获取", 401)
        return session

    def session_user(self, session):
        if not session:
            raise Problem("请先验证邮箱", 401)
        user = self.db.one(
            "SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.digest=? AND s.expires_at>?",
            (digest(session), time.time()),
        )
        if not user:
            raise Problem("登录已过期，请重新验证邮箱", 401)
        if not self._email_allowed(user["email"]):
            raise Problem("当前仅向受邀账号开放", 403)
        user["is_admin"] = user["email"] in self.settings.admin_emails
        return user

    def agent(self, raw, *, pending=False):
        b = self.db.one("SELECT * FROM bindings WHERE token_digest=?", (digest(raw),))
        if not b or b["state"] not in (
            ("pending", "active") if pending else ("active",)
        ):
            raise Problem("Agent 授权无效或已撤销", 401)
        owner = self.db.one("SELECT email FROM users WHERE id=?", (b["user_id"],))
        if not owner or not self._email_allowed(owner["email"]):
            raise Problem("当前仅向受邀账号开放", 403)
        self.db.execute(
            "UPDATE bindings SET last_seen=? WHERE id=?", (time.time(), b["id"])
        )
        return b

    def _oauth_binding(self, db, user_id, name):
        """Create the single active Agent binding used by an OAuth connector."""
        name = " ".join(str(name or "ChatGPT").split())[:80] or "ChatGPT"
        bid, raw = uid(), token()
        self._limit(db, "oauth-bindings:" + user_id, 10, 3600)
        db.execute(
            "UPDATE mcp_event_subscriptions SET active=0,updated_at=? "
            "WHERE binding_id IN (SELECT id FROM bindings WHERE user_id=? AND state='active')",
            (time.time(), user_id),
        )
        db.execute(
            "UPDATE bindings SET state='revoked' WHERE user_id=? AND state='active'",
            (user_id,),
        )
        db.execute(
            "INSERT INTO bindings(id,user_id,name,mode,state,token_digest,test_read,test_download,test_ok,created_at,last_seen) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (bid, user_id, name, "poll", "active", digest(raw), 1, 1, 1, time.time(), time.time()),
        )
        # OAuth connection creation never overrides an existing delivery
        # opt-out. A new subscription starts with agent delivery enabled; the
        # owner can still disable it from the account page.
        audit(db, user_id, "oauth_binding_created", bid, {"name": name})
        return bid

    def oauth_binding(self, user_id, name):
        with self.db.transaction() as db:
            return self._oauth_binding(db, user_id, name)

    def oauth_identity(self, raw, *, require_subscription=True):
        """Resolve an OAuth access token to the same binding shape as agent()."""
        b = self.db.one(
            "SELECT b.*,t.client_id oauth_client_id,t.scope oauth_scope,t.resource oauth_resource "
            "FROM oauth_tokens t JOIN bindings b ON b.id=t.binding_id "
            "WHERE t.digest=? AND t.kind='access' AND t.revoked_at IS NULL AND t.expires_at>?",
            (digest(raw), time.time()),
        )
        if not b or b["state"] != "active":
            raise Problem("Agent 授权无效或已撤销", 401)
        owner = self.db.one("SELECT email FROM users WHERE id=?", (b["user_id"],))
        if not owner or not self._email_allowed(owner["email"]):
            raise Problem("当前仅向受邀账号开放", 403)
        if require_subscription:
            sub = self.db.one(
                "SELECT * FROM subscriptions WHERE user_id=? AND product_id='quantgt' "
                "AND status='active' AND (expires_at IS NULL OR expires_at>?)",
                (b["user_id"], time.time()),
            )
            if not sub or not sub["agent_enabled"]:
                raise Problem("该账号的 Agent 交付已关闭", 403)
        self.db.execute(
            "UPDATE bindings SET last_seen=? WHERE id=?", (time.time(), b["id"])
        )
        return b

    def subscribe(self, user_id, product_id):
        now = time.time()
        with self.db.transaction() as db:
            p = db.execute(
                "SELECT * FROM products WHERE id=? AND active=1", (product_id,)
            ).fetchone()
            if not p:
                raise Problem("订阅内容不存在", 404)
            if p["paid"]:
                raise Problem("请先完成付款", 402)
            existing = db.execute(
                "SELECT * FROM subscriptions WHERE user_id=? AND product_id=?",
                (user_id, product_id),
            ).fetchone()
            if existing:
                if existing["status"] == "ended":
                    db.execute(
                        "UPDATE subscriptions SET status='active' WHERE id=?",
                        (existing["id"],),
                    )
                    result = dict(existing)
                    result["status"] = "active"
                    audit(db, user_id, "resubscribe", existing["id"])
                    return result
                return dict(existing)  # Never silently reset delivery consent.
            sid = uid()
            db.execute(
                "INSERT INTO subscriptions(id,user_id,product_id,created_at) VALUES(?,?,?,?)",
                (sid, user_id, product_id, now),
            )
            db.execute("INSERT INTO delivery_preferences VALUES(?,'always')", (sid,))
            audit(db, user_id, "subscribe", sid)
        return self.db.one("SELECT * FROM subscriptions WHERE id=?", (sid,))

    def subscriptions(self, user_id):
        return self.db.all(
            "SELECT s.*,p.name,p.paid,COALESCE(d.email_policy,'always') email_policy FROM subscriptions s JOIN products p ON p.id=s.product_id LEFT JOIN delivery_preferences d ON d.subscription_id=s.id WHERE s.user_id=? ORDER BY s.created_at",
            (user_id,),
        )

    def preferences(self, user_id, sid, values):
        allowed = {"email_enabled", "agent_enabled", "email_format", "email_policy"}
        if not values or set(values) - allowed:
            raise Problem("不支持的交付设置")
        for key in ("email_enabled", "agent_enabled"):
            if key in values and type(values[key]) is not bool:
                raise Problem("交付开关必须是布尔值")
        if values.get("email_format", "summary") not in (
            "summary",
            "full",
        ) or values.get("email_policy", "always") not in ("fallback", "always"):
            raise Problem("无效的邮件交付方式")
        with self.db.transaction() as db:
            row = db.execute(
                "SELECT * FROM subscriptions WHERE id=? AND user_id=?", (sid, user_id)
            ).fetchone()
            if not row:
                raise Problem("订阅不存在", 404)
            fields = {k: v for k, v in values.items() if k != "email_policy"}
            if fields:
                db.execute(
                    "UPDATE subscriptions SET "
                    + ",".join(k + "=?" for k in fields)
                    + " WHERE id=?",
                    (*fields.values(), sid),
                )
            if "email_policy" in values:
                db.execute(
                    "INSERT INTO delivery_preferences VALUES(?,?) ON CONFLICT(subscription_id) DO UPDATE SET email_policy=excluded.email_policy",
                    (sid, values["email_policy"]),
                )
            audit(db, user_id, "preferences", sid, values)

    def unsubscribe_link(self, value):
        sid = action_id(self.settings.secret, value)
        with self.db.transaction() as db:
            row = db.execute(
                "SELECT * FROM subscriptions WHERE id=?", (sid,)
            ).fetchone()
            if not row:
                raise Problem("订阅不存在", 404)
            db.execute("UPDATE subscriptions SET email_enabled=0 WHERE id=?", (sid,))
            audit(db, "email-link", "email_unsubscribe", sid)
        return {"message": "内容邮件已关闭。Agent 交付与已付权益保持原设置。"}

    def cancel_subscription(self, user_id, sid):
        with self.db.transaction() as db:
            s = db.execute(
                "SELECT * FROM subscriptions WHERE id=? AND user_id=?", (sid, user_id)
            ).fetchone()
            if not s:
                raise Problem("订阅不存在", 404)
            request_id = (
                s["cancel_request_id"]
                if s["cancel_requested"] and s["cancel_request_id"]
                else uid()
            )
            db.execute(
                "UPDATE subscriptions SET email_enabled=0,agent_enabled=0,auto_renew=0,cancel_requested=1,status=CASE WHEN expires_at IS NULL OR status='pending' THEN 'ended' ELSE status END WHERE id=?",
                (sid,),
            )
            db.execute(
                "UPDATE subscriptions SET cancel_request_id=? WHERE id=?",
                (request_id, sid),
            )
            if s["billing_id"]:
                enqueue(
                    db,
                    "billing_cancel",
                    {"subscription_id": sid, "request_id": request_id},
                    "cancel:" + s["billing_id"] + ":" + request_id,
                )
            audit(db, user_id, "cancel_all_delivery", sid)
        return {"message": "两路交付已停止；已安排取消续费，已付权益保留至到期。"}

    def new_binding(self, user_id, name, mode="poll", endpoint=None, raw_token=None):
        if (
            mode not in ("poll", "webhook")
            or not isinstance(name, str)
            or not 1 <= len(name.strip()) <= 80
        ):
            raise Problem("请填写有效的 Agent 名称与接收方式")
        if mode == "webhook":
            webhook_target(endpoint or "")
        bid, raw = uid(), raw_token or token()
        with self.db.transaction() as db:
            self._limit(db, "bindings:" + user_id, 10, 3600)
            db.execute(
                "INSERT INTO bindings(id,user_id,name,mode,endpoint,token_digest,created_at) VALUES(?,?,?,?,?,?,?)",
                (bid, user_id, name.strip(), mode, endpoint, digest(raw), time.time()),
            )
            audit(db, user_id, "binding_created", bid, {"mode": mode})
            if mode == "webhook":
                enqueue(db, "webhook_test", {"binding_id": bid}, "test:" + bid)
        return {
            "id": bid,
            "token": raw,
            "webhook_secret": mac(self.settings.secret, "hook:" + bid)
            if mode == "webhook"
            else None,
            "message": "凭证只显示一次。请安全保存并完成测试。",
        }

    def activate(self, user_id, bid):
        with self.db.transaction() as db:
            b = db.execute(
                "SELECT * FROM bindings WHERE id=? AND user_id=?", (bid, user_id)
            ).fetchone()
            if not b or b["state"] == "revoked":
                raise Problem("连接不存在或已撤销", 404)
            if not b["test_ok"] or (b["mode"] == "webhook" and not b["test_hook"]):
                raise Problem("请先完成正文、附件和回执测试", 409)
            db.execute(
                "UPDATE mcp_event_subscriptions SET active=0,updated_at=? "
                "WHERE binding_id IN (SELECT id FROM bindings WHERE user_id=? AND state='active' AND id<>?)",
                (time.time(), user_id, bid),
            )
            db.execute(
                "UPDATE bindings SET state='revoked' WHERE user_id=? AND state='active' AND id<>?",
                (user_id, bid),
            )
            db.execute("UPDATE bindings SET state='active' WHERE id=?", (bid,))
            # Receipt ownership survives changing the connection.
            if b["mode"] == "webhook":
                for r in db.execute(
                    "SELECT event_id FROM receipts WHERE user_id=? AND state='pending'",
                    (user_id,),
                ).fetchall():
                    self._delivery(db, r["event_id"], user_id, "webhook", bid)
            audit(db, user_id, "binding_activated", bid)

    def revoke(self, user_id, bid):
        with self.db.transaction() as db:
            if not db.execute(
                "UPDATE bindings SET state='revoked' WHERE id=? AND user_id=?",
                (bid, user_id),
            ).rowcount:
                raise Problem("连接不存在", 404)
            db.execute(
                "UPDATE mcp_event_subscriptions SET active=0,updated_at=? WHERE binding_id=? AND user_id=?",
                (time.time(), bid, user_id),
            )
            audit(db, user_id, "binding_revoked", bid)

    def pair(self, name, mode, endpoint, ip):
        if mode != "poll":
            raise Problem("自动配对先使用定时读取；Webhook 请在网页中创建")
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
            raise Problem("Agent 名称无效")
        secret, pid = token(), uid()
        code = secrets.token_hex(4).upper()
        with self.db.transaction() as db:
            self._limit(db, "pair:" + digest(ip), 15, 3600)
            db.execute(
                "INSERT INTO pairings(id,code,secret_digest,name,mode,endpoint,expires_at) VALUES(?,?,?,?,?,?,?)",
                (pid, code, digest(secret), name, mode, endpoint, time.time() + 600),
            )
        return {
            "pair_id": pid,
            "device_secret": secret,
            "user_code": code,
            "verification_uri": self.settings.base_url + "/app?pair=" + code,
            "expires_in": 600,
        }

    def approve_pair(self, user_id, code):
        # Commit the attempt before validation so a failed guess cannot roll it back.
        with self.db.transaction() as db:
            self._limit(db, "pair-approve:" + user_id, 10, 600)
        if not isinstance(code, str) or not re.fullmatch(r"[0-9A-Fa-f]{8}", code):
            raise Problem("配对码已过期或已使用", 409)
        # Single transaction prevents pairing takeover/approval races.
        with self.db.transaction() as db:
            pair = db.execute(
                "SELECT * FROM pairings WHERE code=? AND expires_at>?",
                (code.upper(), time.time()),
            ).fetchone()
            if not pair or pair["binding_id"]:
                raise Problem("配对码已过期或已使用", 409)
            bid = uid()
            db.execute(
                "INSERT INTO bindings(id,user_id,name,mode,token_digest,created_at) VALUES(?,?,?,?,?,?)",
                (
                    bid,
                    user_id,
                    pair["name"],
                    "poll",
                    pair["secret_digest"],
                    time.time(),
                ),
            )
            db.execute("UPDATE pairings SET binding_id=? WHERE id=?", (bid, pair["id"]))
            audit(db, user_id, "pair_approved", bid)
        return {"binding_id": bid, "message": "已授权，等待 Agent 完成测试。"}

    def entitled(self, user_id, product_id):
        return self.db.one(
            "SELECT * FROM subscriptions WHERE user_id=? AND product_id=? AND status='active' AND (expires_at IS NULL OR expires_at>?)",
            (user_id, product_id, time.time()),
        )

    def _delivery(
        self,
        db,
        event_id,
        user_id,
        channel,
        binding_id="",
        delay=0,
        job_kind="deliver",
        job_dedupe=None,
    ):
        did, now = uid(), time.time()
        cur = db.execute(
            "INSERT OR IGNORE INTO deliveries(id,event_id,user_id,channel,binding_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            (did, event_id, user_id, channel, binding_id, now, now),
        )
        if cur.rowcount:
            enqueue(
                db,
                job_kind,
                {"delivery_id": did},
                job_dedupe or "delivery:" + did,
                available_at=now + delay,
            )

    def account_notice(self, db, user_id, product_id, kind, title, body, identity):
        """Account notices share event/receipt/delivery semantics, without exposing report data."""
        eid = digest("notice:" + identity)[:32]
        if db.execute("SELECT id FROM events WHERE id=?", (eid,)).fetchone():
            return eid
        seq = db.execute("SELECT COALESCE(MAX(seq),0)+1 FROM events").fetchone()[0]
        db.execute(
            "INSERT INTO events VALUES(?,?,?,?,?,?,?,?)",
            (
                eid,
                seq,
                product_id,
                None,
                None,
                "account:" + kind,
                title + "\n" + body,
                time.time(),
            ),
        )
        db.execute("INSERT INTO receipts(user_id,event_id) VALUES(?,?)", (user_id, eid))
        sub = db.execute(
            "SELECT * FROM subscriptions WHERE user_id=? AND product_id=?",
            (user_id, product_id),
        ).fetchone()
        binding = db.execute(
            "SELECT * FROM bindings WHERE user_id=? AND state='active'", (user_id,)
        ).fetchone()
        pref = (
            db.execute(
                "SELECT email_policy FROM delivery_preferences WHERE subscription_id=?",
                (sub["id"],),
            ).fetchone()
            if sub
            else None
        )
        delay = (
            1800
            if sub
            and sub["agent_enabled"]
            and binding
            and (not pref or pref[0] == "fallback")
            else 0
        )
        # Queue even opted-out email for an auditable skip; never revive consent at dispatch.
        self._delivery(
            db,
            eid,
            user_id,
            "email",
            delay=delay,
            job_kind="account_mail",
            job_dedupe=identity,
        )
        if sub and sub["agent_enabled"] and binding and binding["mode"] == "webhook":
            self._delivery(db, eid, user_id, "webhook", binding["id"])
        return eid

    def _fanout(self, db, event_id, product_id, only_user=None):
        now = time.time()
        rows = db.execute(
            "SELECT s.*,COALESCE(p.email_policy,'always') email_policy FROM subscriptions s LEFT JOIN delivery_preferences p ON p.subscription_id=s.id WHERE product_id=? AND status='active' AND (expires_at IS NULL OR expires_at>?)",
            (product_id, now),
        ).fetchall()
        for s in rows:
            if only_user and s["user_id"] != only_user:
                continue
            db.execute(
                "INSERT OR IGNORE INTO receipts(user_id,event_id) VALUES(?,?)",
                (s["user_id"], event_id),
            )
            b = db.execute(
                "SELECT * FROM bindings WHERE user_id=? AND state='active'",
                (s["user_id"],),
            ).fetchone()
            if s["agent_enabled"] and b and b["mode"] == "webhook":
                self._delivery(db, event_id, s["user_id"], "webhook", b["id"])
            if s["email_enabled"]:
                delay = (
                    1800
                    if s["email_policy"] == "fallback" and s["agent_enabled"] and b
                    else 0
                )
                self._delivery(db, event_id, s["user_id"], "email", delay=delay)

    @guard_artifacts
    def publish(
        self,
        product_id,
        source_key,
        title,
        body,
        *,
        files=(),
        html="",
        changes="",
        actor="source",
        baseline=False,
    ):
        if (
            not isinstance(title, str)
            or not 1 <= len(title) <= 250
            or "\n" in title
            or "\r" in title
            or not isinstance(body, str)
            or len(body) > 500000
            or len(files) > 12
        ):
            raise Problem("标题、正文或附件超出限制")
        if not self.db.one("SELECT id FROM products WHERE id=?", (product_id,)):
            raise Problem("内容产品不存在", 404)
        if sum(len(f["data"]) for f in files) > 32 * 1024 * 1024:
            raise Problem("同一版本附件总量超过 32 MiB", 413)
        manifest = []
        for f in files:
            data = f["data"]
            if len(data) > self.settings.max_attachment_bytes:
                raise Problem("附件过大", 413)
            name = (
                Path(f["name"].replace("\\", "/"))
                .name.replace("\r", "")
                .replace("\n", "")[:180]
            )
            if not name:
                raise Problem("附件名称无效")
            sha = hashlib.sha256(data).hexdigest()
            path = self.settings.data_dir / "files" / sha
            if not path.exists():
                tmp = path.with_name("." + uid())
                with open(tmp, "xb") as out:
                    os.chmod(tmp, 0o600)
                    out.write(data)
                    out.flush()
                    os.fsync(out.fileno())
                os.replace(tmp, path)
            manifest.append(
                {
                    "name": name,
                    "mime": mimetypes.guess_type(name)[0] or "application/octet-stream",
                    "size": len(data),
                    "sha256": sha,
                    "storage_key": sha,
                }
            )
        fingerprint = digest(
            json.dumps(
                [
                    title,
                    body,
                    html,
                    changes,
                    [(x["name"], x["sha256"]) for x in manifest],
                ],
                ensure_ascii=False,
            )
        )
        now = time.time()
        with self.db.transaction() as db:
            content = db.execute(
                "SELECT * FROM contents WHERE product_id=? AND source_key=?",
                (product_id, source_key),
            ).fetchone()
            if content:
                previous = db.execute(
                    "SELECT * FROM versions WHERE content_id=? AND number=?",
                    (content["id"], content["current_version"]),
                ).fetchone()
                if previous["fingerprint"] == fingerprint:
                    return {
                        "id": content["id"],
                        "version": content["current_version"],
                        "unchanged": True,
                    }
                if content["withdrawn"]:
                    raise Problem("已撤回内容不能重新发布；请创建新内容", 409)
                cid, number, kind = (
                    content["id"],
                    content["current_version"] + 1,
                    "revised",
                )
                db.execute(
                    "UPDATE contents SET current_version=? WHERE id=?", (number, cid)
                )
            else:
                cid, number, kind = uid(), 1, "published"
                db.execute(
                    "INSERT INTO contents VALUES(?,?,?,1,0,?)",
                    (cid, product_id, source_key, now),
                )
            vid = uid()
            db.execute(
                "INSERT INTO versions VALUES(?,?,?,?,?,?,?,?,?)",
                (vid, cid, number, title, body, html, changes, fingerprint, now),
            )
            for f in manifest:
                db.execute(
                    "INSERT INTO attachments VALUES(?,?,?,?,?,?,?)",
                    (
                        uid(),
                        vid,
                        f["name"],
                        f["mime"],
                        f["size"],
                        f["sha256"],
                        f["storage_key"],
                    ),
                )
            eid = uid()
            seq = db.execute("SELECT COALESCE(MAX(seq),0)+1 FROM events").fetchone()[0]
            db.execute(
                "INSERT INTO events VALUES(?,?,?,?,?,?,?,?)",
                (eid, seq, product_id, cid, vid, kind, title, now),
            )
            if not baseline:
                self._fanout(db, eid, product_id)
                from .mcp_events import MCPEvents

                MCPEvents(self).enqueue_for_event(db, eid, product_id)
            audit(
                db,
                actor,
                "publish" if not baseline else "baseline",
                cid,
                {"version": number, "event": eid},
            )
        return {"id": cid, "version": number, "event_id": eid, "unchanged": False}

    def withdraw(self, cid, reason, actor):
        if (
            not isinstance(reason, str)
            or not reason.strip()
            or len(reason) > 500
            or "\n" in reason
            or "\r" in reason
        ):
            raise Problem("撤回说明须为 1–500 字的单行文本")
        with self.db.transaction() as db:
            c = db.execute("SELECT * FROM contents WHERE id=?", (cid,)).fetchone()
            if not c:
                raise Problem("内容不存在", 404)
            if c["withdrawn"]:
                return {"already_withdrawn": True}
            db.execute("UPDATE contents SET withdrawn=1 WHERE id=?", (cid,))
            eid = uid()
            seq = db.execute("SELECT COALESCE(MAX(seq),0)+1 FROM events").fetchone()[0]
            db.execute(
                "INSERT INTO events VALUES(?,?,?,?,?,?,?,?)",
                (
                    eid,
                    seq,
                    c["product_id"],
                    cid,
                    None,
                    "withdrawn",
                    str(reason)[:500],
                    time.time(),
                ),
            )
            self._fanout(db, eid, c["product_id"])
            from .mcp_events import MCPEvents

            MCPEvents(self).enqueue_for_event(db, eid, c["product_id"])
            audit(db, actor, "withdraw", cid)
        return {"event_id": eid}

    def content(self, user_id, cid, version=None):
        c = self.db.one("SELECT * FROM contents WHERE id=?", (cid,))
        if not c or not self.entitled(user_id, c["product_id"]):
            raise Problem("内容不存在或无访问权限", 404)
        if c["withdrawn"]:
            raise Problem("此内容已撤回", 410)
        v = self.db.one(
            "SELECT * FROM versions WHERE content_id=? AND number=?",
            (cid, version or c["current_version"]),
        )
        if not v:
            raise Problem("版本不存在", 404)
        v["attachments"] = self.db.all(
            "SELECT id,name,mime,size,sha256 FROM attachments WHERE version_id=?",
            (v["id"],),
        )
        v["product_id"] = c["product_id"]
        return v

    def attachment(self, user_id, aid):
        a = self.db.one(
            "SELECT a.*,v.content_id,v.number FROM attachments a JOIN versions v ON v.id=a.version_id WHERE a.id=?",
            (aid,),
        )
        if not a:
            raise Problem("附件不存在", 404)
        self.content(user_id, a["content_id"], a["number"])
        path = self.settings.data_dir / "files" / a["storage_key"]
        if not path.is_file() or path.is_symlink() or path.stat().st_size != a["size"]:
            raise Problem("附件暂不可用，请稍后重试", 503)
        if hashlib.sha256(path.read_bytes()).hexdigest() != a["sha256"]:
            raise Problem("附件完整性检查失败", 503)
        return a, path

    def events(
        self,
        user_id,
        after=0,
        limit=50,
        pending=False,
        agent=False,
        *,
        newest=False,
        before=None,
    ):
        condition = " AND r.state='pending'" if pending else ""
        params = [user_id, max(0, after), time.time()]
        if agent:
            condition += " AND s.agent_enabled=1"
        if before is not None:
            condition += " AND e.seq<?"
            params.append(max(0, before))
        params.append(min(max(limit, 1), 100))
        return self.db.all(
            "SELECT e.*,r.state processing_state,c.withdrawn,v.number version FROM receipts r JOIN events e ON e.id=r.event_id JOIN subscriptions s ON s.user_id=r.user_id AND s.product_id=e.product_id LEFT JOIN contents c ON c.id=e.content_id LEFT JOIN versions v ON v.id=e.version_id WHERE r.user_id=? AND e.seq>? AND ((s.status='active' AND (s.expires_at IS NULL OR s.expires_at>?)) OR e.kind LIKE 'account:%')"
            + condition
            + (" ORDER BY e.seq DESC LIMIT ?" if newest else " ORDER BY e.seq LIMIT ?"),
            params,
        )

    def ack(self, binding, event_id):
        with self.db.transaction() as db:
            b = db.execute(
                "SELECT * FROM bindings WHERE id=? AND state='active'", (binding["id"],)
            ).fetchone()
            r = db.execute(
                "SELECT r.* FROM receipts r JOIN events e ON e.id=r.event_id JOIN subscriptions s ON s.product_id=e.product_id AND s.user_id=r.user_id WHERE r.user_id=? AND r.event_id=? AND s.agent_enabled=1 AND ((s.status='active' AND (s.expires_at IS NULL OR s.expires_at>?)) OR e.kind LIKE 'account:%')",
                (binding["user_id"], event_id, time.time()),
            ).fetchone()
            if not b or not r:
                raise Problem("事件不存在或当前连接无权处理", 404)
            db.execute(
                "UPDATE receipts SET state='processed',processed_at=?,binding_id=? WHERE user_id=? AND event_id=?",
                (time.time(), binding["id"], binding["user_id"], event_id),
            )
        return {"state": "processed"}

    def replay(self, event_id, user_id=None, *, apply=False):
        event = self.db.one("SELECT * FROM events WHERE id=?", (event_id,))
        if not event:
            raise Problem("事件不存在", 404)
        account_event = event["kind"].startswith("account:")
        if account_event:
            rows = self.db.all(
                "SELECT user_id FROM receipts WHERE event_id=?", (event_id,)
            )
            if user_id and user_id not in {r["user_id"] for r in rows}:
                raise Problem("不能把账号通知补投给其他用户", 403)
            if user_id:
                rows = [r for r in rows if r["user_id"] == user_id]
        else:
            rows = self.db.all(
                "SELECT s.user_id FROM subscriptions s WHERE s.product_id=? AND status='active' AND (expires_at IS NULL OR expires_at>?)"
                + (" AND s.user_id=?" if user_id else ""),
                (event["product_id"], time.time(), *((user_id,) if user_id else ())),
            )
        if apply:
            with self.db.transaction() as db:
                if not account_event:
                    self._fanout(db, event_id, event["product_id"], only_user=user_id)
                for row in rows:
                    db.execute(
                        "INSERT OR IGNORE INTO receipts(user_id,event_id) VALUES(?,?)",
                        (row["user_id"], event_id),
                    )
                # Never requeue successes or uncertain SMTP sends. Retry dead jobs explicitly.
                targets = db.execute(
                    "SELECT d.id,j.id job_id FROM deliveries d JOIN jobs j ON json_extract(j.payload,'$.delivery_id')=d.id WHERE d.event_id=? AND d.state='failed'"
                    + (" AND d.user_id=?" if user_id else ""),
                    (event_id, *((user_id,) if user_id else ())),
                ).fetchall()
                for row in targets:
                    db.execute(
                        "UPDATE deliveries SET state='queued' WHERE id=?", (row["id"],)
                    )
                    db.execute(
                        "UPDATE jobs SET state='queued',available_at=?,attempts=0 WHERE id=?",
                        (time.time(), row["job_id"]),
                    )
                audit(db, "admin", "replay", event_id, {"retried": len(targets)})
        return {
            "eligible_users": len(rows),
            "applied": apply,
            "note": "成功或状态不明的投递不会重发；发送前仍检查权益和偏好。",
        }
