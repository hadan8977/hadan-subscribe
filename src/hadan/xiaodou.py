"""Xiaodou RSA2 checkout. User scans; only verified payment facts grant access.

Protocol: xiaodou-official/xiaodou-open-platform, v1 (2026-10-17).
Private keys stay in a restricted server file. No provider SDK or extra service.
"""
from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from .db import audit, enqueue, uid
from .service import Problem

PLATFORM_KID = "xdpk-2026-09"
PLATFORM_FINGERPRINT = "c828f7e47c2eef5a958aa32009f5151bda43b585c979b40bf7c795b0b48d35f4"
CHANNELS = {"wechat": "WECHAT_H5", "alipay": "ALIPAY_H5"}


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def canonical_request(method, path, timestamp, nonce, app_id, kid, request_id, body=b""):
    # This adapter deliberately uses fixed ASCII paths, no query parameters.
    if not re.fullmatch(r"/api/open/v1/(?:payments(?:/[A-Za-z0-9_-]{6,64})?|settlement/accounts)", path):
        raise ValueError("Unsupported payment API path")
    return "\n".join(("XD-Signature-v1", method, path, "", timestamp, nonce,
                      app_id, kid, request_id, "application/json" if method == "POST" else "",
                      hashlib.sha256(body).hexdigest())).encode()


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON field")
            result[key] = value
        return result
    try:
        value = json.loads(raw, object_pairs_hook=pairs, parse_constant=lambda _: None)
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (ValueError, UnicodeError, RecursionError):
        raise Problem("付款凭证无效", 400) from None


def https_origin(value):
    try:
        p = urlsplit(value)
        return bool(p.scheme == "https" and p.hostname and p.port in (None, 443)
                    and not p.username and not p.password and not p.query and not p.fragment
                    and p.path in ("", "/") and not any(c.isspace() for c in value))
    except ValueError:
        return False


