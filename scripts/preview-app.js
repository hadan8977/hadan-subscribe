"use strict";
// Loaded only by scripts/preview.py, before the production app.js.
// All state is disposable and local to this document. No network fallback exists.
(() => {
  if (document.body?.dataset.preview !== "true") return;

  const EMAIL = "reader@example.test";
  const CODE = "123456";
  const PAIR = "DEADBEEF";
  const SUBSCRIPTION = "PREVIEW_SUBSCRIPTION";
  const BINDING = "PREVIEW_BINDING";
  const CONTENT = "00000000000000000000000000000001";
  const now = () => Math.floor(Date.now() / 1000);
  const text = (zh, en) => document.documentElement.lang === "en" ? en : zh;
  const reply = (data, status = 200) => new Response(JSON.stringify({...data, preview:true}), {
    status, headers:{"Content-Type":"application/json", "Cache-Control":"no-store"},
  });
  const problem = (zh, en, status = 400) => reply({error:text(zh, en)}, status);
  const unavailable = () => problem("此操作未包含在本地演示中。", "This action is not available in the local demo.", 503);

  let signedIn = false, requestedCode = false, subscription = null, binding = null;
  let pairApproved = false, pairExpires = now() + 900, activateAt = 0;
  const createdAt = now();

  function finishSimulatedTest() {
    if (binding?.state === "pending" && activateAt && Date.now() >= activateAt) {
      binding = {...binding, state:"active", test_ok:1, last_seen:now()};
      activateAt = 0;
    }
  }

  function grantAccess() {
    // Renewing demo access never resets a previous delivery choice.
    subscription = {
      id:SUBSCRIPTION, user_id:"PREVIEW_USER", product_id:"quantgt", name:"Quant", paid:1,
      status:"active", created_at:createdAt, email_enabled:true, agent_enabled:true,
      email_format:"summary", email_policy:"fallback", auto_renew:0, billing_id:null,
      ...subscription, expires_at:now() + 30 * 86400,
    };
    subscription.status = "active";
    return subscription;
  }

  function content() {
    return {
      id:"PREVIEW_VERSION", content_id:CONTENT, product_id:"quantgt", number:1,
      title:"Portfolio rebalanced — sample report", created_at:createdAt, changes:"",
      body:"Portfolio: SMPB added; SMPD removed; SMPA score updated.\nWeekly Watchlist: SMPC.\nThis is a local preview of a report and two attachments.",
      attachments:[
        {id:"PREVIEW_FILE_XLSX", name:"quant-sample.xlsx", mime:"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", preview_url:"/static/quant-sample.xlsx"},
        {id:"PREVIEW_FILE_PNG", name:"quant-sample.png", mime:"image/png", preview_url:"/static/quant-sample.png"},
      ],
    };
  }

  const routes = new Map([
    ["POST /api/auth/request", (body) => {
      if (String(body.email || "").trim().toLowerCase() !== EMAIL) return problem(
        "演示请使用 reader@example.test。", "Use reader@example.test for this demo.");
      requestedCode = true;
      return reply({ok:true, message:text("演示验证码为 123456，没有发送邮件。", "The demo code is 123456. No email was sent.")});
    }],
    ["POST /api/auth/verify", (body) => {
      if (!requestedCode || String(body.email || "").trim().toLowerCase() !== EMAIL || String(body.code || "") !== CODE) {
        return problem("请先获取演示验证码，再输入 123456。", "Request the demo code first, then enter 123456.");
      }
      signedIn = true;
      requestedCode = false;
      return reply({ok:true});
    }],
    ["POST /api/auth/logout", () => {
      signedIn = false;
      return reply({ok:true});
    }],
    ["GET /api/me", () => {
      finishSimulatedTest();
      return reply({
        user:{id:"PREVIEW_USER", email:EMAIL, is_admin:false}, csrf:"PREVIEW_INVALID_CSRF",
        products:[{id:"quantgt", name:"Quant", description:"", paid:1}],
        subscriptions:subscription ? [subscription] : [], bindings:binding ? [binding] : [],
      });
    }],
    ["GET /api/purchase-options", () => reply({
      methods:[{id:"code", enabled:true}], plans:[], terms_version:"PREVIEW_NO_PURCHASE",
    })],
    ["POST /api/redeem", (body) => {
      if (String(body.code || "").trim().toUpperCase() !== "QUANT-DEMO") return problem(
        "演示兑换码为 QUANT-DEMO。", "The demo access code is QUANT-DEMO.");
      const access = grantAccess();
      return reply({expires_at:access.expires_at, message:text("已模拟开通 30 天，未创建真实订阅。", "30 days of access simulated. No real subscription was created.")});
    }],
    ["POST /api/subscriptions", (body) => body.product_id === "quantgt" ? reply(grantAccess()) : unavailable()],
    ["PATCH /api/subscriptions/" + SUBSCRIPTION, (body) => {
      if (!subscription) return unavailable();
      const keys = Object.keys(body), allowed = ["email_enabled", "agent_enabled", "email_format", "email_policy"];
      if (!keys.length || keys.some(key => !allowed.includes(key))) return unavailable();
      if (["email_enabled", "agent_enabled"].some(key => key in body && typeof body[key] !== "boolean")) return unavailable();
      if ("email_format" in body && !["summary", "full"].includes(body.email_format)) return unavailable();
      if ("email_policy" in body && !["fallback", "always"].includes(body.email_policy)) return unavailable();
      subscription = {...subscription, ...body};
      return reply({ok:true});
    }],
    ["POST /api/subscriptions/" + SUBSCRIPTION + "/cancel", () => {
      if (!subscription) return unavailable();
      subscription = {...subscription, email_enabled:false, agent_enabled:false, auto_renew:0, cancel_requested:1};
      return reply({ok:true, expires_at:subscription.expires_at});
    }],
    ["GET /api/pair/details", (_, url) => {
      if (url.searchParams.get("code")?.toUpperCase() !== PAIR || pairApproved || pairExpires <= now()) return problem(
        "请使用演示配对码 DEADBEEF。", "Use the demo pairing code DEADBEEF.", 409);
      return reply({name:"Quant Demo Agent", expires_at:pairExpires});
    }],
    ["POST /api/pair/approve", (body) => {
      if (!subscription || subscription.status !== "active" || subscription.expires_at <= now()) return problem(
        "请先兑换演示订阅。", "Activate demo access first.", 403);
      if (String(body.code || "").trim().toUpperCase() !== PAIR || pairApproved || pairExpires <= now()) return problem(
        "请使用演示配对码 DEADBEEF。", "Use the demo pairing code DEADBEEF.", 409);
      pairApproved = true;
      binding = {id:BINDING, name:"Quant Demo Agent", mode:"poll", state:"pending", test_ok:0, created_at:now(), last_seen:null};
      activateAt = Date.now() + 900;
      // This only advances local demo state; it sends no requests and runs no agent.
      setTimeout(finishSimulatedTest, 920);
      return reply({binding_id:BINDING, state:"pending"});
    }],
    ["DELETE /api/bindings/" + BINDING, () => {
      if (!binding) return unavailable();
      binding = {...binding, state:"revoked"};
      activateAt = 0;
      return reply({ok:true});
    }],
    ["GET /api/events", (_, url) => {
      finishSimulatedTest();
      const visible = subscription?.status === "active" && subscription.expires_at > now();
      const after = Number(url.searchParams.get("after") || 0);
      const before = url.searchParams.has("before") ? Number(url.searchParams.get("before")) : Infinity;
      const events = visible && after < 1 && before > 1 ? [{
        id:"PREVIEW_EVENT", seq:1, product_id:"quantgt", content_id:CONTENT, version:1,
        kind:"published", summary:content().title, created_at:createdAt, withdrawn:false,
        processing_state:binding?.state === "active" && subscription.agent_enabled ? "processed" : "pending",
      }] : [];
      return reply({events, next_cursor:events.length ? 1 : after, next_before:events.length ? 1 : null});
    }],
    ["GET /api/library", (_, url) => {
      const visible = subscription?.status === "active" && subscription.expires_at > now();
      const before = url.searchParams.has("before") ? Number(url.searchParams.get("before")) : Infinity;
      const contents = visible && before > createdAt ? [{id:CONTENT, product_id:"quantgt", title:content().title, version:1, created_at:createdAt}] : [];
      return reply({contents, next_before:contents.length ? createdAt : null, next_before_id:contents.length ? CONTENT : null});
    }],
    ["GET /api/content/" + CONTENT, (_, url) => {
      if (!subscription || subscription.status !== "active" || subscription.expires_at <= now()) return unavailable();
      if (url.searchParams.has("version") && url.searchParams.get("version") !== "1") return unavailable();
      return reply(content());
    }],
  ]);
  const publicRoutes = new Set(["POST /api/auth/request", "POST /api/auth/verify", "GET /api/purchase-options"]);

  window.fetch = async (input, init = {}) => {
    // Never keep or call the original fetch, including for malformed inputs.
    try {
      const request = input instanceof Request ? input : null;
      const url = new URL(request ? request.url : String(input), location.href);
      const method = String(init.method || request?.method || "GET").toUpperCase();
      const key = method + " " + url.pathname;
      if (url.origin !== location.origin || url.username || url.password || !routes.has(key)) return unavailable();
      if ((init.signal || request?.signal)?.aborted) throw new DOMException("Aborted", "AbortError");
      if (!publicRoutes.has(key) && !signedIn) return problem("请先完成演示登录。", "Complete the demo sign-in first.", 401);
      let body = {};
      if (!["GET", "HEAD"].includes(method)) {
        const raw = init.body !== undefined ? init.body : request ? await request.clone().text() : "{}";
        if (typeof raw !== "string" || raw.length > 4096) return unavailable();
        try { body = raw ? JSON.parse(raw) : {}; } catch (_) { return unavailable(); }
        if (!body || Array.isArray(body) || typeof body !== "object") return unavailable();
      }
      return routes.get(key)(body, url);
    } catch (error) {
      if (error.name === "AbortError") throw error;
      return unavailable();
    }
  };

  // Inputs are conveniences, not credentials or automatically submitted actions.
  for (const [id, value] of [["email", EMAIL], ["redeem-code", "QUANT-DEMO"], ["pair-code", PAIR]]) {
    const input = document.getElementById(id);
    if (input && !input.value) input.value = value;
  }
})();
