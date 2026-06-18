// ==UserScript==
// @name         Quark Pan Token Exporter
// @namespace    local.pan_downloader.quark
// @version      1.0.0
// @description  Export Quark Pan cookies for quark_pan_direct_downloader.py.
// @match        https://pan.quark.cn/*
// @match        https://drive-pc.quark.cn/*
// @match        https://*.quark.cn/*
// @match        https://*.uc.cn/*
// @run-at       document-idle
// @grant        GM_cookie
// @grant        GM_setClipboard
// @grant        GM_notification
// ==/UserScript==

(function () {
  "use strict";

  const COOKIE_URLS = ["https://pan.quark.cn/", "https://drive-pc.quark.cn/"];
  const FIELD_DEFS = [
    ["QUARK_COOKIE", "Cookie header"],
    ["QUARK_TO_PDIR_FID", "Save target folder fid"]
  ];

  function listCookies(details) {
    return new Promise((resolve) => {
      if (typeof GM_cookie === "undefined" || !GM_cookie.list) {
        resolve([]);
        return;
      }
      GM_cookie.list(details, (cookies, error) => {
        if (error) {
          console.warn("GM_cookie.list error:", error);
          resolve([]);
          return;
        }
        resolve(Array.isArray(cookies) ? cookies : []);
      });
    });
  }

  async function buildCookieHeader() {
    const cookieMap = new Map();
    for (const url of COOKIE_URLS) {
      const cookies = await listCookies({ url });
      for (const cookie of cookies) {
        if (cookie.name && cookie.value) cookieMap.set(cookie.name, cookie.value);
      }
    }
    return Array.from(cookieMap.entries()).map(([name, value]) => `${name}=${value}`).join("; ");
  }

  function copyText(text) {
    if (typeof GM_setClipboard === "function") {
      GM_setClipboard(text, "text");
    } else {
      navigator.clipboard.writeText(text);
    }
  }

  function notify(text) {
    if (typeof GM_notification === "function") {
      GM_notification({ title: "Quark Pan Token Exporter", text, timeout: 2500 });
    } else {
      alert(text);
    }
  }

  function createField(labelText, value) {
    const wrapper = document.createElement("label");
    wrapper.style.cssText = "display:block;margin-top:8px;font-size:12px;color:#374151;";
    const label = document.createElement("div");
    label.textContent = labelText;
    label.style.cssText = "margin-bottom:4px;font-weight:600;";
    const input = document.createElement("input");
    input.type = "text";
    input.value = value || "";
    input.style.cssText = "width:100%;box-sizing:border-box;padding:6px;border:1px solid #d1d5db;border-radius:6px;font:12px Consolas,monospace;";
    wrapper.append(label, input);
    return { wrapper, input };
  }

  function createPanel() {
    const panel = document.createElement("div");
    panel.style.cssText = "position:fixed;right:18px;bottom:18px;z-index:2147483647;width:460px;max-width:calc(100vw - 36px);padding:12px;border:1px solid #d1d5db;border-radius:8px;background:#fff;box-shadow:0 10px 24px rgba(15,23,42,.16);font:13px/1.45 Arial,sans-serif;color:#111827;";
    const title = document.createElement("div");
    title.textContent = "Quark Pan Token Exporter";
    title.style.cssText = "font-weight:700;margin-bottom:8px;";
    const status = document.createElement("div");
    status.style.cssText = "margin-bottom:8px;color:#4b5563;";
    const textarea = document.createElement("textarea");
    textarea.readOnly = true;
    textarea.style.cssText = "width:100%;height:92px;box-sizing:border-box;resize:vertical;border:1px solid #d1d5db;border-radius:6px;padding:8px;font:12px/1.4 Consolas,monospace;background:#f9fafb;color:#111827;";
    const manual = document.createElement("div");
    manual.style.cssText = "margin-top:8px;border-top:1px solid #e5e7eb;padding-top:8px;";
    const fields = {};
    for (const [key, label] of FIELD_DEFS) {
      const field = createField(label, key === "QUARK_TO_PDIR_FID" ? "0" : "");
      fields[key] = field.input;
      manual.appendChild(field.wrapper);
    }
    const row = document.createElement("div");
    row.style.cssText = "display:flex;gap:8px;margin-top:8px;";
    const refreshButton = document.createElement("button");
    refreshButton.textContent = "Refresh";
    const copyButton = document.createElement("button");
    copyButton.textContent = "Copy JSON";
    const closeButton = document.createElement("button");
    closeButton.textContent = "Close";
    [refreshButton, copyButton, closeButton].forEach((button) => {
      button.style.cssText = "border:1px solid #cbd5e1;border-radius:6px;background:#fff;padding:6px 10px;cursor:pointer;";
    });
    row.append(refreshButton, copyButton, closeButton);
    panel.append(title, status, textarea, manual, row);
    document.documentElement.appendChild(panel);

    function buildJson() {
      return JSON.stringify({
        QUARK_COOKIE: fields.QUARK_COOKIE.value.trim(),
        QUARK_TO_PDIR_FID: fields.QUARK_TO_PDIR_FID.value.trim() || "0"
      }, null, 2);
    }

    async function refresh() {
      fields.QUARK_COOKIE.value = await buildCookieHeader();
      textarea.value = buildJson();
      const ok = fields.QUARK_COOKIE.value;
      status.textContent = ok ? "Cookie found. Copy JSON to credentials.local.json." : "Cookie not found automatically. Fill it manually below.";
      status.style.color = ok ? "#166534" : "#b45309";
    }

    refreshButton.addEventListener("click", refresh);
    copyButton.addEventListener("click", () => {
      textarea.value = buildJson();
      copyText(textarea.value);
      notify("JSON copied.");
    });
    closeButton.addEventListener("click", () => panel.remove());
    refresh();
  }

  createPanel();
})();

