"use strict";
(() => {
  const $ = (selector) => document.querySelector(selector);
  const bundle = JSON.parse($("#quant-i18n")?.textContent || '{"strings":{},"errors":{}}');
  let lang = document.documentElement.lang === "en" ? "en" : "zh-CN";
  const t = (key, vars = {}) => (bundle.strings[lang]?.[key] || key).replace(/\{(\w+)\}/g, (all, name) => vars[name] ?? all);
  function localMessage(value) {
    const text = String(value || "");
    if (lang === "en") return bundle.errors[text] || (/[\u3400-\u9fff]/.test(text) ? t("generic_error") : text);
    const original = Object.keys(bundle.errors).find((key) => bundle.errors[key] === text);
    return original || text;
  }
  let statusState = { key: null, text: "", args: {}, error: false };
  let codeTargetEmail = "";
  const renderStatus = () => {
    const element = $("#oauth-status");
    if (!element) return;
    const text = statusState.key ? t(statusState.key, statusState.args) : localMessage(statusState.text);
    element.textContent = text;
    element.classList.toggle("error", !!statusState.error);
    element.classList.toggle("success", !statusState.error && !!text);
  };
  const setStatus = (text = "", { error = false, key = null, args = {} } = {}) => {
    statusState = { key, text, args, error };
    renderStatus();
  };
  const renderCodeTarget = () => {
    const element = $("#oauth-code-target");
    if (!element) return;
    element.hidden = !codeTargetEmail;
    if (codeTargetEmail) element.textContent = t("oauth_code_sent", { email: codeTargetEmail });
  };
  const fail = (error) => {
    if (error?.key) setStatus("", { error: true, key: error.key });
    else setStatus(error?.message || t("oauth_request_failed"), { error: true });
  };
  const timeoutError = () => {
    const error = new Error(t("oauth_timeout"));
    error.key = "oauth_timeout";
    return error;
  };
  const request = async (path, body, contentType = "application/json") => {
    const controller = new AbortController();
    const deadline = setTimeout(() => controller.abort(), 20000);
    try {
      const response = await fetch(path, {
        method: "POST",
        credentials: "same-origin",
        signal: controller.signal,
        headers: { "Content-Type": contentType, "Accept": "application/json", "Accept-Language": lang },
        body: contentType === "application/json" ? JSON.stringify(body) : body,
      });
      let data = {};
      try { data = await response.json(); } catch (_) {}
      if (!response.ok) throw new Error(data.error_description || data.error || t("oauth_request_failed"));
      return data;
    } catch (error) {
      if (error?.name === "AbortError" || error instanceof TypeError) throw timeoutError();
      throw error;
    } finally {
      clearTimeout(deadline);
    }
  };
  const safeRedirect = (value) => {
    try {
      const target = new URL(value);
      const loopback = target.protocol === "http:" && ["localhost", "127.0.0.1", "[::1]"].includes(target.hostname);
      const cursorCallback = target.protocol === "cursor:" && target.hostname === "anysphere.cursor-mcp" && target.pathname === "/oauth/callback";
      if (target.protocol !== "https:" && !loopback && !cursorCallback) throw new Error();
      return target.href;
    } catch (_) {
      const error = new Error(t("oauth_redirect_invalid"));
      error.key = "oauth_redirect_invalid";
      throw error;
    }
  };

  // app.js owns the shared language links; re-render dynamic OAuth status after
  // it has switched the document language, without losing form input.
  document.querySelectorAll("[data-language]").forEach((link) => link.addEventListener("click", () => {
    setTimeout(() => {
      lang = document.documentElement.lang === "en" ? "en" : "zh-CN";
      renderStatus();
      renderCodeTarget();
    }, 0);
  }));
  renderStatus();
  renderCodeTarget();

  const loginForm = $("#oauth-login-form");
  const codeForm = $("#oauth-code-form");
  if (loginForm) {
    loginForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const button = loginForm.querySelector("button");
      button.disabled = true;
      setStatus("", { key: "oauth_sending" });
      try {
        codeTargetEmail = $("#oauth-email").value.trim();
        await request("/api/auth/request", { email: codeTargetEmail });
        codeForm.hidden = false;
        renderCodeTarget();
        $("#oauth-code").focus();
        setStatus("", { key: "oauth_code_sent", args: { email: codeTargetEmail } });
      } catch (error) {
        fail(error);
      } finally {
        button.disabled = false;
      }
    });
  }
  if (codeForm) {
    codeForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const button = codeForm.querySelector("button");
      button.disabled = true;
      setStatus("", { key: "oauth_verifying" });
      try {
        const result = await request("/api/auth/verify", {
          email: $("#oauth-email").value.trim(),
          code: $("#oauth-code").value.trim(),
          oauth_request_id: codeForm.dataset.requestId || "",
        });
        if (result.oauth_redirect) {
          location.assign(safeRedirect(result.oauth_redirect));
        } else if (result.oauth_error) {
          setStatus(result.oauth_error, { error: true });
        } else {
          location.reload();
        }
      } catch (error) {
        fail(error);
      } finally {
        button.disabled = false;
      }
    });
  }

  const decisionForm = $("#oauth-decision-form");
  if (decisionForm) {
    decisionForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const button = event.submitter;
      if (!button) return;
      button.disabled = true;
      setStatus("", { key: "oauth_working" });
      const body = new URLSearchParams(new FormData(decisionForm));
      body.set(button.name, button.value);
      try {
        const data = await request("/oauth/authorize/decision", body, "application/x-www-form-urlencoded");
        location.assign(safeRedirect(data.redirect));
      } catch (error) {
        fail(error);
        button.disabled = false;
      }
    });
  }
})();
