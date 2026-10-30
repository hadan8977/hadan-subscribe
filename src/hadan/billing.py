from __future__ import annotations

import hmac
import json
import time
import httpx

from .db import audit, enqueue, uid
from .security import mac
from .service import Problem


class Billing:
    """Hosted Stripe checkout; provider events are verified and reconciled server-side."""

    def __init__(self, service):
        self.s = service
        self.cfg = service.settings

    def api(self, method, path, data=None, identity=None):
        if not self.cfg.stripe_key:
            raise Problem("支付尚未配置，请联系管理员", 503)
        headers = {"Authorization": "Bearer " + self.cfg.stripe_key}
        if identity:
            headers["Idempotency-Key"] = identity
        with httpx.Client(timeout=20, follow_redirects=False) as client:
            r = client.request(
                method, "https://api.stripe.com/v1/" + path, data=data, headers=headers
            )
            if r.status_code >= 400:
                raise RuntimeError(f"Billing provider HTTP {r.status_code}")
            return r.json()

    def checkout(self, user_id, product_id, *, agreement_id=None):
        from .terms import TERMS_VERSION, link_agreement
        p = self.s.db.one(
            "SELECT * FROM products WHERE id=? AND active=1", (product_id,)
        )
        if not p or not p["paid"] or not p["price_id"]:
            raise Problem("该内容尚未配置付费订阅", 409)
        sub = self.s.db.one(
            "SELECT * FROM subscriptions WHERE user_id=? AND product_id=?",
            (user_id, product_id),
        )
        if sub and sub["billing_id"]:
            raise Problem("已有付费订阅，请在续费管理中操作", 409)
        user = self.s.db.one("SELECT * FROM users WHERE id=?", (user_id,))
        value = self.api(
            "POST",
            "checkout/sessions",
            {
                "mode": "subscription",
                "customer_email": user["email"],
                "client_reference_id": user_id,
                "line_items[0][price]": p["price_id"],
                "line_items[0][quantity]": "1",
                "subscription_data[metadata][user_id]": user_id,
                "subscription_data[metadata][product_id]": product_id,
                "success_url": self.cfg.base_url + "/app?payment=processing",
                "cancel_url": self.cfg.base_url + "/app",
                **({"metadata[consent_id]": agreement_id,
                    "metadata[terms_version]": TERMS_VERSION,
                    "subscription_data[metadata][consent_id]": agreement_id,
                    "subscription_data[metadata][terms_version]": TERMS_VERSION} if agreement_id else {}),
            },
            identity="checkout:" + agreement_id if agreement_id else "checkout:"
            + user_id
            + ":"
            + product_id
            + ":"
            + str(int(time.time() // 1800)),
        )
        with self.s.db.transaction() as db:
            sid = uid()
            created = db.execute(
                "INSERT OR IGNORE INTO subscriptions(id,user_id,product_id,status,expires_at,email_enabled,agent_enabled,auto_renew,created_at) VALUES(?,?,?,'pending',0,1,1,1,?)",
                (sid, user_id, product_id, time.time()),
            ).rowcount
            if created:
                db.execute(
                    "INSERT INTO delivery_preferences VALUES(?,'always')", (sid,)
                )
            if agreement_id:
                if not value.get("id"):
                    raise Problem("支付服务暂不可用，请稍后重试", 503)
                link_agreement(db, user_id, agreement_id, value["id"])
        return {"url": value["url"]}

    def portal(self, user_id):
        sub = self.s.db.one(
            "SELECT * FROM subscriptions WHERE user_id=? AND billing_id IS NOT NULL LIMIT 1",
            (user_id,),
        )
        if not sub:
            raise Problem("没有需要管理的付费订阅", 404)
        remote = self.api("GET", "subscriptions/" + sub["billing_id"])
        return {
            "url": self.api(
                "POST",
                "billing_portal/sessions",
                {
                    "customer": remote["customer"],
                    "return_url": self.cfg.base_url + "/app",
                },
            )["url"]
        }

    def cancel(self, subscription_id, request_id=None):
        s = self.s.db.one("SELECT * FROM subscriptions WHERE id=?", (subscription_id,))
        if s and s["billing_id"] and s["cancel_requested"]:
            if request_id and request_id != s["cancel_request_id"]:
                return
            request_id = request_id or s["cancel_request_id"] or "legacy"
            self.api(
                "POST",
                "subscriptions/" + s["billing_id"],
                {"cancel_at_period_end": "true"},
                identity="cancel:" + s["billing_id"] + ":" + request_id,
            )
            self.s.db.execute(
                "UPDATE subscriptions SET cancel_requested=0,cancel_request_id=NULL,auto_renew=0 WHERE id=? AND cancel_requested=1 AND (cancel_request_id=? OR cancel_request_id IS NULL)",
                (subscription_id, request_id),
            )

    def webhook(self, body, signature):
        if not self.cfg.stripe_webhook_secret:
            raise Problem("支付回调尚未配置", 503)
        fields = {}
        for part in signature.split(","):
            key, _, value = part.partition("=")
            fields.setdefault(key, []).append(value)
        try:
            stamp = int(fields["t"][0])
        except (KeyError, ValueError):
            raise Problem("签名无效", 400)
        expected = mac(self.cfg.stripe_webhook_secret, str(stamp) + "." + body.decode())
        if abs(time.time() - stamp) > 300 or not any(
            hmac.compare_digest(expected, x) for x in fields.get("v1", [])
        ):
            raise Problem("签名无效", 400)
        event = json.loads(body)
        if not isinstance(event, dict) or not event.get("id"):
            raise Problem("事件无效")
        with self.s.db.transaction() as db:
            if db.execute(
                "SELECT id FROM billing_events WHERE id=?", (event["id"],)
            ).fetchone():
                return {"received": True, "duplicate": True}
            obj = event.get("data", {}).get("object", {})
            kind = event.get("type", "")
            remote_id = None
            if kind.startswith("customer.subscription."):
                remote_id = obj.get("id")
            elif kind.startswith("invoice."):
                remote_id = obj.get("subscription") or obj.get("parent", {}).get(
                    "subscription_details", {}
                ).get("subscription")
            elif kind == "checkout.session.completed":
                remote_id = obj.get("subscription")
            if remote_id:
                enqueue(
                    db,
                    "billing_reconcile",
                    {
                        "billing_id": remote_id,
                        "event_id": event["id"],
                        "event_kind": kind,
                    },
                    "billing:" + event["id"],
                )
            db.execute(
                "INSERT INTO billing_events VALUES(?,?)", (event["id"], time.time())
            )
        return {"received": True}

    def reconcile(self, billing_id, event_id, event_kind="periodic"):
        remote = self.api("GET", "subscriptions/" + billing_id)
        meta = remote.get("metadata", {})
        user_id = meta.get("user_id")
        product_id = meta.get("product_id")
        user = self.s.db.one("SELECT * FROM users WHERE id=?", (user_id,))
        product = self.s.db.one("SELECT * FROM products WHERE id=?", (product_id,))
        if not user or not product:
            raise Problem("支付元数据无法匹配用户与内容")
        items = remote.get("items", {}).get("data", [])
        if not items or any(
            x.get("price", {}).get("id") != product["price_id"] for x in items
        ):
            raise Problem("支付商品不匹配")
        end = remote.get("current_period_end") or max(
            x.get("current_period_end", 0) for x in items
        )
        invoice = remote.get("latest_invoice")
        if isinstance(invoice, str):
            invoice = self.api("GET", "invoices/" + invoice)
        paid = (
            isinstance(invoice, dict)
            and invoice.get("status") == "paid"
            and remote.get("status") in ("active", "past_due", "canceled")
        )
        if not isinstance(end, (int, float)) or end <= 0:
            raise Problem("付款周期无效")
        now = time.time()
        with self.s.db.transaction() as db:
            existing = db.execute(
                "SELECT * FROM subscriptions WHERE user_id=? AND product_id=?",
                (user_id, product_id),
            ).fetchone()
            sid = existing["id"] if existing else uid()
            if existing:
                if existing["billing_id"] and existing["billing_id"] != billing_id:
                    raise Problem("同一内容存在不同付款订阅，请对账后处理", 409)
                # An authenticated provider mapping is needed to cancel an unpaid
                # checkout too. It grants no entitlement by itself.
                db.execute(
                    "UPDATE subscriptions SET billing_id=? WHERE id=?",
                    (billing_id, sid),
                )
            elif not paid:
                db.execute(
                    "INSERT INTO subscriptions(id,user_id,product_id,status,expires_at,billing_id,auto_renew,created_at) VALUES(?,?,?,'pending',0,?,?,?)",
                    (
                        sid,
                        user_id,
                        product_id,
                        billing_id,
                        int(not remote.get("cancel_at_period_end")),
                        now,
                    ),
                )
                db.execute(
                    "INSERT INTO delivery_preferences VALUES(?,'always')", (sid,)
                )
            if paid:
                if existing:
                    # Payment never opts the user back into content delivery.
                    db.execute(
                        "UPDATE subscriptions SET status='active',expires_at=MAX(COALESCE(expires_at,0),?),billing_id=?,auto_renew=CASE WHEN cancel_requested=1 OR ? THEN 0 ELSE 1 END WHERE id=?",
                        (
                            end,
                            billing_id,
                            bool(remote.get("cancel_at_period_end")),
                            sid,
                        ),
                    )
                else:
                    db.execute(
                        "INSERT INTO subscriptions(id,user_id,product_id,expires_at,billing_id,auto_renew,created_at) VALUES(?,?,?,?,?,?,?)",
                        (
                            sid,
                            user_id,
                            product_id,
                            end,
                            billing_id,
                            int(not remote.get("cancel_at_period_end")),
                            now,
                        ),
                    )
                    db.execute(
                        "INSERT INTO delivery_preferences VALUES(?,'always')", (sid,)
                    )
                created = db.execute(
                    "INSERT OR IGNORE INTO payments VALUES(?,?,?,?,?,?,?)",
                    (
                        uid(),
                        user_id,
                        product_id,
                        invoice["id"],
                        invoice.get("amount_paid", 0),
                        invoice.get("currency", "usd"),
                        now,
                    ),
                ).rowcount
                if created:
                    self.s.account_notice(
                        db,
                        user_id,
                        product_id,
                        "payment_succeeded",
                        "Hadan · 订阅付款成功",
                        "订阅权益已更新。你之前设置的邮件和 Agent 接收偏好保持不变。",
                        "payment-notice:" + invoice["id"],
                    )
            if existing and (
                remote.get("cancel_at_period_end") or remote.get("status") == "canceled"
            ):
                db.execute(
                    "UPDATE subscriptions SET auto_renew=0,cancel_requested=0,cancel_request_id=NULL WHERE id=?",
                    (sid,),
                )
            if (
                existing
                and existing["cancel_requested"]
                and not remote.get("cancel_at_period_end")
                and remote.get("status") != "canceled"
            ):
                request_id = existing["cancel_request_id"] or "legacy"
                enqueue(
                    db,
                    "billing_cancel",
                    {"subscription_id": sid, "request_id": request_id},
                    "cancel:" + billing_id + ":" + request_id,
                )
            audit(
                db,
                "billing",
                "reconciled",
                billing_id,
                {
                    "provider_event": event_id,
                    "paid": paid,
                    "status": remote.get("status"),
                },
            )
            if event_kind == "invoice.payment_failed":
                self.s.account_notice(
                    db,
                    user_id,
                    product_id,
                    "payment_failed",
                    "Hadan · 续费未成功",
                    "续费未成功，请前往订阅页面更新支付方式。已付权益保留至到期。",
                    "account:" + event_id,
                )
