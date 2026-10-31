"use strict";
(() => {
  const $ = (selector) => document.querySelector(selector);
  const status = (text, error = false) => {
    const element = $("#oauth-status");
    if (!element) return;
    element.textContent = text;
    element.classList.toggle("error", error);
    element.classList.toggle("success", !error && !!text);
  };
  const request = async (path, body) => {
    const response = await fetch(path, {
      method: "POST",
      credentials: "same-origin",
      headers: {"Content-Type": "application/json", "Accept": "application/json"},
      body: JSON.stringify(body),
    });
    let data = {};
    try { data = await response.json(); } catch (_) {}
    if (!response.ok) throw new Error(data.error || data.error_description || "Request failed");
    return data;
  };

  const loginForm = $("#oauth-login-form");
  const codeForm = $("#oauth-code-form");
  if (loginForm) {
    loginForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const button = loginForm.querySelector("button");
      button.disabled = true;
      status("Sending a sign-in code…");
      try {
        await request("/api/auth/request", {email: $("#oauth-email").value.trim()});
        codeForm.hidden = false;
        $("#oauth-code").focus();
        status("Code sent. Check your inbox.");
      } catch (error) {
        status(error.message, true);
      } finally {
        button.disabled = false;
      }
    });
  }
  const safeRedirect = (value) => {
    const target = new URL(value);
    const loopback = target.protocol === "http:" && ["localhost", "127.0.0.1", "[::1]"].includes(target.hostname);
    const cursorCallback = target.protocol === "cursor:" && target.hostname === "anysphere.cursor-mcp" && target.pathname === "/oauth/callback";
    if (target.protocol !== "https:" && !loopback && !cursorCallback) {
      throw new Error("Invalid authorization redirect");
    }
    return target.href;
  };
  if (codeForm) {
    codeForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const button = codeForm.querySelector("button");
      button.disabled = true;
      status("Verifying…");
      try {
        const result = await request("/api/auth/verify", {
          email: $("#oauth-email").value.trim(),
          code: $("#oauth-code").value.trim(),
          oauth_request_id: codeForm.dataset.requestId || "",
        });
        if (result.oauth_redirect) {
          location.assign(safeRedirect(result.oauth_redirect));
        } else if (result.oauth_error) {
          status(result.oauth_error, true);
        } else {
          location.reload();
        }
      } catch (error) {
        status(error.message, true);
      } finally {
        button.disabled = false;
      }
    });
  }

  const decisionForm = $("#oauth-decision-form");
  if (decisionForm) {
    decisionForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const submitter = event.submitter;
      if (!submitter) return;
      const button = submitter;
      button.disabled = true;
      status("Working…");
      const body = new URLSearchParams(new FormData(decisionForm));
      body.set(submitter.name, submitter.value);
      try {
        const response = await fetch("/oauth/authorize/decision", {
          method: "POST",
          credentials: "same-origin",
          headers: {
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
          },
          body,
        });
        let data = {};
        try { data = await response.json(); } catch (_) {}
        if (!response.ok) throw new Error(data.error_description || data.error || "Authorization failed");
        location.assign(safeRedirect(data.redirect));
      } catch (error) {
        status(error.message, true);
        button.disabled = false;
      }
    });
  }
})();