class Xiaodou:
    def __init__(self, service):
        self.s, self.cfg = service, service.settings

    def private_key(self):
        try:
            path = Path(self.cfg.xiaodou_private_key_file)
            if not path.is_absolute() or path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
                raise ValueError()
            key = serialization.load_pem_private_key(path.read_bytes(), password=None)
            if not isinstance(key, rsa.RSAPrivateKey) or key.key_size < 2048:
                raise ValueError()
            return key
        except (ValueError, TypeError, OSError):
            raise Problem("支付配置尚未完成", 503) from None

    def ready(self, *, receive_existing=False):
        if receive_existing:
            # Stopping checkout or losing a signing key must not discard paid callbacks.
            return bool(re.fullmatch(r"xdop_[A-Za-z0-9]{16}", self.cfg.xiaodou_app_id))
        basic = bool((self.cfg.xiaodou_enabled or receive_existing)
                     and re.fullmatch(r"xdop_[A-Za-z0-9]{16}", self.cfg.xiaodou_app_id)
                     and https_origin(self.cfg.xiaodou_base_url)
                     and https_origin(self.cfg.base_url)
                     and self.cfg.xiaodou_trade_scope == "CARD_VIRTUAL"
                     and self.cfg.xiaodou_cashier_origins
                     and all(https_origin(x) for x in self.cfg.xiaodou_cashier_origins)
                     and self.cfg.xiaodou_methods
                     and set(self.cfg.xiaodou_methods) <= set(CHANNELS))
        if not basic:
            return False
        try:
            self.private_key()
        except Problem:
            return False
        return True

    def platform_key(self, kid):
        if kid != PLATFORM_KID:
            raise Problem("支付签名密钥未确认", 503)
        pem = Path(__file__).with_name("static").joinpath("xiaodou-platform-public.pem").read_bytes()
        normalized = pem.replace(b"\r\n", b"\n").replace(b"\r", b"\n").strip()
        if hashlib.sha256(normalized).hexdigest() != PLATFORM_FINGERPRINT:
            raise Problem("支付签名密钥未确认", 503)
        return serialization.load_pem_public_key(pem)

    def verify(self, kid, signature, canonical):
        try:
            raw = base64.b64decode(signature, validate=True)
            self.platform_key(kid).verify(raw, canonical, padding.PKCS1v15(), hashes.SHA256())
        except (InvalidSignature, ValueError, TypeError):
            raise Problem("付款签名无效", 400) from None

    def api(self, method, path, *, body="", request_id=None):
        request_id = request_id or uid()
        timestamp, nonce = str(int(time.time())), uid()
        key = self.private_key()
        pem = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        kid = "mkid_" + hashlib.sha256(pem.strip()).hexdigest()[:16]
        raw = body.encode()
        canonical = canonical_request(method, path, timestamp, nonce, self.cfg.xiaodou_app_id, kid, request_id, raw)
        headers = {"X-XD-App-Id": self.cfg.xiaodou_app_id, "X-XD-Timestamp": timestamp,
                   "X-XD-Nonce": nonce, "X-XD-Key-Id": kid, "X-XD-Request-Id": request_id,
                   "X-XD-Sign": base64.b64encode(key.sign(canonical, padding.PKCS1v15(), hashes.SHA256())).decode()}
        if method == "POST":
            headers["Content-Type"] = "application/json"
        try:
            with httpx.Client(timeout=15, follow_redirects=False) as http:
                response = http.request(method, self.cfg.xiaodou_base_url + path, headers=headers, content=raw)
        except httpx.RequestError:
            raise Problem("支付状态确认中，请勿重复付款", 503) from None
        if len(response.content) > 128 * 1024:
            raise Problem("付款凭证无效", 502)
        h = response.headers
        signature = h.get("x-xd-response-sign", "")
        if signature:
            if h.get("x-xd-request-id") != request_id:
                raise Problem("付款凭证无效", 502)
            canonical = "\n".join(("XD-Response-v1", str(response.status_code),
                h.get("content-type", "").split(";", 1)[0].strip().lower(), request_id,
                h.get("x-xd-response-timestamp", ""), h.get("x-xd-response-nonce", ""),
                h.get("x-xd-response-key-id", ""), hashlib.sha256(response.content).hexdigest())).encode()
            self.verify(h.get("x-xd-response-key-id", ""), signature, canonical)
        elif 200 <= response.status_code < 300:
            # Unsigned pre-authentication errors are legitimate; success is never unsigned.
            raise Problem("付款凭证无效", 502)
        value = strict_json(response.content)
        if not 200 <= response.status_code < 300:
            code = value.get("code", "")
            if code == "OPEN_API_CHANNEL_UNAVAILABLE":
                raise Problem("该付款通道尚未开通，请选择其他方式", 503)
            if response.status_code == 409:
                raise Problem("支付状态确认中，请勿重复付款", 409)
            raise Problem("支付服务暂不可用，请稍后重试", 503)
        return value

    def create_order(self, user_id, plan_id, method, *, agreement_id=None, payer_ip=None, checkout_permission=None):
        from .commerce import Commerce
        from .terms import link_agreement
        if not self.ready() or method not in self.cfg.xiaodou_methods:
            raise Problem("该付款方式尚未开放", 503)
        try:
            if not ipaddress.ip_address(payer_ip).is_global:
                raise ValueError()
        except (ValueError, TypeError):
            raise Problem("请在订阅网页确认付款", 400) from None
        plan = next((p for p in Commerce(self.s).plans() if p["id"] == plan_id), None)
        if not plan:
            raise Problem("订阅方案不存在", 404)
        now = time.time()
        with self.s.db.transaction() as db:
            self.s._limit(db, "purchase:" + user_id, 8, 600)
            if checkout_permission:
                permission = db.execute("SELECT p.* FROM checkout_permissions p JOIN bindings b ON b.id=p.binding_id WHERE p.id=? AND p.user_id=? AND b.user_id=p.user_id AND b.state IN ('pending','active')", (checkout_permission, user_id)).fetchone()
                if not permission or permission["plan_id"] != plan_id or permission["method"] != method:
                    raise Problem("需要本人确认付款授权", 403)
                previous = db.execute("SELECT * FROM purchase_orders WHERE id=? AND user_id=? AND provider='xiaodou'", (checkout_permission, user_id)).fetchone()
                if previous and previous["status"] != "creating":
                    return self.public_order(dict(previous))
                if (not previous and permission["expires_at"] <= now) or permission["plan_snapshot"] != encoded(plan) or permission["payer_ip"] != payer_ip or permission["agreement_id"] != agreement_id:
                    raise Problem("付款授权已过期，请重新确认", 403)
            product = db.execute("SELECT id FROM products WHERE id='quantgt' AND active=1").fetchone()
            if not product:
                raise Problem("该内容暂不可开通", 409)
            sub = db.execute("SELECT * FROM subscriptions WHERE user_id=? AND product_id='quantgt'", (user_id,)).fetchone()
            if sub and sub["status"] == "active" and (sub["expires_at"] is None or
                (plan.get("access_until") and sub["expires_at"] >= plan["access_until"])):
                raise Problem("当前权益无需重复购买", 409)
            if sub and sub["billing_id"] and sub["auto_renew"]:
                raise Problem("请先在续费管理中停止自动续费", 409)
            if not sub:
                sid = uid()
                db.execute("INSERT INTO subscriptions(id,user_id,product_id,status,expires_at,created_at) VALUES(?,?,'quantgt','pending',0,?)", (sid, user_id, now))
                db.execute("INSERT INTO delivery_preferences VALUES(?,'always')", (sid,))
            current = None
            if checkout_permission:
                # Resume the exact provider request even after the local
                # 10-minute window; inserting the same id would violate the
                # purchase_orders primary key and could double-charge.
                current = db.execute(
                    "SELECT o.*,d.request_body,d.request_id,d.access_until,d.app_id "
                    "FROM purchase_orders o JOIN purchase_details d ON d.order_id=o.id "
                    "WHERE o.id=? AND o.user_id=? AND o.provider='xiaodou'",
                    (checkout_permission, user_id),
                ).fetchone()
            if current is None:
                current = db.execute(
                    "SELECT o.*,d.request_body,d.request_id,d.access_until,d.app_id FROM purchase_orders o "
                    "JOIN purchase_details d ON d.order_id=o.id WHERE o.user_id=? AND "
                    "(o.status IN ('verifying','review') OR (o.status IN ('creating','pending') AND o.expires_at>?)) "
                    "ORDER BY o.created_at DESC LIMIT 1",
                    (user_id, now),
                ).fetchone()
            if current:
                if checkout_permission and current["id"] != checkout_permission:
                    raise Problem(
                        "已有待确认订单，请先完成或核对原订单",
                        409,
                        code="order_exists",
                        data={"order_id": current["id"], "status": current["status"]},
                    )
                if current["provider"] != "xiaodou" or current["app_id"] != self.cfg.xiaodou_app_id or current["plan_id"] != plan_id or current["method"] != method:
                    raise Problem(
                        "已有待确认订单，请先完成或核对原订单",
                        409,
                        code="order_exists",
                        data={"order_id": current["id"], "status": current["status"]},
                    )
                if current["status"] != "creating":
                    return self.public_order(dict(current))
                order_id, request_id, body = current["id"], current["request_id"], current["request_body"]
            else:
                # Unknown in-flight payments from another gateway also block a second charge.
                pending = db.execute(
                    "SELECT id,status FROM purchase_orders WHERE user_id=? AND "
                    "(status IN ('verifying','review') OR (status IN ('creating','pending') AND expires_at>?)) "
                    "ORDER BY created_at DESC LIMIT 1",
                    (user_id, now),
                ).fetchone()
                if pending:
                    raise Problem(
                        "已有待确认订单，请先完成或核对原订单",
                        409,
                        code="order_exists",
                        data={"order_id": pending["id"], "status": pending["status"]},
                    )
                order_id, request_id = checkout_permission or uid(), uid()
                body = encoded({"outTradeNo": order_id, "channel": CHANNELS[method], "amountFen": plan["amount_minor"],
                    "currency": "CNY", "feeBearer": "SELLER", "subject": plan["title"],
                    "tradeScope": self.cfg.xiaodou_trade_scope, "payerClientIp": str(payer_ip),
                    "returnUrl": self.cfg.base_url + "/app?order=" + order_id})
                db.execute("INSERT INTO purchase_orders(id,user_id,product_id,plan_id,days,amount_minor,currency,method,provider,status,created_at,expires_at) VALUES(?,?,'quantgt',?,?,?,?,?,'xiaodou','creating',?,?)",
                    (order_id, user_id, plan_id, plan["days"], plan["amount_minor"], "CNY", method, now, now + 600))
                db.execute("INSERT INTO purchase_details(order_id,access_until,app_id,request_body,request_id) VALUES(?,?,?,?,?)",
                    (order_id, plan.get("access_until"), self.cfg.xiaodou_app_id, body, request_id))
            link_agreement(db, user_id, agreement_id, order_id)
        result = self.api("POST", "/api/open/v1/payments", body=body, request_id=request_id)
        self.apply_snapshot(order_id, result, materials=True)
        from .commerce import Commerce
        return Commerce(self.s).order(user_id, order_id)

    def public_order(self, row):
        detail = self.s.db.one("SELECT * FROM purchase_details WHERE order_id=?", (row["id"],))
        status = row["status"]
        if status in ("creating", "pending") and row["expires_at"] <= time.time():
            status = "expired"
        can_pay = status == "pending" and self.ready() and detail and detail["app_id"] == self.cfg.xiaodou_app_id
        return {"id": row["id"], "status": status, "paid": status == "paid", "method": row["method"],
                "amount_minor": row["amount_minor"], "currency": row["currency"], "expires_at": row["expires_at"],
                "access_until": detail["access_until"] if detail else None,
                "pay_url": detail["pay_url"] if can_pay else None,
                "qr_url": "/api/orders/" + row["id"] + "/qr" if row["qr_data"] and can_pay else None}

    def _payment_url(self, value, *, alipay=False):
        if not isinstance(value, str) or len(value) > 8192 or any(c.isspace() for c in value):
            return None
        try:
            p = urlsplit(value)
            origin = p.scheme + "://" + p.netloc
            allowed = (p.hostname == "qr.alipay.com" and p.path.startswith("/")) if alipay else origin in self.cfg.xiaodou_cashier_origins
            return value if allowed and p.scheme == "https" and p.port in (None, 443) and not p.username and not p.password and not p.fragment else None
        except ValueError:
            return None

    def _row(self, db, order_id):
        if not isinstance(order_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{6,64}", order_id):
            raise Problem("付款订单不存在", 404)
        row = db.execute("SELECT o.*,d.access_until,d.app_id,d.request_body FROM purchase_orders o JOIN purchase_details d ON d.order_id=o.id WHERE o.id=? AND o.provider='xiaodou'", (order_id,)).fetchone()
        if not row or row["app_id"] != self.cfg.xiaodou_app_id:
            raise Problem("付款订单不存在", 404)
        return row

    def _review(self, db, order_id, summary):
        now = time.time()
        fingerprint = hashlib.sha256(("xiaodou-review:" + order_id).encode()).hexdigest()
        existing = db.execute("SELECT id FROM incidents WHERE fingerprint=?", (fingerprint,)).fetchone()
        if existing:
            db.execute("UPDATE incidents SET state='open',count=count+1,updated_at=? WHERE id=?", (now, existing["id"]))
            return
        iid = uid()
        db.execute("INSERT INTO incidents(id,fingerprint,kind,summary,details,created_at,updated_at) VALUES(?,?,'payment_review',?,?,?,?)",
                   (iid, fingerprint, summary, "order_id=" + order_id, now, now))
        if self.cfg.ops_ai:
            enqueue(db, "diagnose", {"incident_id": iid}, "diagnose:" + iid, queue="ops")

    def _settle(self, db, row):
        from .commerce import Commerce
        if row["status"] == "closed" or db.execute("SELECT id FROM payments WHERE provider_event=?", ("xiaodou:" + row["id"],)).fetchone():
            return
        # Payment identity is the local order, shared by callback and signed-query recovery.
        db.execute("INSERT INTO payments VALUES(?,?,?,?,?,?,?)", (uid(), row["user_id"], row["product_id"],
            "xiaodou:" + row["id"], row["amount_minor"], "cny", time.time()))
        try:
            if db.execute("SELECT refund_id FROM purchase_refunds WHERE provider='xiaodou' AND order_id=? AND status='succeeded'", (row["id"],)).fetchone():
                raise Problem("付款已有退款，权益待核对", 409)
            if not db.execute("SELECT id FROM products WHERE id=? AND active=1", (row["product_id"],)).fetchone():
                raise Problem("该内容暂不可开通", 409)
            end = Commerce(self.s)._grant(db, row["user_id"], row["product_id"], row["days"], access_until=row["access_until"])
            status, title = "paid", "Quant · 订阅已开通"
            body = "付款已确认，订阅权益已开通。无自动续费。"
            audit(db, "billing", "purchase_paid", row["id"], {"expires_at": end, "amount_minor": row["amount_minor"]})
        except Problem:
            status, title, body = "review", "Quant · 付款待核对", "已确认付款，订阅权益待核对。请勿重复付款。"
            audit(db, "billing", "paid_order_needs_review", row["id"])
            self._review(db, row["id"], "Xiaodou payment requires entitlement reconciliation")
        db.execute("UPDATE purchase_orders SET status=?,paid_at=?,qr_data=NULL WHERE id=?", (status, time.time(), row["id"]))
        db.execute("UPDATE purchase_details SET pay_url=NULL WHERE order_id=?", (row["id"],))
        self.s.account_notice(db, row["user_id"], row["product_id"], "payment_succeeded" if status == "paid" else "payment_review",
            title, body, "xiaodou-notice:" + row["id"])

    def apply_snapshot(self, order_id, value, *, materials=False):
        with self.s.db.transaction() as db:
            row = self._row(db, order_id)
            if (value.get("outTradeNo") != order_id or type(value.get("amountFen")) is not int
                or value["amountFen"] != row["amount_minor"] or value.get("currency") != "CNY"
                or value.get("channel") != CHANNELS[row["method"]] or value.get("feeBearer", "SELLER") != "SELLER"):
                raise Problem("付款金额或订单不匹配", 400)
            remote_id = value.get("openOrderId")
            if not isinstance(remote_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", remote_id):
                raise Problem("付款凭证无效", 400)
            # Prefixing provides a namespace without rewriting historical remote-id constraints.
            remote_id = "xiaodou:" + self.cfg.xiaodou_app_id + ":" + remote_id
            if row["remote_id"] and row["remote_id"] != remote_id:
                raise Problem("付款金额或订单不匹配", 400)
            other = db.execute("SELECT id FROM purchase_orders WHERE remote_id=? AND id<>?", (remote_id, order_id)).fetchone()
            if other:
                raise Problem("付款金额或订单不匹配", 400)
            db.execute("UPDATE purchase_orders SET remote_id=? WHERE id=?", (remote_id, order_id))
            state = value.get("status")
            if state == "SUCCEEDED":
                self._settle(db, row)
                return
            if row["status"] in ("paid", "review", "closed"):
                return
            if state in ("CLOSED", "FAILED"):
                # These provider states can still be promoted by a late confirmed payment.
                db.execute("UPDATE purchase_orders SET status='expired',qr_data=NULL WHERE id=?", (order_id,))
                db.execute("UPDATE purchase_details SET pay_url=NULL WHERE order_id=?", (order_id,))
                return
            if state not in ("CREATED", "PAYING", "UNKNOWN"):
                raise Problem("付款凭证无效", 400)
            if state == "UNKNOWN" or value.get("unknown") is True:
                db.execute("UPDATE purchase_orders SET status='verifying',qr_data=NULL WHERE id=?", (order_id,))
                db.execute("UPDATE purchase_details SET pay_url=NULL WHERE order_id=?", (order_id,))
                return
            if not materials or row["status"] != "creating":
                # Request-id replays return the FIRST response, not the current state.
                # Never restore an old payable QR after a newer uncertain/closed event.
                return
            try:
                expiry = datetime.fromisoformat(value["expireAt"].replace("Z", "+00:00"))
                if expiry.tzinfo is None:
                    raise ValueError()
                expires_at = expiry.timestamp()
            except (KeyError, ValueError, TypeError, OverflowError):
                raise Problem("付款凭证无效", 400) from None
            pay_url = self._payment_url(value.get("payUrl"))
            qr = self._payment_url(value.get("qrCode"), alipay=True) if row["method"] == "alipay" else None
            qr = qr or pay_url
            if not qr or not pay_url:
                raise Problem("付款链接验证失败", 502)
            db.execute("UPDATE purchase_orders SET status='pending',qr_data=?,expires_at=? WHERE id=?", (qr, expires_at, order_id))
            db.execute("UPDATE purchase_details SET pay_url=? WHERE order_id=?", (pay_url, order_id))

    def notify(self, raw, headers):
        if not self.ready(receive_existing=True):
            raise Problem("该付款方式尚未开放", 503)
        names = ("event-id", "event-type", "timestamp", "nonce", "key-id")
        values = [headers.get("x-xd-webhook-" + k, "") for k in names]
        if any(not re.fullmatch(r"[!-~]{1,160}", x) for x in values):
            raise Problem("付款凭证无效", 400)
        event_id, kind, timestamp, nonce, kid = values
        # Local freshness policy: retries are re-signed with a fresh delivery timestamp.
        if not timestamp.isdigit() or abs(time.time() - int(timestamp)) > 300:
            raise Problem("付款通知已过期", 400)
        canonical = "\n".join(("XD-Webhook-v1", *values, hashlib.sha256(raw).hexdigest())).encode()
        self.verify(kid, headers.get("x-xd-webhook-signature", ""), canonical)
        value = strict_json(raw)
        if value.get("appId") != self.cfg.xiaodou_app_id or value.get("eventId") != event_id or value.get("eventType") != kind:
            raise Problem("付款凭证无效", 400)
        with self.s.db.transaction() as db:
            row = self._row(db, value.get("outTradeNo"))
            body_digest = hashlib.sha256(raw).hexdigest()
            seen = db.execute("SELECT order_id,body_digest FROM purchase_events WHERE provider='xiaodou' AND event_id=?", (event_id,)).fetchone()
            if seen:
                if seen["order_id"] != row["id"] or seen["body_digest"] != body_digest:
                    raise Problem("付款凭证无效", 400)
                return {"received": True}
            if type(value.get("grossAmountFen")) is not int or value["grossAmountFen"] != row["amount_minor"]:
                raise Problem("付款金额或订单不匹配", 400)
            if kind == "PAYMENT_SUCCEEDED":
                if value.get("status") != "SUCCEEDED" or type(value.get("eventAmountFen")) is not int or value["eventAmountFen"] != row["amount_minor"] or value.get("feeBearer", "SELLER") != "SELLER":
                    raise Problem("付款金额或订单不匹配", 400)
                self._settle(db, row)
            elif kind in ("PAYMENT_CLOSED", "PAYMENT_FAILED"):
                if row["status"] not in ("paid", "review", "closed"):
                    db.execute("UPDATE purchase_orders SET status='expired',qr_data=NULL WHERE id=?", (row["id"],))
                    db.execute("UPDATE purchase_details SET pay_url=NULL WHERE order_id=?", (row["id"],))
            elif kind in ("REFUND_SUCCEEDED", "REFUND_FAILED"):
                refund_id, amount = value.get("outRefundNo"), value.get("eventAmountFen")
                state = "SUCCEEDED" if kind == "REFUND_SUCCEEDED" else "FAILED"
                if (not isinstance(refund_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{6,64}", refund_id)
                    or type(amount) is not int or not 0 < amount <= row["amount_minor"] or value.get("status") != state):
                    raise Problem("付款凭证无效", 400)
                previous = db.execute("SELECT * FROM purchase_refunds WHERE provider='xiaodou' AND refund_id=?", (refund_id,)).fetchone()
                if previous and (previous["order_id"] != row["id"] or previous["amount_minor"] != amount):
                    raise Problem("付款金额或订单不匹配", 400)
                if not previous:
                    db.execute("INSERT INTO purchase_refunds VALUES('xiaodou',?,?,?,?,?,?)", (refund_id, row["id"], amount, state.lower(), event_id, time.time()))
                elif state == "SUCCEEDED":
                    db.execute("UPDATE purchase_refunds SET status='succeeded',event_id=? WHERE provider='xiaodou' AND refund_id=?", (event_id, refund_id))
                if state == "SUCCEEDED":
                    db.execute("UPDATE purchase_orders SET status='review',qr_data=NULL WHERE id=? AND status<>'closed'", (row["id"],))
                    db.execute("UPDATE purchase_details SET pay_url=NULL WHERE order_id=?", (row["id"],))
                # An external refund must be reconciled against any later/manual grants.
                # The model cannot silently revoke unrelated access or initiate another refund.
                self._review(db, row["id"], "Xiaodou refund requires operator reconciliation")
                audit(db, "billing", "provider_refund_notice", row["id"], {"event_id": event_id, "refund_id": refund_id, "amount_minor": amount, "kind": kind})
            else:
                raise Problem("付款凭证无效", 400)
            db.execute("INSERT INTO purchase_events VALUES('xiaodou',?,?,?,?)", (event_id, row["id"], body_digest, time.time()))
        return {"received": True}

    def reconcile(self, order_id):
        if not self.ready(receive_existing=True):
            return {"skipped": "provider unavailable"}
        row = self.s.db.one("SELECT o.status,d.app_id FROM purchase_orders o JOIN purchase_details d ON d.order_id=o.id WHERE o.id=? AND o.provider='xiaodou'", (order_id,))
        if not row or row["app_id"] != self.cfg.xiaodou_app_id or row["status"] in ("paid", "review", "closed"):
            return {"skipped": "terminal or absent"}
        result = self.api("GET", "/api/open/v1/payments/" + order_id)
        self.apply_snapshot(order_id, result)
        local = self.s.db.one("SELECT status FROM purchase_orders WHERE id=?", (order_id,))
        return {"status": result.get("status"), "needs_review": result.get("status") == "UNKNOWN" or local["status"] == "review"}
