"""One-time access purchases and owner-issued codes. No automatic spending."""
from __future__ import annotations

import csv
import hashlib
import hmac
import os
import re
import secrets
import struct
import time
import zlib
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .db import audit, uid
from .security import digest
from .service import Problem


def code_digest(value):
    normalized = re.sub(r"[\s-]", "", str(value)).upper()
    if not re.fullmatch(r"QNT[A-HJ-NP-Z2-9]{24}", normalized):
        raise Problem("兑换码无效或已使用", 400)
    return digest(normalized)


def amount_minor(value):
    try:
        amount = Decimal(str(value)) * 100
        if not amount.is_finite() or amount != amount.to_integral_value() or not 0 < amount <= 1_000_000:
            raise ValueError()
        return int(amount)
    except (InvalidOperation, ValueError, OverflowError):
        raise Problem("付款金额不匹配", 400)


def qr_png(value: str, *, box_size: int = 10, border: int = 4) -> bytes:
    """Render a QR code as an opaque RGB PNG.

    SVG path-only QR images have a transparent background, which becomes
    unreadable in dark chat clients. A white quiet zone and white modules keep
    the downloaded image scannable everywhere.
    """
    import qrcode

    code = qrcode.QRCode(
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=1,
        border=0,
    )
    code.add_data(value)
    code.make(fit=True)
    matrix = code.get_matrix()
    modules = len(matrix)
    side = (modules + border * 2) * box_size
    white, black = b"\xff\xff\xff", b"\x00\x00\x00"
    rows = bytearray()
    for y in range(-border, modules + border):
        row = bytearray()
        if 0 <= y < modules:
            source = matrix[y]
            for x in range(-border, modules + border):
                row.extend((black if 0 <= x < modules and source[x] else white) * box_size)
        else:
            row.extend(white * box_size * (modules + border * 2))
        for _ in range(box_size):
            rows.append(0)
            rows.extend(row)

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", side, side, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(bytes(rows), 9))
        + chunk(b"IEND", b"")
    )


