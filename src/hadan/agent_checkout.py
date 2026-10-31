"""Agent-initiated or owner-approved QR checkout, never spending."""
import base64
import ipaddress
import json
import time

from .commerce import Commerce
from .db import audit, uid
from .service import Problem
from .terms import begin_agreement
from .xiaodou import Xiaodou, encoded


class AgentCheckout:
    def __init__(self, service):
        self.s = service

    def authorize(self, user_id, binding_id, payload, payer_ip):
        provider = Xiaodou(self.s)
        if not provider.ready():
            raise Problem("该付款方式尚未开放", 503)
        binding = self.s.db.one("SELECT id FROM bindings WHERE id=? AND user_id=? AND state IN ('pending','active')", (binding_id, user_id))
        if not binding:
            raise Problem("连接不存在", 404)
        plan = next((p for p in Commerce(self.s).plans() if p["id"] == payload.get("plan_id")), None)
        if not plan or not plan.get("access_until"):
            raise Problem("订阅方案不存在", 404)
        if self.s.db.one(
            "SELECT id FROM purchase_orders WHERE user_id=? AND "
            "(status IN ('verifying','review') OR (status IN ('creating','pending') AND expires_at>?))",
            (user_id, time.time()),
        ):
            raise Problem("已有待确认订单，请先完成或核对原订单", 409)
        method = payload.get("method")
        if method not in self.s.settings.xiaodou_methods:
            raise Problem("该付款方式尚未开放", 503)
        try:
            if not ipaddress.ip_address(payer_ip).is_global:
                raise ValueError()
        except (ValueError, TypeError):
            raise Problem("请在订阅网页确认付款", 400) from None
        agreement_id = begin_agreement(self.s, user_id, "purchase", payload)
        now, identity = time.time(), uid()
        with self.s.db.transaction() as db:
            # Superseding an unconsumed permission does not cancel a real payment order.
            db.execute("UPDATE checkout_permissions SET expires_at=? WHERE binding_id=? AND NOT EXISTS(SELECT 1 FROM purchase_orders o WHERE o.id=checkout_permissions.id)", (now, binding_id))
            db.execute("INSERT INTO checkout_permissions VALUES(?,?,?,?,?,?,?,?,?,?)", (identity, binding_id,
                user_id, plan["id"], method, encoded(plan), agreement_id, str(payer_ip), now + 1800, now))
            audit(db, user_id, "agent_checkout_authorized", identity, {"binding_id": binding_id,
                "plan_id": plan["id"], "amount_minor": plan["amount_minor"], "access_until": plan["access_until"], "method": method})
        return {"order_id": identity, "permission_id": identity, "expires_at": now + 1800,
                "amount_minor": plan["amount_minor"], "currency": "CNY",
                "access_until": plan["access_until"], "payment_by": "owner"}

    def start(self, binding, payload, payer_ip):
        """Start a checkout after the owner confirms the live plan in chat.

        This removes the extra website authorization click. The server still
        fixes the plan, amount and method from its own catalog; the agent can
        only choose among published options, and the owner must still pay.
        """
        allowed = {"plan_id", "method", "accept_terms", "terms_version"}
        if not isinstance(payload, dict) or not payload or set(payload) - allowed:
            raise Problem("付款参数无效", 400)
        with self.s.db.transaction() as db:
            self.s._limit(db, "agent-checkout:" + binding["id"], 6, 3600)
        result = self.authorize(binding["user_id"], binding["id"], payload, payer_ip)
        permission = self.s.db.one(
            "SELECT * FROM checkout_permissions WHERE id=? AND binding_id=? AND user_id=?",
            (result["permission_id"], binding["id"], binding["user_id"]),
        )
        if not permission:
            raise Problem("付款授权不存在或已失效", 404)
        return self._prepare_permission(binding, permission)

    def _permission(self, binding):
        # Prefer the permission that already produced an order, so a newer
        # unused permission cannot hide a paid/in-flight order from the agent.
        row = self.s.db.one(
            "SELECT p.* FROM checkout_permissions p "
            "LEFT JOIN purchase_orders o ON o.id=p.id "
            "WHERE p.binding_id=? AND p.user_id=? "
            "ORDER BY (o.id IS NOT NULL) DESC, p.created_at DESC LIMIT 1",
            (binding["id"], binding["user_id"]),
        )
        if not row:
            raise Problem("需要本人确认付款授权", 403)
        return row

    def _public(self, order, user_id, *, include_qr=False):
        base = self.s.settings.base_url.rstrip("/")
        qr_available = bool(order.get("qr_url"))
        value = {key: order[key] for key in ("id", "status", "paid", "method", "amount_minor", "currency", "access_until")} | {
            "qr_available": qr_available,
            "qr_mime_type": "image/png" if qr_available else None,
            "qr_expires_at": order.get("expires_at") if qr_available else None,
            "qr_download_url": (base + "/api/agent/checkout/qr") if qr_available else None,
            # The agent must show the QR, not send the owner into a cloud browser.
            "checkout_url": (base + "/app?order=" + order["id"]) if (not qr_available and order.get("status") == "pending") else None,
            "display_hint": (
                "Show qr_image inline to the owner; do not open a browser or cloud computer. "
                "The owner scans it with WeChat."
            ) if qr_available else None,
            "payment_by": "owner",
            "automatic_renewal": False,
        }
        if include_qr and qr_available:
            raw = Commerce(self.s).qr(user_id, order["id"])
            value["qr_image"] = "data:image/png;base64," + base64.b64encode(raw).decode("ascii")
        return value

    def prepare_for_owner(self, user_id, permission_id):
        """Create the approved order from the owner's authenticated web session.

        The Agent permission exists before an order exists; treating its
        permission id as an order id made the web UI poll a non-existent order.
        This route lets the same UI that granted permission obtain the QR
        immediately, while the Agent API remains available for delegated use.
        """
        permission = self.s.db.one(
            "SELECT p.* FROM checkout_permissions p JOIN bindings b ON b.id=p.binding_id "
            "WHERE p.id=? AND p.user_id=? AND b.user_id=p.user_id AND b.state IN ('pending','active')",
            (permission_id, user_id),
        )
        if not permission:
            raise Problem("付款授权不存在或已失效", 404)
        existing = self.s.db.one(
            "SELECT id,status FROM purchase_orders WHERE id=? AND user_id=?",
            (permission_id, user_id),
        )
        if existing and existing["status"] != "creating":
            return Commerce(self.s).order(user_id, existing["id"])
        if permission["expires_at"] <= time.time():
            raise Problem("付款授权已过期，请重新确认", 403)
        Commerce(self.s).create_order(
            user_id,
            permission["plan_id"],
            permission["method"],
            agreement_id=permission["agreement_id"],
            payer_ip=permission["payer_ip"],
            checkout_permission=permission["id"],
        )
        return Commerce(self.s).order(user_id, permission_id)

    def _prepare_permission(self, binding, permission):
        existing = self.s.db.one("SELECT id FROM purchase_orders WHERE id=? AND user_id=?", (permission["id"], binding["user_id"]))
        if existing and self.s.db.one("SELECT status FROM purchase_orders WHERE id=?", (existing["id"],))["status"] != "creating":
            return self._public(
                Commerce(self.s).order(binding["user_id"], existing["id"]),
                binding["user_id"],
                include_qr=True,
            )
        if not existing and permission["expires_at"] <= time.time():
            raise Problem("付款授权已过期，请重新确认", 403)
        order = Commerce(self.s).create_order(binding["user_id"], permission["plan_id"], permission["method"],
            agreement_id=permission["agreement_id"], payer_ip=permission["payer_ip"], checkout_permission=permission["id"])
        return self._public(order, binding["user_id"], include_qr=True)

    def prepare(self, binding):
        return self._prepare_permission(binding, self._permission(binding))

    def qr(self, binding):
        permission = self._permission(binding)
        order = self.s.db.one(
            "SELECT id FROM purchase_orders WHERE id=? AND user_id=?",
            (permission["id"], binding["user_id"]),
        )
        if not order:
            raise Problem("付款二维码尚未生成", 409)
        return Commerce(self.s).qr(binding["user_id"], permission["id"])

    def status(self, binding):
        permission = self._permission(binding)
        order = self.s.db.one(
            "SELECT id FROM purchase_orders WHERE id=? AND user_id=?",
            (permission["id"], binding["user_id"]),
        )
        if not order:
            if permission["expires_at"] <= time.time():
                raise Problem("付款授权已过期，请重新确认", 403)
            return {
                "id": permission["id"],
                "permission_id": permission["id"],
                "status": "authorized",
                "paid": False,
                "method": permission["method"],
                "amount_minor": json.loads(permission["plan_snapshot"])["amount_minor"],
                "currency": "CNY",
                "access_until": json.loads(permission["plan_snapshot"])["access_until"],
                "qr_available": False,
                "qr_mime_type": None,
                "qr_expires_at": None,
                "qr_download_url": None,
                "checkout_url": None,
                "display_hint": None,
                "next_action": "Call prepare_checkout with the confirmed live plan to create the QR order.",
                "payment_by": "owner",
                "automatic_renewal": False,
            }
        return self._public(Commerce(self.s).order(binding["user_id"], permission["id"]), binding["user_id"])
