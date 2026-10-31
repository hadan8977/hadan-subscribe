"use strict";
(() => {
  const $ = (selector) => document.querySelector(selector);
  const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const bundle = JSON.parse($("#quant-i18n")?.textContent || '{"strings":{},"errors":{}}');
  const QUANT = "quantgt";
  let lang = document.documentElement.lang === "en" ? "en" : "zh-CN";
  let csrf = "", account = null, adminData = null, toastTimer;
  const entry = new URLSearchParams(location.search);
  const pairHome = $("#pair-form")?.parentElement;
  let onboarding = entry.get("flow") === "subscribe" || entry.has("pair");
  let currentTab = "updates", codeRequested = false, codeCooldownTimer = null;
  let onboardingAgent = entry.has("pair"), connectionTimer = null, connectionPolls = 0;
  let displayedEvents = [], displayedArchives = [], eventBefore = null, archiveBefore = null, archiveBeforeId = null;
  let moreEvents = false, moreArchives = false, feedLoading = true, feedError = false;
  let feedGeneration = 0;
  let pairDetails = null, pairChecking = false, pairApproved = false, pairCheckedCode = "", pairBindingId = null, pairAutoTried = false;
  let purchaseData = null, purchaseError = false, currentOrder = null, orderTimer = null, orderPolls = 0, agreedTermsVersion = null, agreedQuoteId = null;
  let orderPause = null, orderFailures = 0;
  const t = (key, vars = {}) => (bundle.strings[lang]?.[key] || key).replace(/\{(\w+)\}/g, (all, name) => vars[name] ?? all);
  function localMessage(value) {
    const text = String(value || "");
    if (lang === "en") return bundle.errors[text] || (/[\u3400-\u9fff]/.test(text) ? t("generic_error") : text);
    const original = Object.keys(bundle.errors).find((key) => bundle.errors[key] === text);
    return original || text;
  }
  function toast(message, error = false) {
    const element = $("#toast"); if (!element) return;
    element.textContent = message; element.className = "visible"; element.setAttribute("role", error ? "alert" : "status");
    clearTimeout(toastTimer); toastTimer = setTimeout(() => { element.className = ""; }, 5200);
  }
  function status(element, text, error = false) { if (element) { element.textContent = text; element.classList.toggle("error", error); element.classList.toggle("success", !error && !!text); } }
  async function api(path, method = "GET", body) {
    const controller = new AbortController(); const deadline = setTimeout(() => controller.abort(), 20000);
    try {
      const response = await fetch(path, {method, signal:controller.signal, credentials:"same-origin", headers:{"Content-Type":"application/json", "X-CSRF-Token":csrf, "Accept-Language":lang}, body:body === undefined ? undefined : JSON.stringify(body)});
      let result; try { result = await response.json(); } catch (_) { throw new Error(t("generic_error")); }
      if (!response.ok) { const error = new Error(localMessage(result.error || t("generic_error"))); error.status = response.status; error.data = result?.data || null; throw error; }
      return result;
    } catch (error) { if (error.name === "AbortError" || error instanceof TypeError) throw new Error(t("network_error")); throw error; }
    finally { clearTimeout(deadline); }
  }
  function bind(selector, event, action) {
    const element = $(selector); if (!element) return;
    element.addEventListener(event, async (e) => {
      if (event === "submit") e.preventDefault();
      const button = event === "submit" ? element.querySelector('button[type="submit"],button:not([type])') : element.tagName === "BUTTON" ? element : null;
      if (button) button.disabled = true;
      try { await action(e); } catch (error) {
        if ($("#login") && !$("#login").hidden) { $("#login-status").dataset.statusKey=''; status($("#login-status"), error.message, true); }
        else toast(error.message, true);
      }
      finally { if (button?.isConnected) button.disabled = button.dataset.locked === "true"; }
    });
  }
  function date(value, withTime = false) {
    if (!value) return t("access_lifetime");
    const d = typeof value === "number" ? new Date(value * 1000) : new Date(value);
    if (!Number.isFinite(d.getTime())) return "—";
    return new Intl.DateTimeFormat(lang, {dateStyle:"medium", ...(withTime ? {timeStyle:"short"} : {})}).format(d);
  }
  function money(minor, currency) {
    if (!Number.isFinite(Number(minor)) || !/^[A-Z]{3}$/i.test(currency || "")) return "";
    try { return new Intl.NumberFormat(lang, {style:"currency", currency:currency.toUpperCase()}).format(Number(minor) / 100); } catch (_) { return ""; }
  }
  function safeURL(raw, {image = false} = {}) {
    if (!raw || typeof raw !== "string") return null;
    if (image && /^data:image\/(png|jpeg|webp);base64,[a-z0-9+/=]+$/i.test(raw)) return raw;
    try { const url = new URL(raw, location.origin); if (url.username || url.password) return null; if (url.protocol !== "https:" && !(url.protocol === "http:" && url.origin === location.origin)) return null; return url.href; } catch (_) { return null; }
  }
  let landingData = null, landingError = false;
  const subscription = () => account?.subscriptions.find((s) => s.product_id === QUANT);
  const activeAccess = (s) => !!(s && s.status === "active" && (!s.expires_at || Number(s.expires_at) > Date.now() / 1000));
  const activeAgent = () => account?.bindings.find((b) => b.state === "active");
  function renderLandingPrice() {
    const targets=document.querySelectorAll("[data-live-price]"); if(!targets.length)return;
    let text=t(landingError?"purchase_price_unavailable":"loading");
    if(!landingError&&landingData){
      const plans=(landingData.plans||[]).filter(p=>p.id&&Number.isFinite(Number(p.amount_minor))&&money(p.amount_minor,p.currency)&&(Number(p.days)>0||Number(p.access_until)>Date.now()/1000));
      if(!plans.length)text=t("purchase_price_unavailable");
      else{
        const plan=plans.reduce((a,b)=>Number(a.amount_minor)<=Number(b.amount_minor)?a:b);
        const period=plan.access_until?t("plan_until",{date:date(plan.access_until)}):t("plan_days",{days:plan.days});
        text=t(plans.length>1?"landing_price_from":"landing_price",{price:money(plan.amount_minor,plan.currency),period});
      }
    }
    targets.forEach(target=>{target.textContent=text;});
  }
  async function loadLandingPrice() {
    if(!document.querySelector("[data-live-price]"))return;
    if(landingData||landingError){renderLandingPrice();return;}
    try{landingData=await api("/api/purchase-options");landingError=false;}
    catch(_){landingError=true;}
    renderLandingPrice();
  }
  const selectedAgent = () => pairBindingId ? account?.bindings.find(b=>b.id===pairBindingId && b.state==="active") : (entry.has("pair") && !pairApproved ? null : activeAgent());
  const stateName = (state) => bundle.strings[lang]?.["state_" + state] || state;
  function tab(name, updateURL = true) {
    if (!["updates", "subscriptions", "connections"].includes(name)) name = "updates";
    currentTab = name;
    if ($("#account-heading")) $("#account-heading").textContent = t({updates:"reports",connections:"delivery",subscriptions:"subscription"}[name]);
    document.querySelectorAll("[data-tab]").forEach((b) => { const active = b.dataset.tab === name; b.classList.toggle("active", active); b.setAttribute("aria-selected", String(active)); });
    document.querySelectorAll(".tab-panel").forEach((p) => { p.hidden = p.id !== "view-" + name; });
    if (updateURL) { const url = new URL(location.href); url.searchParams.set("view", name); history.replaceState(null, "", url.pathname + url.search + url.hash); }
  }
  function applyLanguage(next, writeURL = true) {
    lang = next === "en" ? "en" : "zh-CN"; document.documentElement.lang = lang;
    document.cookie = "quant_lang=" + lang + "; Path=/; SameSite=Lax" + (location.protocol === "https:" ? "; Secure" : "");
    if (writeURL) { const url = new URL(location.href); url.searchParams.set("lang", lang); history.replaceState(null, "", url.pathname + url.search + url.hash); }
    document.querySelectorAll("[data-i18n]").forEach((el) => {
      if (!el.dataset.i18n) return;
      let text = t(el.dataset.i18n);
      if (el.dataset.i18nName) text = text.replace(/\{name\}/g, el.dataset.i18nName);
      el.textContent = text;
    });
    document.querySelectorAll("[data-mail-sender-notice]").forEach((el) => { el.textContent = t("mail_sender_notice", {email: el.dataset.mailSender || ""}); });
    for (const attribute of ["placeholder", "aria-label", "content", "alt"]) document.querySelectorAll("[data-i18n-" + attribute + "]").forEach((el) => el.setAttribute(attribute, t(el.getAttribute("data-i18n-" + attribute))));
    document.querySelectorAll("[data-language]").forEach((a) => { a.toggleAttribute("aria-current", a.dataset.language === lang); if (a.dataset.language === lang) a.setAttribute("aria-current", "true"); const url = new URL(location.href); url.searchParams.set("lang", a.dataset.language); a.href = url.pathname + url.search + url.hash; });
    document.querySelectorAll("[data-message-original]").forEach((el) => { el.textContent = localMessage(el.dataset.messageOriginal) || t("saved_heading"); });
    document.querySelectorAll("[data-timestamp]").forEach((el) => { el.textContent=date(Number(el.dataset.timestamp),true); });
    if (codeRequested && $("#request-code")) $("#request-code").textContent = t("resend_code");
    for (const selector of ["#login-status", "#redeem-status"]) { const el = $(selector); if (el?.dataset.statusKey) status(el, t(el.dataset.statusKey), el.classList.contains("error")); else if (el?.textContent) status(el, localMessage(el.textContent), el.classList.contains("error")); }
    if (account && $("#account")) renderAccount();
    if (adminData) renderAdmin();
    renderPurchase(); renderOrder(); renderOnboarding(); renderLandingPrice();
    if ($("#agent-prompt-text")) $("#agent-prompt-text").value=t("agent_prompt", {url:location.origin});
    if ($("#guide-url")) $("#guide-url").value=t("agent_prompt", {url:location.origin});
  }
  document.querySelectorAll("[data-language]").forEach((a) => a.addEventListener("click", (event) => { event.preventDefault(); applyLanguage(a.dataset.language); }));
  document.querySelectorAll("[data-tab]").forEach((b) => b.addEventListener("click", () => tab(b.dataset.tab)));

  function localized(element, key) {
    if (!element) return;
    element.dataset.i18n = key; element.textContent = t(key);
  }
  function nextReport() {
    const next = new URLSearchParams(location.search).get("next");
    return next && /^\/read\/[a-f0-9]{32}(?:\?version=[1-9]\d*)?$/.test(next) ? next : null;
  }
  function returnToReport() {
    const next = nextReport();
    if (entry.has("pair") || !next || !activeAccess(subscription())) return false;
    const target = new URL(next, location.origin); target.searchParams.set("lang", lang);
    location.assign(target.pathname + target.search); return true;
  }
  function renderOnboarding() {
    const progress = $("#onboarding-progress"); if (!progress) return;
    progress.hidden = !onboarding;
    localized($("#login h1"), onboarding ? "onboard_login_title" : "login_title");
    const ready = !!(account && activeAccess(subscription()));
    progress.querySelectorAll("[data-step]").forEach(el => {
      el.removeAttribute("aria-current");
      if (el.dataset.step === (!account ? "email" : ready ? "done" : "access")) el.setAttribute("aria-current", "step");
    });
    $("#onboarding-ready").hidden = !(onboarding && ready && !onboardingAgent);
    const connecting = onboarding && ready && onboardingAgent;
    $("#onboarding-agent-heading").hidden = !connecting;
    $("#onboarding-connect-actions").hidden = !connecting;
    $("#workspace").dataset.connectStep = String(connecting);
    $("#onboarding-access").hidden = !(onboarding && account && !ready);
    if (!account) return;
    $("#account").hidden = onboarding && ready && !onboardingAgent;
    $("#account .workspace-heading").hidden = onboarding;
    $("#account .tabs").hidden = onboarding;
    if (onboarding && !ready) tab("subscriptions", false);
    const pairBeforePayment = !!(entry.has("pair") && !ready && !pairApproved);
    if ($("#pair-prepayment")) {
      $("#pair-prepayment").hidden = !pairBeforePayment;
      const destination = pairBeforePayment ? $("#pair-prepayment") : pairHome;
      if (destination && $("#pair-form").parentElement !== destination) destination.append($("#pair-form"));
    }
    if (connecting) {
      tab("connections", false);
      localized($("#onboard-connect-later"), subscription().email_enabled ? "onboard_connect_later" : "onboard_connect_later_off");
      const connected = !!selectedAgent();
      const revoked = account.bindings.some(b=>b.id===pairBindingId && b.state==="revoked");
      $("#workspace").dataset.connectionReady = String(connected);
      $("#onboard-connect-status").hidden = !connected && !pairApproved;
      $("#onboard-connect-finish").disabled = !connected;
      $("#onboard-connect-later").hidden = connected;
      $("#onboard-connect-refresh").hidden = connected;
      localized($("#onboard-connect-status"), connected ? (subscription().agent_enabled ? (entry.has("pair") ? "onboard_handoff_ready" : "onboard_connect_ready") : "onboard_connect_off") : revoked ? "state_revoked" : pairApproved ? (connectionPolls >= 20 ? "onboard_connect_timeout" : "onboard_connect_wait") : "onboard_connect_copy");
      if (connected) { clearTimeout(connectionTimer); connectionTimer=null; }
    }
    if (onboarding && ready) {
      $("#welcome-email").textContent = account.user.email;
      localized($("#welcome-delivery"), subscription().email_enabled ? "onboard_mail_ready" : "onboard_mail_off");
      $("#onboard-enable-email").hidden = !!subscription().email_enabled;
      localized($("#welcome-agent-copy"), activeAgent() ? (subscription().agent_enabled ? "onboard_agent_connected" : "onboard_agent_paused") : "onboard_agent_copy");
      localized($("#onboard-agent"), activeAgent() ? "onboard_agent_manage" : "onboard_agent_action");
    }
  }
  function finishOnboarding(destination) {
    if (destination === "updates" && returnToReport()) return;
    onboarding = false; onboardingAgent = false; clearTimeout(connectionTimer); connectionTimer=null;
    const url = new URL(location.href); url.searchParams.delete("flow");
    history.replaceState(null, "", url.pathname + url.search + url.hash);
    currentTab = destination; renderAccount(); tab(destination);
    $("#account-heading").setAttribute("tabindex", "-1"); $("#account-heading").focus({preventScroll:true});
  }
  async function finishSignIn() {
    await loadAccount({waitForContent:false});
    if (returnToReport()) return;
    if (!entry.has("pair") && nextReport() && !activeAccess(subscription())) onboarding = true;
    // Only an explicit subscription entry can create first-time free access.
    // Authentication, renewals and rendering never overwrite existing preferences.
    if (!entry.has("pair") && entry.get("flow") === "subscribe" && onboarding && !subscription() && account.products.find(p => p.id === QUANT)?.paid === 0) {
      await api("/api/subscriptions", "POST", {product_id:QUANT});
      await loadAccount({waitForContent:false});
    }
    if (!activeAccess(subscription()) && !entry.has("pair")) currentTab = "subscriptions";
    renderAccount();
    if (onboarding && activeAccess(subscription())) $("#welcome-heading").focus({preventScroll:true});
  }

  function renderAccount() {
    $("#login").hidden = true; $("#account").hidden = false;
    $("#account-email").textContent = account.user.email; $("#admin-link").hidden = !account.user.is_admin;
    const sub = subscription(), active = activeAccess(sub), product = account.products.find((p) => p.id === QUANT);
    const label = active ? "access_active" : sub?.status === "pending" ? "access_pending" : sub?.status === "ended" ? "access_ended" : sub ? "access_expired" : "access_none";
    const expiry = sub?.status === "pending" ? t("onboard_pending") : sub?.expires_at ? t("access_until", {date:date(sub.expires_at)}) : active ? t("access_lifetime") : t("access_explainer");
    $("#access-status").innerHTML = '<div><span class="badge">' + esc(t(label)) + '</span><h2>Quant</h2><p>' + esc(expiry) + '</p></div>' + (!active && product?.paid === 0 ? '<button type="button" class="button" data-subscribe="'+QUANT+'">' + esc(t("free_start")) + '</button>' : '');
    $("#subscriptions-list").innerHTML = sub ? '<div class="subscription"><div class="delivery-actions"><button type="button" class="delivery-toggle" data-delivery-pref="email_enabled" data-sid="'+esc(sub.id)+'" aria-pressed="'+(sub.email_enabled?'true':'false')+'">'+esc(t("email_delivery"))+'</button><button type="button" class="delivery-toggle" data-delivery-pref="agent_enabled" data-sid="'+esc(sub.id)+'" aria-pressed="'+(sub.agent_enabled?'true':'false')+'">'+esc(t("agent_delivery"))+'</button></div></div>' : '<p class="empty">'+esc(t("need_access_delivery"))+'</p>';
    const connections = account.bindings.filter((b) => b.state !== "revoked");
    $("#bindings-list").innerHTML = connections.length ? connections.map((b) => '<article class="entry"><div><h3>'+esc(b.name)+' <span class="badge">'+esc(stateName(b.state))+'</span></h3><p>'+esc(t(b.mode === "poll" ? "poll_mode" : "webhook_mode"))+' · '+esc(b.last_seen?t("last_connection",{date:date(b.last_seen,true)}):t("first_connection"))+'</p></div><button class="quiet danger" data-revoke="'+esc(b.id)+'">'+esc(t("revoke"))+'</button></article>').join("") : '<div class="empty"><strong>'+esc(t("agent_not_connected"))+'</strong>'+esc(t("agent_optional"))+'</div>';
    if ($(".redeem-panel")) $(".redeem-panel").hidden = active && !!sub && !sub.expires_at;
    // Keep the purchase panel available for finite plans that can extend
    // access. The form itself is gated by the live plan eligibility below;
    // the order status card lives outside this panel so it survives payment.
    if ($("#purchase-section")) $("#purchase-section").hidden = false;
    if (onboarding && !active) $("#purchase-section").open = true;
    if (entry.has("pair") && !pairApproved) {
      currentTab = active ? "connections" : "subscriptions";
      if (!active) $("#purchase-section").open = true;
    }
    $("#pair-access-note").hidden = !entry.has("pair") || active || pairApproved;
    renderPair();
    renderUpdates(); tab(currentTab, false); renderOnboarding();
  }
  async function loadAccount({feed = true, waitForContent = true} = {}) {
    account = await api("/api/me"); csrf = account.csrf;
    if (!$("#account")) return;
    const params = new URLSearchParams(location.search);
    const orderId = params.get("order");
    if (orderId && !params.has("view")) currentTab = "subscriptions";
    renderAccount();
    const tasks = [loadPurchaseOptions()]; if (feed) tasks.push(loadReports());
    if (!currentOrder && /^[A-Za-z0-9_-]{6,64}$/.test(orderId||"")) tasks.push(api('/api/orders/'+orderId).then(order=>{
      setCurrentOrder(order);
      orderPolls=0;
      checkOrder(true);
    }).catch(error=>{
      if(error.status===404){
        const clean=new URL(location.href);clean.searchParams.delete("order");history.replaceState(null,"",clean.pathname+clean.search+clean.hash);
      }
    }));
    const code = $("#pair-code")?.value.trim().toUpperCase();
    if (code && code !== pairCheckedCode && !pairChecking) tasks.push(inspectPair());
    watchConnection();
    if (waitForContent) {
      await Promise.allSettled(tasks);
      await autoApprovePair();
    } else void Promise.allSettled(tasks);
  }
  function watchConnection() {
    if ((!onboardingAgent && currentTab !== "connections") || !pairApproved || selectedAgent() || account?.bindings.some(b=>b.id===pairBindingId&&b.state==="revoked") || connectionPolls >= 20 || connectionTimer) return;
    connectionTimer = setTimeout(async () => {
      connectionTimer=null; connectionPolls++;
      try { await loadAccount({feed:false}); } catch (_) { if (connectionPolls >= 20) renderOnboarding(); else watchConnection(); }
    }, 3000);
  }
  function renderPair() {
    const code = $("#pair-code")?.value.trim().toUpperCase();
    const valid = !!(pairDetails && pairDetails.code === code && pairDetails.expires_at * 1000 > Date.now());
    if (!$("#pair-review")) return;
    $("#pair-review").hidden = !valid;
    $("#pair-agent-name").textContent = valid ? pairDetails.name : "";
    $("#approve-pair").disabled = !valid || pairApproved;
    $("#approve-pair").dataset.locked = String($("#approve-pair").disabled);
  }
  async function autoApprovePair() {
    if (entry.get("manual") === "1" || pairAutoTried || pairApproved || activeAgent()) return;
    const code = $("#pair-code")?.value.trim().toUpperCase();
    if (!pairDetails || pairDetails.code !== code || pairDetails.expires_at * 1000 <= Date.now()) return;
    pairAutoTried = true;
    try {
      const approved = await api('/api/pair/approve', 'POST', {code});
      pairBindingId = approved.binding_id; pairApproved = true; pairDetails = null; connectionPolls = 0;
      $("#pair-code").value = '';
      const clean = new URL(location.href); clean.searchParams.delete("pair"); clean.searchParams.delete("manual");
      history.replaceState(null, "", clean.pathname + clean.search + clean.hash);
      await loadAccount({feed:false}); toast(t("pair_saved"));
    } catch (error) {
      pairAutoTried = false;
      status($("#pair-error"), error.message, true);
    }
  }
  async function inspectPair() {
    const code = $("#pair-code").value.trim().toUpperCase();
    pairDetails = null; renderPair();
    if (!/^[A-F0-9]{8}$/.test(code)) { status($("#pair-error"), t("pair_link_invalid"), true); return; }
    pairChecking = true; pairCheckedCode = code;
    try {
      const result = await api('/api/pair/details?code=' + encodeURIComponent(code));
      if ($("#pair-code").value.trim().toUpperCase() === code) { pairDetails = {...result, code}; status($("#pair-error"), ""); }
    } catch (error) { status($("#pair-error"), error.message, true); }
    finally { pairChecking = false; renderPair(); }
  }
  async function loadReports() {
    const generation = ++feedGeneration;
    feedLoading = true; feedError = false; renderUpdates();
    const results = await Promise.allSettled([api("/api/events?limit=100&newest=true"), api("/api/library")]);
    // A pre-activation response must not replace a newer, authorized feed.
    if (generation !== feedGeneration) return;
    if (results[0].status === "fulfilled") { const data=results[0].value; displayedEvents=data.events.filter((e)=>e.product_id===QUANT); eventBefore=data.next_before; moreEvents=data.events.length===100; } else feedError=true;
    if (results[1].status === "fulfilled") { const data=results[1].value; displayedArchives=data.contents.filter((e)=>e.product_id===QUANT); archiveBefore=data.next_before; archiveBeforeId=data.next_before_id; moreArchives=data.contents.length===50; } else feedError=true;
    feedLoading=false; renderUpdates();
  }
  function renderUpdates() {
    const target=$("#updates-list"); if(!target || !account)return;
    const sub=subscription(), showAgent=!!(sub?.agent_enabled && activeAgent());
    const notices=[], reports=[];
    for(const event of displayedEvents) {
      if(!event.content_id) {
        const key={"account:expiring":"notice_expiring","account:expired":"notice_expired","account:payment_succeeded":"notice_paid","account:payment_failed":"notice_failed","account:access_granted":"notice_granted","account:payment_verifying":"notice_verifying","account:payment_review":"notice_review","account:payment_resolved":"notice_resolved"}[event.kind];
        const originalBody=event.summary.split("\n").slice(1).join("\n");
        const noticeBody=key&&event.kind!=="account:payment_resolved"?t(key+"_body"):localMessage(originalBody);
        notices.push('<article class="entry"><div><span class="badge">'+esc(t("account_notice"))+'</span><h3>'+esc(key?t(key):localMessage(event.summary.split("\n")[0]))+'</h3><p>'+esc(noticeBody)+'</p><p>'+esc(date(event.created_at,true))+(showAgent?' · '+esc(t(event.processing_state==='processed'?"processed":"pending_agent")):'')+'</p></div></article>');
      } else {
        const kind={published:"new_report",revised:"revised",withdrawn:"withdrawn"}[event.kind]||"new_report";
        reports.push('<article class="entry"><div><span class="badge">'+esc(t(kind))+'</span><h3>'+esc(event.summary)+'</h3><p>'+esc(date(event.created_at,true))+(showAgent?' · '+esc(t(event.processing_state==='processed'?"processed":"pending_agent")):'')+'</p></div>'+(event.withdrawn?'<span class="muted">'+esc(t("access_stopped"))+'</span>':'<a class="text-link" href="/read/'+encodeURIComponent(event.content_id)+'">'+esc(t("read_report"))+' ↗</a>')+'</article>');
      }
    }
    const seen=new Set(displayedEvents.map((e)=>e.content_id));
    const archives=displayedArchives.filter((c)=>!seen.has(c.id));
    const archiveHTML=archives.map((c)=>'<article class="entry"><div><h3>'+esc(c.title)+'</h3><p>'+esc(date(c.created_at))+'</p></div><a class="text-link" href="/read/'+encodeURIComponent(c.id)+'">'+esc(t("read_report"))+' ↗</a></article>').join("");
    target.innerHTML=(feedError?'<p class="error">'+esc(t("load_failed"))+'</p>':'')+(feedLoading&&!displayedEvents.length&&!displayedArchives.length?'<p class="muted">'+esc(t("loading"))+'</p>':'')+'<div id="events-list">'+(notices.length?'<div class="notice-list">'+notices.join("")+'</div>':'')+reports.join("")+'</div>'+(moreEvents?'<button id="load-older-events" class="quiet pagination">'+esc(t("load_older"))+'</button>':'')+(archives.length||moreArchives?'<div class="section-head"><h2>'+esc(t("archive"))+'</h2><span class="muted">'+esc(t("archive_access"))+'</span></div><div id="archive-list">'+archiveHTML+'</div>'+(moreArchives?'<button id="load-more-archive" class="quiet pagination">'+esc(t("load_archive"))+'</button>':''):'')+(!feedLoading&&!reports.length&&!archives.length&&!notices.length?'<div class="empty">'+esc(t(activeAccess(sub)?"no_reports":"no_subscription_reports"))+(!activeAccess(sub)?'<p><button type="button" class="text-button" data-open-access>'+esc(t("get_access"))+' ↗</button></p>':'')+'</div>':'');
    bind("#load-older-events","click",async()=>{if(eventBefore===null)return;const data=await api("/api/events?"+new URLSearchParams({limit:"100",newest:"true",before:String(eventBefore)}));const existing=new Set(displayedEvents.map(e=>e.id));displayedEvents.push(...data.events.filter(e=>e.product_id===QUANT&&!existing.has(e.id)));eventBefore=data.next_before;moreEvents=data.events.length===100;renderUpdates();});
    bind("#load-more-archive","click",async()=>{if(archiveBefore===null)return;const args=new URLSearchParams({limit:"50",before:String(archiveBefore)});if(archiveBeforeId)args.set("before_id",archiveBeforeId);const data=await api("/api/library?"+args);const existing=new Set(displayedArchives.map(e=>e.id));displayedArchives.push(...data.contents.filter(e=>e.product_id===QUANT&&!existing.has(e.id)));archiveBefore=data.next_before;archiveBeforeId=data.next_before_id;moreArchives=data.contents.length===50;renderUpdates();});
  }
  async function loadPurchaseOptions() {
    if(!$("#purchase-options"))return;
    try {
      const updated=await api("/api/purchase-options");
      const oldQuotes=new Map((purchaseData?.plans||[]).map(p=>[p.id,p.quote_id]));
      const changed=(updated.plans||[]).some(p=>oldQuotes.has(p.id)&&oldQuotes.get(p.id)!==p.quote_id);
      if(updated.terms_version!==purchaseData?.terms_version||changed)resetTerms();
      purchaseData=updated;purchaseError=false;
    }catch(_){purchaseError=true;}
    renderPurchase();
  }
  function purchaseEligible(plans) {
    const sub=subscription();
    if(!activeAccess(sub)) return true;
    if(!sub?.expires_at) return false; // lifetime access cannot be extended by a duplicate purchase
    return (plans||[]).some(p=>!p.access_until || Number(p.access_until)>Number(sub.expires_at));
  }
  function renderPurchase() {
    const target=$("#purchase-options"); if(!target)return;
    if(!purchaseData){target.innerHTML='<p class="muted">'+esc(t(purchaseError?"purchase_unavailable":"loading"))+'</p>';return;}
    const methods=(purchaseData.methods||[]).filter(m=>["xianyu","wechat","alipay","usdt"].includes(m.id));
    const plans=(purchaseData.plans||[]).filter(p=>p.id&&(Number(p.days)>0||Number(p.access_until)>Date.now()/1000)&&Number.isFinite(Number(p.amount_minor))&&money(p.amount_minor,p.currency));
    const enabled=methods.filter(m=>m.enabled&&["wechat","alipay"].includes(m.id));
    const xianyu=methods.find(m=>m.id==='xianyu'&&m.enabled), listing=safeURL(xianyu?.url);
    const oldPlan=$("#purchase-plan").value, oldMethod=$("input[name=payment-method]:checked")?.value;
    const eligible=purchaseEligible(plans);
    target.innerHTML=(listing?'<p><a class="text-link" href="'+esc(listing)+'" target="_blank" rel="noopener noreferrer">'+esc(t("xianyu_purchase"))+' ↗</a></p>':'')+(plans.length?'':'<p class="muted">'+esc(t("no_plans"))+'</p>')+(plans.length&&!enabled.length?'<p class="muted">'+esc(t("purchase_unavailable"))+'</p>':'')+(plans.length&&!eligible?'<p class="muted">'+esc(t("access_already_active"))+'</p>':'');
    $("#purchase-form").hidden=!(plans.length&&enabled.length&&eligible);
    $("#purchase-plan").innerHTML=plans.map(p=>'<option value="'+esc(p.id)+'" data-quote="'+esc(p.quote_id||"")+'">Quant · '+esc(p.access_until?t("plan_until",{date:new Intl.DateTimeFormat(lang==='en'?'en-GB':'zh-CN',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date(p.access_until*1000-1))}):t("plan_days",{days:p.days}))+' · '+esc(money(p.amount_minor,p.currency))+'</option>').join("");
    if(plans.some(p=>p.id===oldPlan))$("#purchase-plan").value=oldPlan;
    $("#purchase-methods").innerHTML=enabled.map((m,i)=>'<label><input type="radio" name="payment-method" value="'+esc(m.id)+'" '+(m.id===oldMethod||(!oldMethod&&i===0)?'checked':'')+' required>'+esc(t("method_"+m.id))+'</label>').join("");
  }
  function orderTerminal(order) {
    return !order || order.paid || ["paid","confirmed","complete","completed","expired","failed","cancelled","canceled","review","closed"].includes(order.status);
  }
  function setCurrentOrder(order, {updateURL = true} = {}) {
    currentOrder = order;
    orderPause = null; orderFailures = 0;
    if (updateURL && order?.id) {
      const url = new URL(location.href);
      url.searchParams.set("order", order.id);
      if (!url.searchParams.has("view")) url.searchParams.set("view", "subscriptions");
      history.replaceState(null, "", url.pathname + url.search + url.hash);
    }
    renderOrder();
  }
  function stopOrderCheck() { clearTimeout(orderTimer); orderTimer = null; }
  function scheduleOrderCheck(delay = 6000) {
    stopOrderCheck();
    orderTimer = setTimeout(() => {
      orderTimer = null;
      if (document.hidden) { scheduleOrderCheck(6000); return; }
      checkOrder(true);
    }, delay);
  }
  function renderOrder() {
    const target=$("#purchase-order");if(!target)return;
    if(!currentOrder){target.hidden=true;return;}
    target.hidden=false;
    const paid=!!currentOrder.paid||["paid","confirmed","complete","completed"].includes(currentOrder.status);
    const failed=["failed","cancelled","canceled"].includes(currentOrder.status), expired=currentOrder.status==='expired',closed=currentOrder.status==='closed';
    const held=["verifying","review"].includes(currentOrder.status);
    const blocked=held||["creating","pending"].includes(currentOrder.status);
    if($("#purchase-submit")){ $("#purchase-submit").dataset.locked=String(blocked); $("#purchase-submit").disabled=blocked; }
    const heading=orderPause==="session"?"order_session_expired":orderPause==="paused"?"order_check_paused":closed?"order_closed":currentOrder.status==='review'?"order_review":currentOrder.status==='verifying'?"order_verifying":paid?"order_paid":expired?"order_expired":failed?"order_failed":"order_pending";
    const pay=safeURL(currentOrder.pay_url), qr=safeURL(currentOrder.qr_url,{image:true});
    const embeddable=qr&&(qr.startsWith('data:')||new URL(qr).origin===location.origin);
    const amount=money(currentOrder.amount_minor,currentOrder.currency);
    const scan=currentOrder.method==='alipay'?t("scan_alipay"):t("scan_wechat");
    const showQr=!paid&&!expired&&!failed&&!held&&!closed&&embeddable;
    const fallback=pay||(qr&&!embeddable?qr:null);
    const showLink=!paid&&!expired&&!failed&&!held&&!closed&&!!(pay||(qr&&!embeddable));
    const showCheck=!paid&&!failed&&!closed;
    target.innerHTML='<div class="order-card">'
      +'<div class="order-head"><div><p class="eyebrow">'+esc(t("order_heading"))+'</p><h3>'+esc(t(heading))+'</h3></div>'+(paid?'<span class="order-badge" aria-hidden="true">✓</span>':'')+'</div>'
      +(amount?'<p class="order-amount">'+esc(amount)+'</p>':'')
      +(orderPause==="session"?'<p class="error">'+esc(t("order_session_expired"))+'</p>':'')
      +(orderPause==="paused"?'<p class="muted">'+esc(t("order_check_paused"))+'</p>':'')
      +(showQr?'<p class="order-hint">'+esc(scan)+'</p><div class="qr-wrap"><img class="order-qr" src="'+esc(qr)+'" alt="'+esc(t("qr_alt"))+'"></div>':'')
      +(showQr?'<div class="qr-fallback" hidden><p class="muted">'+esc(t("qr_unavailable"))+'</p>'+(fallback?'<a class="button" href="'+esc(fallback)+'" target="_blank" rel="noopener noreferrer">'+esc(t("open_payment"))+' ↗</a>':'')+'</div>':'')
      +(currentOrder.expires_at&&!paid&&!held&&!closed?'<p class="order-expiry">'+esc(t("order_expiry",{date:date(currentOrder.expires_at,true)}))+'</p>':'')
      +(showLink?'<div class="order-actions"><a class="button" href="'+esc(pay||qr)+'" target="_blank" rel="noopener noreferrer">'+esc(t("open_payment"))+' ↗</a><p class="muted">'+esc(t("open_payment_note"))+'</p></div>':'')
      +(showCheck?'<div class="order-actions"><button id="check-payment" class="button" type="button">'+esc(t("check_payment"))+'</button></div>':'')
      +'</div>';
    if(showQr){
      const image=target.querySelector(".order-qr");
      image?.addEventListener("error",()=>{image.hidden=true;const fallbackBox=target.querySelector(".qr-fallback");if(fallbackBox)fallbackBox.hidden=false;});
    }
    if(!target.dataset.shown){target.dataset.shown="1";target.scrollIntoView({behavior:"smooth",block:"center"});}
    bind("#check-payment","click",()=>checkOrder(false));
  }
  async function checkOrder(automatic=false) {
    if(!currentOrder?.id)return;
    stopOrderCheck();
    try {
      if(!automatic&&!currentOrder.paid&&!["paid","closed"].includes(currentOrder.status)){
        // Reconciliation is best-effort; only an invalid session blocks the
        // subsequent authoritative status read.
        try{await api("/api/orders/"+encodeURIComponent(currentOrder.id)+"/reconcile","POST",{});}catch(error){if(error.status===401)throw error;}
      }
      currentOrder=await api("/api/orders/"+encodeURIComponent(currentOrder.id));
      orderPause=null;orderFailures=0;renderOrder();
      if(orderTerminal(currentOrder)){
        if(currentOrder.paid||["paid","confirmed","complete","completed"].includes(currentOrder.status)){await loadAccount();toast(t("order_paid"));}
        return;
      }
      if(automatic){
        if(orderPolls++<20)scheduleOrderCheck(6000);
        else {orderPause="paused";renderOrder();}
      }
    } catch(error){
      if(!automatic)throw error;
      if(error.status===401){orderPause="session";renderOrder();return;}
      orderFailures++;
      if(error.status===404&&orderPolls++<20){scheduleOrderCheck(6000);return;}
      if(orderFailures<5&&orderPolls++<20){scheduleOrderCheck(Math.min(30000,3000*Math.pow(2,orderFailures)));return;}
      orderPause="paused";renderOrder();
    }
  }
  function renderAdmin() {
    if(!adminData)return;const data=adminData;
    $("#readiness").innerHTML=[[t("mail_system"),data.readiness.mail==='file'?t("local_test"):'SMTP'],[t("maintenance_agent"),t(data.readiness.ai?'enabled':'disabled')],[t("payments"),t(data.readiness.payments?'configured':'not_configured')],[t("environment"),t(data.readiness.production?'production':'local_test')]].map(([label,value])=>'<div class="stat"><small>'+esc(label)+'</small><strong>'+esc(value)+'</strong></div>').join("");
    const jobNames={"source:picks":"job_picks","source:weekly_digest":"job_digest","source:official_mail":"job_mail","source:health_site":"job_site","source:daily_admin_status":"job_admin","source:activate_parser":"job_parser_activate",login_mail:"job_login",account_mail:"job_account",deliver:"job_delivery",purchase_reconcile:"job_payment_check",webhook_test:"job_webhook_test",billing_cancel:"job_cancel_billing",billing_reconcile:"job_payment_check",diagnose:"job_diagnose",propose_parser:"job_parser_proposal",admin_summary:"job_admin"};
    $("#job-status").innerHTML=(data.jobs.length?data.jobs.map(j=>'<p>'+esc(jobNames[j.kind]?t(jobNames[j.kind]):j.kind)+' · '+esc(stateName(j.state))+' <strong>'+j.count+'</strong></p>').join(""):'<p class="muted">'+esc(t("no_jobs"))+'</p>')+((data.deliveries||[]).length?'<p><strong>'+esc(t("delivery_counts"))+'</strong></p>'+data.deliveries.map(d=>'<p>'+esc(bundle.strings[lang]?.["delivery_state_"+d.state]||stateName(d.state))+' <strong>'+d.count+'</strong></p>').join(""):'');
    $("#incident-list").innerHTML=data.incidents.length?data.incidents.map(i=>'<div class="entry"><div><span class="badge">'+esc(stateName(i.state))+'</span><p>'+esc(i.summary)+'</p><small class="muted">'+esc(t("occurrences",{count:i.count}))+'</small>'+(i.diagnosis?'<details><summary>'+esc(t("diagnosis"))+'</summary><pre>'+esc(i.diagnosis)+'</pre></details>':'')+'</div></div>').join(""):'<p class="muted">'+esc(t("no_incidents"))+'</p>';
    $("#members").innerHTML=data.members.map(m=>'<tr><td>'+esc(m.email)+'</td><td>'+esc(m.name)+'</td><td>'+esc(date(m.expires_at))+'</td><td>'+esc(t(m.email_enabled?'on':'off'))+'</td><td>'+esc(t(m.agent_enabled?'on':'off'))+'</td></tr>').join("");
    if($("#admin-orders"))$("#admin-orders").innerHTML=(data.orders||[]).length?'<div class="table-scroll"><table><thead><tr><th>'+esc(t("order_id"))+'</th><th>'+esc(t("method_label"))+'</th><th>'+esc(t("amount"))+'</th><th>'+esc(t("status_label"))+'</th></tr></thead><tbody>'+data.orders.map(o=>'<tr><td>'+esc(o.id)+'<br><span class="muted">'+esc(date(o.created_at,true))+'</span></td><td>'+esc(t("method_"+o.method))+'</td><td>'+esc(money(o.amount_minor,o.currency))+'</td><td>'+esc(bundle.strings[lang]?.["payment_state_"+o.status]||o.status)+'</td></tr>').join("")+'</tbody></table></div>':'<p class="muted">'+esc(t("no_orders"))+'</p>';
    $("#admin-recent").innerHTML=data.recent.map(e=>'<article class="entry"><div><h3>'+esc(e.summary.split("\n")[0])+'</h3><p class="muted">'+esc(date(e.created_at))+(e.withdrawn?' · '+esc(t("withdrawn")):'')+'</p></div><div class="entry-actions">'+(e.content_id&&!e.withdrawn?'<button class="quiet danger" data-withdraw="'+esc(e.content_id)+'">'+esc(t("withdraw_button"))+'</button>':'')+'<button class="quiet" data-replay="'+esc(e.id)+'">'+esc(t("replay_button"))+'</button></div></article>').join("");
  }
  async function loadAdmin(){const me=await api("/api/me");csrf=me.csrf;adminData=await api("/api/admin/status");renderAdmin();}
  document.addEventListener("change",async(event)=>{
    const el=event.target.closest("[data-pref]");if(!el)return;el.disabled=true;
    try{const value=el.type==='checkbox'?el.checked:el.value;await api('/api/subscriptions/'+encodeURIComponent(el.dataset.sid),'PATCH',{[el.dataset.pref]:value});const sub=subscription();if(sub)sub[el.dataset.pref]=value;toast(t("preferences_saved"));}
    catch(error){toast(error.message,true);await loadAccount({feed:false});}finally{if(el.isConnected)el.disabled=false;}
  });
  document.addEventListener("click",async(event)=>{
    const toggle=event.target.closest("[data-delivery-pref]");
    if(toggle){const sub=subscription();if(!sub)return;const pref=toggle.dataset.deliveryPref;const next=!Boolean(sub[pref]);toggle.disabled=true;try{await api('/api/subscriptions/'+encodeURIComponent(sub.id),'PATCH',{[pref]:next});await loadAccount({feed:false});toast(t("preferences_saved"));}catch(error){toast(error.message,true);}finally{if(toggle.isConnected)toggle.disabled=false;}return;}
    const access=event.target.closest("[data-open-access]");if(access){tab("subscriptions");return;}
    const button=event.target.closest("[data-subscribe],[data-revoke],[data-withdraw],[data-replay]");if(!button)return;
    if(button.dataset.revoke&&!confirm(t("revoke_confirm")))return;
    button.disabled=true;
    try{
      if(button.dataset.subscribe){await api('/api/subscriptions','POST',{product_id:QUANT});await loadAccount();toast(t("subscribed"));}
      else if(button.dataset.revoke){await api('/api/bindings/'+encodeURIComponent(button.dataset.revoke),'DELETE');await loadAccount({feed:false});toast(t("revoked_toast"));}
      else if(button.dataset.withdraw){const reason=prompt(t("withdraw_prompt"));if(reason===null)return;await api('/api/admin/content/'+encodeURIComponent(button.dataset.withdraw)+'/withdraw','POST',{reason});await loadAdmin();toast(t("withdraw_done"));}
      else if(button.dataset.replay){const path='/api/admin/replay/'+encodeURIComponent(button.dataset.replay);const preview=await api(path,'POST',{apply:false});if(confirm(t("replay_confirm",{count:preview.eligible_users}))){await api(path,'POST',{apply:true});await loadAdmin();toast(t("replay_done"));}}
    }catch(error){toast(error.message,true);}finally{if(button.isConnected)button.disabled=false;}
  });
  function clearCodeCooldown(){if(codeCooldownTimer){clearInterval(codeCooldownTimer);codeCooldownTimer=null;}}
  function startCodeCooldown(){
    clearCodeCooldown();
    const button=$("#resend-code"); if(!button)return;
    let left=60;
    button.disabled=true;
    button.textContent=t("resend_in",{seconds:left});
    codeCooldownTimer=setInterval(()=>{
      left-=1;
      if(left<=0){clearCodeCooldown();button.disabled=false;button.textContent=t("resend_code");}
      else button.textContent=t("resend_in",{seconds:left});
    },1000);
  }
  async function requestLoginCode(event){
    if(!$("#email").reportValidity())return;
    const source=event?.currentTarget?.id==="resend-code"?$("#resend-code"):$("#request-code");
    if(!source)return;
    source.disabled=true;
    try{
      await api('/api/auth/request','POST',{email:$("#email").value});
      codeRequested=true;
      $("#code-fields").hidden=false;
      $("#code").required=true;
      $("#code").value='';
      $("#verify-code").disabled=true;
      $("#code").focus();
      $("#request-code").hidden=true;
      $("#login-status").dataset.statusKey='code_sent';
      status($("#login-status"),t("code_sent"));
      startCodeCooldown();
    }catch(error){
      $("#login-status").dataset.statusKey='';
      status($("#login-status"),error.message,true);
      source.disabled=false;
    }
  }
  bind("#request-code","click",requestLoginCode);
  $("#resend-code")?.addEventListener("click",requestLoginCode);
  bind("#change-email","click",()=>{codeRequested=false;clearCodeCooldown();$("#code-fields").hidden=true;$("#code").required=false;$("#code").value='';$("#verify-code").disabled=true;$("#request-code").hidden=false;$("#request-code").disabled=false;$("#resend-code").disabled=true;$("#resend-code").textContent=t("resend_code");$("#login-status").dataset.statusKey='';status($("#login-status"),'');$("#email").focus();});
  $("#code")?.addEventListener("input",()=>{const input=$("#code");input.value=input.value.replace(/\D/g,"").slice(0,6);$("#verify-code").disabled=input.value.length!==6;});
  bind("#login-form","submit",async()=>{
    let verified;
    try {
      verified=await api('/api/auth/verify','POST',{
        email:$("#email").value,
        code:$("#code").value,
        pair_code:entry.has("pair")&&entry.get("manual")!=="1"?($("#pair-code")?.value.trim().toUpperCase()||""):"",
      });
    }catch(error){$("#login-status").dataset.statusKey='';throw error;}
    $("#code").value='';$("#code-fields").hidden=true;$("#code").required=false;$("#request-code").hidden=true;
    if(verified.pair_binding_id){
      pairBindingId=verified.pair_binding_id;pairApproved=true;pairDetails=null;connectionPolls=0;
      $("#pair-code").value='';
      const clean=new URL(location.href);clean.searchParams.delete("pair");history.replaceState(null,"",clean.pathname+clean.search+clean.hash);
    }else if(verified.pair_error){
      status($("#pair-error"),localMessage(verified.pair_error),true);
    }
    try{await finishSignIn();toast(t(verified.pair_binding_id?"pair_saved":"verified"));}catch(error){$("#retry-account").hidden=false;$("#login-status").dataset.statusKey='account_load_failed';status($("#login-status"),t("account_load_failed"),true);}
  });
  bind("#retry-account", "click", async () => { await finishSignIn(); $("#retry-account").hidden=true; });
  bind("#onboard-read", "click", () => finishOnboarding("updates"));
  bind("#onboard-agent", "click", () => { onboardingAgent=true; connectionPolls=0; renderAccount(); $("#onboard-connect-heading").focus({preventScroll:true}); watchConnection(); });
  bind("#onboard-agent-back", "click", () => { onboardingAgent=false; clearTimeout(connectionTimer); connectionTimer=null; renderAccount(); $("#welcome-heading").focus({preventScroll:true}); });
  bind("#onboard-connect-finish", "click", () => { if(selectedAgent()) finishOnboarding("updates"); });
  bind("#onboard-connect-later", "click", () => finishOnboarding("updates"));
  bind("#onboard-connect-refresh", "click", async () => { connectionPolls=0; await loadAccount({feed:false}); });
  bind("#onboard-enable-email", "click", async () => {
    const sub = subscription(); if (!sub || !activeAccess(sub)) return;
    await api('/api/subscriptions/' + encodeURIComponent(sub.id), 'PATCH', {email_enabled:true});
    await loadAccount({feed:false}); toast(t("preferences_saved"));
  });
  bind("#logout","click",async()=>{await api('/api/auth/logout','POST',{});location.reload();});
  bind("#copy-guide","click",async()=>{const input=$("#guide-url");try{await navigator.clipboard.writeText(input.value);toast(t("copied"));}catch(_){input.focus();input.select();toast(t("copy_manual"));}});
  bind("#copy-agent-prompt", "click", async () => {
    const prompt = t("agent_prompt", {url:location.origin});
    const field = $("#agent-prompt-text"); field.value = prompt;
    try { await navigator.clipboard.writeText(prompt); if (field.dataset.fallback) field.hidden = true; toast(t("copied")); }
    catch (_) { field.hidden = false; field.focus(); field.select(); toast(t("copy_manual")); }
  });
  bind("#inspect-pair", "click", inspectPair);
  bind("#pair-code", "input", () => { pairDetails=null; pairCheckedCode=""; pairApproved=false; pairAutoTried=false; renderPair(); status($("#pair-error"), ""); });
  bind("#pair-form","submit",async()=>{
    const code=$("#pair-code").value.trim().toUpperCase();
    if (!pairDetails || pairDetails.code !== code || pairDetails.expires_at * 1000 <= Date.now()) throw new Error(t("pair_link_invalid"));
    const approved=await api('/api/pair/approve','POST',{code}); pairBindingId=approved.binding_id; pairApproved=true; connectionPolls=0; pairDetails=null; $("#pair-code").value=''; const clean=new URL(location.href); clean.searchParams.delete("pair"); clean.searchParams.delete("manual"); history.replaceState(null,"",clean.pathname+clean.search+clean.hash);
    await loadAccount({feed:false}); toast(t("pair_saved"));
  });
  bind("#binding-mode","change",()=>{$("#endpoint-field").hidden=$("#binding-mode").value!=='webhook';$("#endpoint").required=$("#binding-mode").value==='webhook';});
  bind("#binding-form","submit",async()=>{const result=await api('/api/bindings','POST',{name:$("#binding-name").value,mode:$("#binding-mode").value,endpoint:$("#endpoint").value||null});$("#binding-secret").hidden=false;$("#secret-value").value='Bearer token: '+result.token+(result.webhook_secret?'\nWebhook signing secret: '+result.webhook_secret:'');await loadAccount({feed:false});toast(t("connection_created"));});
  bind("#redeem-form","submit",async()=>{const code=$("#redeem-code").value.trim();try{await api('/api/redeem','POST',{code});$("#redeem-code").value='';$("#redeem-status").dataset.statusKey='redeemed';status($("#redeem-status"),t("redeemed"));await loadAccount();}catch(error){$("#redeem-status").dataset.statusKey='';status($("#redeem-status"),error.message,true);throw error;}});
  function resetTerms() { agreedTermsVersion = null; agreedQuoteId = null; if($("#accept-terms")) $("#accept-terms").checked = false; }
  bind("#accept-terms", "change", () => {
    const checked=!!$("#accept-terms").checked;
    agreedTermsVersion=checked?purchaseData?.terms_version:null;
    agreedQuoteId=checked?($("#purchase-plan").selectedOptions[0]?.dataset.quote||null):null;
  });
  bind("#purchase-plan", "change", resetTerms);
  $("#purchase-methods")?.addEventListener("change", resetTerms);
  bind("#purchase-form","submit",async()=>{
    const method=$("input[name=payment-method]:checked")?.value;if(!method)return;
    const planOption=$("#purchase-plan").selectedOptions[0];
    const quote=planOption?.dataset.quote||agreedQuoteId||null;
    if(!$("#accept-terms").checked||!agreedTermsVersion||agreedTermsVersion!==purchaseData?.terms_version)throw new Error(t("terms_required"));
    if(agreedQuoteId&&quote&&agreedQuoteId!==quote)throw new Error(t("quote_changed"));
    const request={plan_id:$("#purchase-plan").value,method,accept_terms:true,terms_version:agreedTermsVersion,...(quote?{quote_id:quote}:{})};
    try {
      const order=await api('/api/purchase','POST',request);
      resetTerms();orderPolls=0;setCurrentOrder(order);scheduleOrderCheck(6000);
    } catch(error) {
      if(error.status===409&&error.data?.order_id){
        const existing=await api('/api/orders/'+encodeURIComponent(error.data.order_id));
        setCurrentOrder(existing);toast(t("order_recovered"));return;
      }
      if(error.status===409&&(error.data?.quote_changed||error.data?.code==="quote_changed")){
        await loadPurchaseOptions();resetTerms();throw new Error(t("quote_changed"));
      }
      if(!error.status){
        // The POST may have reached the server even when the response was
        // lost. Recover the owner's existing pending order instead of
        // encouraging a second charge.
        try{
          const recent=await api('/api/orders');
          const pending=(recent.orders||[]).find(o=>["creating","pending","verifying","review"].includes(o.status));
          if(pending){setCurrentOrder(pending);toast(t("order_recovered"));return;}
        }catch(_){}
      }
      throw error;
    }
  });
  bind("#refresh-reports","click",loadReports);
  bind("#refresh-admin","click",loadAdmin);
  bind("#job-form","submit",async()=>{await api('/api/admin/jobs','POST',{kind:$("#job-kind").value});await loadAdmin();toast(t("job_queued"));});
  bind("#publish-form","submit",async()=>{const selected=[...$("#files").files];if(selected.length>12||selected.reduce((sum,file)=>sum+file.size,0)>17*1024*1024)throw new Error(t("file_limit"));const files=await Promise.all(selected.map(file=>new Promise((resolve,reject)=>{const reader=new FileReader();reader.onerror=()=>reject(new Error(t("file_read_error")));reader.onload=()=>resolve({name:file.name,base64:String(reader.result).split(',')[1]});reader.readAsDataURL(file);})));const result=await api('/api/admin/publish','POST',{product_id:$("#publish-product").value,source_key:$("#source-key").value||undefined,title:$("#title").value,body:$("#body").value,changes:$("#changes").value,files});status($("#publish-result"),t("published",{number:result.version}));await loadAdmin();toast(t("published",{number:result.version}));});
  if($("#guide-url"))$("#guide-url").value=t("agent_prompt",{url:location.origin});
  if($("#workspace")){const args=new URLSearchParams(location.search);currentTab=args.has('pair')?'connections':args.get('view')||(args.has('product')?'subscriptions':'updates');if(args.has('pair'))$("#pair-code").value=args.get('pair');loadAccount().then(()=>{if(!activeAccess(subscription())&&!args.has('view')&&!args.has('pair')){currentTab='subscriptions';renderAccount();}if(onboarding&&nextReport())returnToReport();}).catch(error=>{if(error.status!==401)toast(error.message,true);});}
  if($("#admin"))loadAdmin().catch(error=>toast(error.message,true));
  window.addEventListener('pagehide',()=>{stopOrderCheck();clearTimeout(connectionTimer);if($("#secret-value"))$("#secret-value").value='';if($("#redeem-code"))$("#redeem-code").value='';});
  window.addEventListener('pageshow',()=>{if(currentOrder&&!orderTerminal(currentOrder)&&!orderPause&&document.visibilityState==='visible')checkOrder(true);});
  document.addEventListener('visibilitychange',()=>{if(!document.hidden&&currentOrder&&!orderTerminal(currentOrder)&&!orderPause)checkOrder(true);});
  applyLanguage(lang,false);
  loadLandingPrice();
})();