class Commerce:
    def __init__(self, service):
        self.s = service
        self.cfg = service.settings

    def plans(self):
        if not isinstance(self.cfg.payment_plans, list) or len(self.cfg.payment_plans) > 10:
            raise Problem("订阅方案配置无效", 503)
        result, seen = [], set()
        for raw in self.cfg.payment_plans:
            if (not isinstance(raw, dict) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,40}", str(raw.get("id", "")))
                or raw["id"] in seen
                or type(raw.get("amount_minor")) is not int or not 1 <= raw["amount_minor"] <= 1_000_000
                or raw.get("currency", "CNY") != "CNY"):
                raise Problem("订阅方案配置无效", 503)
            fixed = raw.get("valid_until")
            extra = {}
            if fixed is not None:
                try:
                    end = datetime.fromisoformat(fixed)
                    if end.tzinfo is None or "days" in raw:
                        raise ValueError()
                    extra = {"access_until": end.timestamp(), "valid_until": end.isoformat()}
                except (ValueError, TypeError, OverflowError):
                    raise Problem("订阅方案配置无效", 503) from None
                # A fixed calendar offer must stop selling when its service period ends.
                if end.timestamp() <= time.time():
                    continue
            elif type(raw.get("days")) is not int or not 1 <= raw["days"] <= 365:
                raise Problem("订阅方案配置无效", 503)
            seen.add(raw["id"])
            result.append({"id": raw["id"], "title": str(raw.get("title", "Quant"))[:80],
                           "days": raw.get("days", 0), "amount_minor": raw["amount_minor"], "currency": "CNY", **extra})
        return result

    def provider_ready(self, *, receive_existing=False):
        if self.cfg.xiaodou_enabled and not receive_existing:
            from .xiaodou import Xiaodou
            return Xiaodou(self.s).ready()
        return bool((self.cfg.xorpay_enabled or receive_existing) and re.fullmatch(r"[0-9]{1,20}", self.cfg.xorpay_aid)
                    and self.cfg.xorpay_secret and self.cfg.base_url.startswith("https://"))

    def options(self):
        plans = self.plans()
        xianyu = urlsplit(self.cfg.xianyu_url)
        link_ok = (xianyu.scheme == "https" and xianyu.hostname in {"www.goofish.com", "goofish.com", "m.goofish.com", "2.taobao.com"}
                   and xianyu.username is None)
        ready = self.provider_ready() and bool(plans)
        methods = self.cfg.xiaodou_methods if self.cfg.xiaodou_enabled else ("wechat", "alipay")
        return {"methods": [{"id": "code", "enabled": True},
                            {"id": "xianyu", "enabled": bool(link_ok), "url": self.cfg.xianyu_url if link_ok else None},
                            {"id": "wechat", "enabled": ready and "wechat" in methods}, {"id": "alipay", "enabled": ready and "alipay" in methods},
                            {"id": "usdt", "enabled": False}],
                "plans": plans if ready else [], "automatic_renewal": False}

    def _grant(self, db, user_id, product_id, days, *, access_until=None):
        now = time.time()
        sub = db.execute("SELECT * FROM subscriptions WHERE user_id=? AND product_id=?", (user_id, product_id)).fetchone()
        if sub and sub["status"] == "active" and sub["expires_at"] is None:
            raise Problem("你已拥有长期权益，无需再次开通", 409)
        if sub and sub["billing_id"] and sub["auto_renew"]:
            raise Problem("请先在续费管理中停止自动续费", 409)
        if access_until is not None:
            if access_until <= now or (sub and (sub["expires_at"] or 0) >= access_until):
                raise Problem("当前权益无需重复购买", 409)
            end = access_until
        else:
            end = max(now, sub["expires_at"] or 0) + days * 86400 if sub else now + days * 86400
        if sub:
            db.execute("UPDATE subscriptions SET status='active',expires_at=? WHERE id=?", (end, sub["id"]))
        else:
            sid = uid()
            db.execute("INSERT INTO subscriptions(id,user_id,product_id,expires_at,created_at) VALUES(?,?,?,?,?)", (sid, user_id, product_id, end, now))
            db.execute("INSERT INTO delivery_preferences VALUES(?,'always')", (sid,))
        return end

    def issue_codes(self, *, product_id="quantgt", days, count, output, expires_at=None, actor="cli"):
        if type(days) is not int or not 1 <= days <= 3650 or type(count) is not int or not 1 <= count <= 1000:
            raise ValueError("Codes require 1–3650 days and 1–1000 items")
        if expires_at is not None and expires_at <= time.time():
            raise ValueError("Code expiry must be in the future")
        target = Path(output)
        if target.exists() or target.is_symlink():
            raise ValueError("Code output already exists; never overwrite issued codes")
        if not self.s.db.one("SELECT id FROM products WHERE id=?", (product_id,)):
            raise ValueError("Unknown product")
        batch = uid()
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        codes = []
        for _ in range(count):
            value = "".join(secrets.choice(alphabet) for _ in range(24))
            codes.append("QNT-" + "-".join(value[i:i+4] for i in range(0, 24, 4)))
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name("." + target.name + "." + uid())
        try:
            with open(tmp, "x", newline="") as stream:
                os.chmod(tmp, 0o600)
                writer = csv.writer(stream)
                writer.writerow(["batch_id", "code", "product", "days", "expires_at"])
                writer.writerows((batch, code, product_id, days, expires_at or "") for code in codes)
                stream.flush(); os.fsync(stream.fileno())
            with self.s.db.transaction() as db:
                db.execute("INSERT INTO code_batches VALUES(?,?,?,?,?,?,?)", (batch, product_id, days, count, expires_at, actor, time.time()))
                db.executemany("INSERT INTO redemption_codes(digest,batch_id,created_at) VALUES(?,?,?)", ((code_digest(code), batch, time.time()) for code in codes))
                # An exclusive filesystem publication cannot replace somebody else's export.
                os.link(tmp, target)
                audit(db, actor, "codes_issued", batch, {"quantity": count, "days": days})
        except BaseException:
            # Only remove this invocation's file, never a competing export.
            if target.exists() and tmp.exists() and os.path.samefile(tmp, target):
                target.unlink()
            raise
        finally:
            tmp.unlink(missing_ok=True)
        return {"batch_id": batch, "quantity": count, "days": days, "output": str(target), "plaintext_in_database": False}

    def revoke_batch(self, batch_id, actor="cli"):
        with self.s.db.transaction() as db:
            count = db.execute("UPDATE redemption_codes SET state='revoked' WHERE batch_id=? AND state='active'", (batch_id,)).rowcount
            audit(db, actor, "codes_revoked", batch_id, {"unused": count})
        return {"revoked_unused": count}

    def redeem(self, user_id, raw_code):
        # Charge failed attempts too; don't rollback this budget with invalid-code errors.
        with self.s.db.transaction() as db:
            self.s._limit(db, "redeem:" + user_id, 12, 3600)
        key = code_digest(raw_code)
        with self.s.db.transaction() as db:
            code = db.execute("SELECT c.*,b.product_id,b.days,b.expires_at FROM redemption_codes c JOIN code_batches b ON b.id=c.batch_id WHERE c.digest=?", (key,)).fetchone()
            if not code or code["state"] != "active" or (code["expires_at"] and code["expires_at"] <= time.time()):
                raise Problem("兑换码无效或已使用", 400)
            product = db.execute("SELECT active FROM products WHERE id=?", (code["product_id"],)).fetchone()
            if not product or not product["active"]:
                raise Problem("该内容暂不可开通", 409)
            end = self._grant(db, user_id, code["product_id"], code["days"])
            db.execute("UPDATE redemption_codes SET state='redeemed',redeemed_by=?,redeemed_at=? WHERE digest=?", (user_id, time.time(), key))
            audit(db, user_id, "code_redeemed", code["batch_id"], {"expires_at": end})
            self.s.account_notice(db, user_id, code["product_id"], "access_granted", "Quant · 订阅已开通", "兑换成功，权益已更新；原有接收偏好保持不变。", "redeem-notice:" + key)
        return {"expires_at": end, "message": "兑换成功，订阅权益已更新"}

    def api(self, method, path, data=None):
        try:
            with httpx.Client(timeout=20, follow_redirects=False) as http:
                response = http.request(method, "https://xorpay.com/api/" + path, data=data)
            if response.status_code != 200:
                raise Problem("支付服务暂不可用，请稍后重试", 503)
            try:
                value = response.json()
            except ValueError:
                raise Problem("支付服务暂不可用，请稍后重试", 503)
            if not isinstance(value, dict):
                raise Problem("支付服务暂不可用，请稍后重试", 503)
            return value
        except httpx.RequestError:
            raise Problem("支付服务暂不可用，请稍后重试", 503) from None

    def _remote_id(self, value):
        value = str(value)
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
            raise Problem("付款通知无效", 400)
        return value

    def _unique_remote(self, db, remote_id, order_id):
        if db.execute("SELECT id FROM purchase_orders WHERE remote_id=? AND id<>?", (remote_id, order_id)).fetchone():
            raise Problem("付款金额或订单不匹配", 400)

    def sign(self, *values):
        # Required by XorPay's official protocol; the secret never goes to a browser.
        return hashlib.md5(("".join(values) + self.cfg.xorpay_secret).encode()).hexdigest()

    def create_order(self, user_id, plan_id, method, *, agreement_id=None, payer_ip=None, checkout_permission=None):
        if self.cfg.xiaodou_enabled:
            from .xiaodou import Xiaodou
            return Xiaodou(self.s).create_order(user_id, plan_id, method, agreement_id=agreement_id, payer_ip=payer_ip,
                                               checkout_permission=checkout_permission)
        if checkout_permission:
            raise Problem("该付款方式尚未开放", 503)
        from .terms import link_agreement
        if not self.provider_ready() or method not in ("wechat", "alipay"):
            raise Problem("该付款方式尚未开放", 503)
        plan = next((p for p in self.plans() if p["id"] == plan_id), None)
        if not plan:
            raise Problem("订阅方案不存在", 404)
        if plan.get("access_until"):
            raise Problem("该付款方式尚未开放", 503)
        if not self.s.db.one("SELECT id FROM products WHERE id='quantgt' AND active=1"):
            raise Problem("该内容暂不可开通", 409)
        existing = self.s.db.one("SELECT * FROM subscriptions WHERE user_id=? AND product_id='quantgt'", (user_id,))
        if existing and existing["status"] == "active" and existing["expires_at"] is None:
            raise Problem("你已拥有长期权益，无需再次开通", 409)
        if existing and existing["billing_id"] and existing["auto_renew"]:
            raise Problem("请先在续费管理中停止自动续费", 409)
        with self.s.db.transaction() as db:
            self.s._limit(db, "purchase:" + user_id, 8, 600)
            # A pending subscription carries account notices but grants no content access.
            if not db.execute("SELECT id FROM subscriptions WHERE user_id=? AND product_id='quantgt'", (user_id,)).fetchone():
                sid = uid()
                db.execute("INSERT INTO subscriptions(id,user_id,product_id,status,expires_at,created_at) VALUES(?,?,'quantgt','pending',0,?)", (sid, user_id, time.time()))
                db.execute("INSERT INTO delivery_preferences VALUES(?,'always')", (sid,))
            if db.execute("SELECT id FROM purchase_orders WHERE user_id=? AND status IN ('verifying','review')", (user_id,)).fetchone():
                raise Problem("付款订单需要核对", 409)
            current = db.execute("SELECT * FROM purchase_orders WHERE provider='xorpay' AND user_id=? AND plan_id=? AND method=? AND amount_minor=? AND days=? AND status IN ('creating','pending') AND expires_at>? ORDER BY created_at DESC LIMIT 1",
                                 (user_id, plan_id, method, plan["amount_minor"], plan["days"], time.time())).fetchone()
            if current:
                order_id = current["id"]
                if current["status"] == "pending":
                    link_agreement(db, user_id, agreement_id, order_id)
                    return self._public_order(dict(current))
            else:
                order_id = uid()
                db.execute("INSERT INTO purchase_orders(id,user_id,product_id,plan_id,days,amount_minor,currency,method,provider,status,created_at,expires_at) VALUES(?,?,'quantgt',?,?,?,?,?,'xorpay','creating',?,?)",
                           (order_id, user_id, plan_id, plan["days"], plan["amount_minor"], "CNY", method, time.time(), time.time()+1800))
            link_agreement(db, user_id, agreement_id, order_id)
        pay_type = "native" if method == "wechat" else "alipay"
        price = f"{plan['amount_minor']/100:.2f}"
        notify = self.cfg.base_url + "/webhooks/xorpay"
        # The fixed merchant name/plan duration must reflect the actual approved service.
        name = "Quant " + str(plan["days"]) + " days"
        response = self.api("POST", "pay/" + self.cfg.xorpay_aid,
                            {"name": name, "pay_type": pay_type, "price": price, "order_id": order_id,
                             "notify_url": notify, "expire": "1800", "sign": self.sign(name, pay_type, price, order_id, notify)})
        if response.get("status") != "ok" or not response.get("aoid"):
            raise Problem("支付服务暂不可用，请稍后重试", 503)
        remote_id = self._remote_id(response["aoid"])
        info = response.get("info")
        if not isinstance(info, dict):
            raise Problem("付款链接验证失败", 502)
        qr = str(info.get("qr", ""))
        parsed = urlsplit(qr)
        allowed = (method == "wechat" and parsed.scheme == "weixin" and parsed.netloc == "wxpay") or (
            method == "alipay" and parsed.scheme == "https" and parsed.hostname in {"qr.alipay.com", "mapi.alipay.com", "openapi.alipay.com"})
        if not allowed or len(qr) > 4096 or parsed.username is not None:
            raise Problem("付款链接验证失败", 502)
        with self.s.db.transaction() as db:
            row = db.execute("SELECT * FROM purchase_orders WHERE id=?", (order_id,)).fetchone()
            self._unique_remote(db, remote_id, order_id)
            if row["remote_id"] and row["remote_id"] != remote_id:
                raise Problem("付款订单需要核对", 409)
            db.execute("UPDATE purchase_orders SET remote_id=?,qr_data=?,status=CASE WHEN status='creating' THEN 'pending' ELSE status END WHERE id=?",
                       (remote_id, qr, order_id))
        return self.order(user_id, order_id)

    def _public_order(self, row):
        if row["provider"] == "xiaodou":
            from .xiaodou import Xiaodou
            return Xiaodou(self.s).public_order(row)
        status = row["status"]
        if status in ("creating", "pending") and row["expires_at"] <= time.time():
            status = "expired"
        can_pay = status == "pending" and self.provider_ready()
        return {"id": row["id"], "status": status, "paid": status == "paid", "method": row["method"],
                "amount_minor": row["amount_minor"], "currency": row["currency"], "expires_at": row["expires_at"],
                "pay_url": row["qr_data"] if row["method"] == "alipay" and can_pay else None,
                "qr_url": "/api/orders/" + row["id"] + "/qr" if row["qr_data"] and can_pay else None}

    def order(self, user_id, order_id):
        row = self.s.db.one("SELECT * FROM purchase_orders WHERE id=? AND user_id=?", (order_id, user_id))
        if not row:
            raise Problem("付款订单不存在", 404)
        return self._public_order(row)

    def qr(self, user_id, order_id):
        value = self.order(user_id, order_id)
        if not value["qr_url"]:
            raise Problem("付款二维码已过期", 410)
        row = self.s.db.one("SELECT qr_data FROM purchase_orders WHERE id=? AND user_id=?", (order_id, user_id))
        return qr_png(row["qr_data"])

    def notify(self, fields):
        # Disabling new checkout must not lose already-authorized in-flight payments.
        if not self.provider_ready(receive_existing=True):
            raise Problem("该付款方式尚未开放", 503)
        required = ("aoid", "order_id", "pay_price", "pay_time", "sign")
        if any(not isinstance(fields.get(k), str) or not fields[k] or len(fields[k]) > 200 for k in required):
            raise Problem("付款通知无效", 400)
        expected = self.sign(*(fields[k] for k in required[:-1]))
        if not hmac.compare_digest(expected, fields["sign"].lower()):
            raise Problem("签名无效", 400)
        paid = amount_minor(fields["pay_price"])
        remote_id = self._remote_id(fields["aoid"])
        with self.s.db.transaction() as db:
            row = db.execute("SELECT * FROM purchase_orders WHERE id=? AND provider='xorpay'", (fields["order_id"],)).fetchone()
            if not row:
                raise Problem("付款订单不存在", 404)
            if paid != row["amount_minor"] or (row["remote_id"] and row["remote_id"] != fields["aoid"]):
                raise Problem("付款金额或订单不匹配", 400)
            self._unique_remote(db, remote_id, row["id"])
            if row["status"] in ("paid", "review", "closed"):
                return {"received": True, "duplicate": True}
            db.execute("INSERT INTO payments VALUES(?,?,?,?,?,?,?)", (uid(), row["user_id"], row["product_id"], "xorpay:" + remote_id, paid, "cny", time.time()))
            # Ignore unsigned more/detail and all user-supplied entitlement fields.
            try:
                if not db.execute("SELECT id FROM products WHERE id=? AND active=1", (row["product_id"],)).fetchone():
                    raise Problem("该内容暂不可开通", 409)
                end = self._grant(db, row["user_id"], row["product_id"], row["days"])
            except Problem:
                # Money may have arrived after a separate lifetime/manual grant.
                # Persist for operator reconciliation; never consume it silently.
                db.execute("UPDATE purchase_orders SET remote_id=?,status='review',paid_at=? WHERE id=?", (fields["aoid"], time.time(), row["id"]))
                audit(db, "billing", "paid_order_needs_review", row["id"])
                self.s.account_notice(db, row["user_id"], row["product_id"], "payment_review", "Quant · 付款待核对", "已确认收到付款，但当前权益发生变化，需要管理员核对。请勿重复付款。", "xorpay-review:" + remote_id)
                return {"received": True, "review": True}
            db.execute("UPDATE purchase_orders SET remote_id=?,status='paid',paid_at=? WHERE id=?", (fields["aoid"], time.time(), row["id"]))
            audit(db, "billing", "purchase_paid", row["id"], {"expires_at": end, "amount_minor": paid})
            self.s.account_notice(db, row["user_id"], row["product_id"], "payment_succeeded", "Quant · 订阅付款成功", "付款已确认，权益已更新。不会自动续费，原有接收偏好保持不变。", "xorpay-notice:" + fields["aoid"])
        return {"received": True}

    def reconcile(self, order_id):
        """Detect missing callbacks; query status alone is not proof of the paid amount."""
        order = self.s.db.one("SELECT provider FROM purchase_orders WHERE id=?", (order_id,))
        if order and order["provider"] == "xiaodou":
            from .xiaodou import Xiaodou
            return Xiaodou(self.s).reconcile(order_id)
        if not self.provider_ready(receive_existing=True):
            return {"skipped": "provider disabled"}
        row = self.s.db.one("SELECT * FROM purchase_orders WHERE id=? AND provider='xorpay'", (order_id,))
        if not row or row["status"] in ("paid", "review", "closed"):
            return {"skipped": "terminal or absent"}
        from urllib.parse import urlencode
        query = urlencode({"order_id": order_id, "sign": self.sign(order_id)})
        result = self.api("GET", "query2/" + self.cfg.xorpay_aid + "?" + query)
        status = result.get("status")
        if status not in ("payed", "success"):
            return {"status": "awaiting_confirmation"}
        with self.s.db.transaction() as db:
            current = db.execute("SELECT status FROM purchase_orders WHERE id=?", (order_id,)).fetchone()
            if current["status"] in ("paid", "review", "closed"):
                return {"skipped": "callback already committed"}
            if current["status"] != "verifying":
                db.execute("UPDATE purchase_orders SET status='verifying' WHERE id=?", (order_id,))
                audit(db, "billing", "payment_callback_missing", order_id)
                self.s.account_notice(db, row["user_id"], row["product_id"], "payment_verifying", "Quant · 付款确认中", "支付平台报告已支付，正在核对完整付款凭证。请勿重复付款；如长时间未开通，请联系管理员。", "xorpay-verifying:" + order_id)
        return {"needs_review": True, "order_id": order_id}

    def resolve_order(self, order_id, *, outcome, reference, actor="cli"):
        """Record an operator's completed external reconciliation, never charge or refund."""
        if outcome not in ("fulfilled", "refunded") or not isinstance(reference, str) or not reference.strip() or len(reference) > 200 or any(c in reference for c in "\r\n\x00"):
            raise ValueError("A completed outcome and provider receipt/reference are required")
        with self.s.db.transaction() as db:
            row = db.execute("SELECT * FROM purchase_orders WHERE id=?", (order_id,)).fetchone()
            if not row or row["status"] not in ("review", "verifying"):
                raise Problem("付款订单不在待核对状态", 409)
            if outcome == "fulfilled":
                sub = db.execute("SELECT * FROM subscriptions WHERE user_id=? AND product_id=?", (row["user_id"], row["product_id"])).fetchone()
                if not sub or sub["status"] != "active" or (sub["expires_at"] is not None and sub["expires_at"] <= time.time()):
                    raise Problem("请先核实并补齐用户权益，再确认处理完成", 409)
            if row["status"] == "verifying":
                db.execute("INSERT INTO payments VALUES(?,?,?,?,?,?,?)", (uid(), row["user_id"], row["product_id"], "manual-reconciliation:" + order_id, row["amount_minor"], "cny", time.time()))
            db.execute("UPDATE purchase_orders SET status='closed',paid_at=COALESCE(paid_at,?) WHERE id=?", (time.time(), order_id))
            audit(db, actor, "order_reconciled_manually", order_id, {"outcome": outcome, "provider_reference": reference.strip(), "previous_status": row["status"], "external_action_performed_by_operator": True})
            db.execute("UPDATE incidents SET state='resolved',updated_at=? WHERE kind='payment_review' AND details=?", (time.time(), "order_id=" + order_id))
            self.s.account_notice(db, row["user_id"], row["product_id"], "payment_resolved", "Quant · 付款核对已完成", "管理员已完成付款核对。处理结果：" + ("权益已确认" if outcome == "fulfilled" else "已在支付平台退款") + "。", "xorpay-resolved:" + order_id)
        return {"id": order_id, "status": "closed", "outcome": outcome, "external_charge_or_refund_executed": False}
