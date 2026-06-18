// ==UserScript==
// @name         Aliyun Drive Token Exporter
// @namespace    local.pan_downloader.aliyun
// @version      1.0.0
// @description  Export Aliyun Drive tokens for aliyun_drive_direct_downloader.py.
// @match        https://www.aliyundrive.com/*
// @match        https://www.alipan.com/*
// @match        https://aliyundrive.com/*
// @match        https://alipan.com/*
// @run-at       document-idle
// @grant        GM_setClipboard
// @grant        GM_notification
// ==/UserScript==

(function () {
  "use strict";

  const FIELD_DEFS = [
    ["ALIYUN_ACCESS_TOKEN", "Access token"],
    ["ALIYUN_REFRESH_TOKEN", "Refresh token"],
    ["ALIYUN_DEFAULT_DRIVE_ID", "Default drive id"]
  ];

  function parseMaybeJson(value) {
    if (!value || typeof value !== "string") return null;
    const text = value.trim();
    if (!text || (!text.startsWith("{") && !text.startsWith("["))) return null;
    try {
      return JSON.parse(text);
    } catch (_) {
      return null;
    }
  }

  function walk(value, visitor, seen) {
    if (value == null) return;
    if (typeof value === "string") {
      const parsed = parseMaybeJson(value);
      if (parsed) walk(parsed, visitor, seen);
      return;
    }
    if (typeof value !== "object") return;
    if (seen.has(value)) return;
    seen.add(value);
    if (Array.isArray(value)) {
      value.forEach((item) => walk(item, visitor, seen));
      return;
    }
    for (const [key, item] of Object.entries(value)) {
      visitor(key, item);
      walk(item, visitor, seen);
    }
  }

  function isAccessToken(value) {
    return typeof value === "string" && value.trim().split(".").length === 3;
  }

  function setLongestAccessToken(result, value) {
    const token = typeof value === "string" ? value.trim() : "";
    if (isAccessToken(token) && token.length > result.ALIYUN_ACCESS_TOKEN.length) {
      result.ALIYUN_ACCESS_TOKEN = token;
    }
  }

  function scanStorage() {
    const result = {
      ALIYUN_ACCESS_TOKEN: "",
      ALIYUN_REFRESH_TOKEN: "",
      ALIYUN_DEFAULT_DRIVE_ID: ""
    };
    const accessKeys = new Set(["access_token", "accessToken", "token", "access"]);
    const refreshKeys = new Set(["refresh_token", "refreshToken", "refresh"]);
    const driveKeys = new Set(["default_drive_id", "defaultDriveId", "resource_drive_id"]);

    function readStore(store) {
      for (let i = 0; i < store.length; i += 1) {
        const key = store.key(i);
        const value = store.getItem(key) || "";
        if (accessKeys.has(key)) setLongestAccessToken(result, value);
        if (refreshKeys.has(key) && value.length > result.ALIYUN_REFRESH_TOKEN.length) {
          result.ALIYUN_REFRESH_TOKEN = value.trim();
        }
        const parsed = parseMaybeJson(value);
        if (!parsed) continue;
        walk(parsed, (childKey, childValue) => {
          if (typeof childValue !== "string") return;
          if (accessKeys.has(childKey)) setLongestAccessToken(result, childValue);
          if (refreshKeys.has(childKey) && childValue.length > result.ALIYUN_REFRESH_TOKEN.length) {
            result.ALIYUN_REFRESH_TOKEN = childValue;
          }
          if (driveKeys.has(childKey) && childValue.length > result.ALIYUN_DEFAULT_DRIVE_ID.length) {
            result.ALIYUN_DEFAULT_DRIVE_ID = childValue;
          }
        }, new WeakSet());
      }
    }

    readStore(localStorage);
    readStore(sessionStorage);
    return result;
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
      GM_notification({ title: "Aliyun Drive Token Exporter", text, timeout: 2500 });
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
    panel.style.cssText = [
      "position:fixed",
      "right:18px",
      "bottom:18px",
      "z-index:2147483647",
      "width:420px",
      "max-width:calc(100vw - 36px)",
      "padding:12px",
      "border:1px solid #d1d5db",
      "border-radius:8px",
      "background:#fff",
      "box-shadow:0 10px 24px rgba(15,23,42,.16)",
      "font:13px/1.45 Arial,sans-serif",
      "color:#111827"
    ].join(";");

    const title = document.createElement("div");
    title.textContent = "Aliyun Drive Token Exporter";
    title.style.cssText = "font-weight:700;margin-bottom:8px;";
    const status = document.createElement("div");
    status.style.cssText = "margin-bottom:8px;color:#4b5563;";
    const textarea = document.createElement("textarea");
    textarea.readOnly = true;
    textarea.style.cssText = "width:100%;height:94px;box-sizing:border-box;resize:vertical;border:1px solid #d1d5db;border-radius:6px;padding:8px;font:12px/1.4 Consolas,monospace;background:#f9fafb;color:#111827;";

    const fields = {};
    const manual = document.createElement("div");
    manual.style.cssText = "margin-top:8px;border-top:1px solid #e5e7eb;padding-top:8px;";
    for (const [key, label] of FIELD_DEFS) {
      const field = createField(label, "");
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
      const data = {};
      for (const [key] of FIELD_DEFS) {
        data[key] = fields[key].value.trim();
      }
      return JSON.stringify(data, null, 2);
    }

    function refresh() {
      const found = scanStorage();
      for (const [key] of FIELD_DEFS) {
        if (found[key]) fields[key].value = found[key];
      }
      textarea.value = buildJson();
      status.textContent = found.ALIYUN_ACCESS_TOKEN ? "Token found in browser storage." : "Token not found automatically. Fill it manually below.";
      status.style.color = found.ALIYUN_ACCESS_TOKEN ? "#166534" : "#b45309";
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
