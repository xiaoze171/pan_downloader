// ==UserScript==
// @name         Baidu Pan Cookie Exporter (手动/自动双模式)
// @namespace    local.baidu_pan_downloader
// @version      2.0.0
// @description  自动读取HttpOnly的BDUSS/STOKEN；若失败则提供手动输入备用方案
// @match        https://pan.baidu.com/*
// @match        https://passport.baidu.com/*
// @match        https://*.baidu.com/*
// @run-at       document-idle
// @grant        GM_cookie
// @grant        GM_setClipboard
// @grant        GM_notification
// ==/UserScript==

(function () {
  "use strict";

  // ======================== 配置 ========================
  const COOKIE_NAMES = ["BDUSS", "BDUSS_BFESS", "STOKEN", "STOKEN_BFESS"];
  const COOKIE_URLS = ["https://pan.baidu.com/"];

  // ======================== GM_cookie 读取 ========================
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

  async function autoReadCookies() {
    const cookieMap = new Map();

    for (const url of COOKIE_URLS) {
      const cookies = await listCookies({ url });
      for (const cookie of cookies) {
        if (COOKIE_NAMES.includes(cookie.name) && cookie.value) {
          cookieMap.set(cookie.name, cookie.value);
        }
      }
    }

    const BDUSS = cookieMap.get("BDUSS") || cookieMap.get("BDUSS_BFESS") || "";
    const STOKEN = cookieMap.get("STOKEN") || cookieMap.get("STOKEN_BFESS") || "";
    return { BDUSS, STOKEN };
  }

  // ======================== 手动模式界面 ========================
  function buildManualPanel(container, onExport) {
    // 清空容器
    container.innerHTML = "";

    const info = document.createElement("div");
    info.textContent = "⚠️ 无法自动读取 Cookie（可能缺少GM_cookie权限或未登录）。请手动填写：";
    info.style.cssText = "margin-bottom:8px;color:#b45309;font-size:12px;";

    const bdussLabel = document.createElement("div");
    bdussLabel.textContent = "BDUSS:";
    bdussLabel.style.cssText = "margin-top:6px;font-weight:500;";
    const bdussInput = document.createElement("input");
    bdussInput.type = "text";
    bdussInput.placeholder = "粘贴 BDUSS 值";
    bdussInput.style.cssText = "width:100%;padding:6px;margin:4px 0;box-sizing:border-box;";

    const stokenLabel = document.createElement("div");
    stokenLabel.textContent = "STOKEN:";
    stokenLabel.style.cssText = "margin-top:6px;font-weight:500;";
    const stokenInput = document.createElement("input");
    stokenInput.type = "text";
    stokenInput.placeholder = "粘贴 STOKEN 值";
    stokenInput.style.cssText = "width:100%;padding:6px;margin:4px 0;box-sizing:border-box;";

    const hint = document.createElement("div");
    hint.innerHTML = "📌 如何获取？<br>1. 打开F12开发者工具 → 应用程序 → Cookies → https://pan.baidu.com<br>2. 找到 BDUSS 和 STOKEN，双击值并复制";
    hint.style.cssText = "font-size:11px;color:#4b5563;margin:8px 0;line-height:1.4;";

    const exportBtn = document.createElement("button");
    exportBtn.textContent = "导出 JSON 并复制";
    exportBtn.style.cssText = "margin-top:6px;padding:6px 12px;cursor:pointer;";

    container.append(info, bdussLabel, bdussInput, stokenLabel, stokenInput, hint, exportBtn);

    exportBtn.addEventListener("click", () => {
      const BDUSS = bdussInput.value.trim();
      const STOKEN = stokenInput.value.trim();
      if (!BDUSS && !STOKEN) {
        alert("至少填写 BDUSS（通常必须）");
        return;
      }
      const json = JSON.stringify({ BDUSS, STOKEN }, null, 2);
      onExport(json);
    });
  }

  // ======================== 通用界面 ========================
  function createPanel() {
    const panel = document.createElement("div");
    panel.style.cssText = [
      "position:fixed",
      "right:18px",
      "bottom:18px",
      "z-index:2147483647",
      "width:380px",
      "max-width:calc(100vw - 36px)",
      "padding:12px",
      "border:1px solid #d1d5db",
      "border-radius:8px",
      "background:#fff",
      "box-shadow:0 10px 24px rgba(15,23,42,.16)",
      "font:13px/1.45 Arial, sans-serif",
      "color:#111827",
    ].join(";");

    const title = document.createElement("div");
    title.textContent = "Baidu Pan Cookie Exporter (BDUSS+STOKEN)";
    title.style.cssText = "font-weight:700;margin-bottom:8px;";

    const status = document.createElement("div");
    status.style.cssText = "margin-bottom:8px;color:#4b5563;";

    const textarea = document.createElement("textarea");
    textarea.readOnly = true;
    textarea.style.cssText = [
      "width:100%",
      "height:100px",
      "box-sizing:border-box",
      "resize:vertical",
      "border:1px solid #d1d5db",
      "border-radius:6px",
      "padding:8px",
      "font:12px/1.4 Consolas, monospace",
      "color:#111827",
      "background:#f9fafb",
    ].join(";");

    const row = document.createElement("div");
    row.style.cssText = "display:flex;gap:8px;margin-top:8px;";

    const refreshButton = document.createElement("button");
    refreshButton.textContent = "Refresh";
    const copyButton = document.createElement("button");
    copyButton.textContent = "Copy JSON";
    const closeButton = document.createElement("button");
    closeButton.textContent = "Close";

    for (const btn of [refreshButton, copyButton, closeButton]) {
      btn.style.cssText = [
        "border:1px solid #cbd5e1",
        "border-radius:6px",
        "background:#fff",
        "padding:6px 10px",
        "cursor:pointer",
      ].join(";");
    }

    row.append(refreshButton, copyButton, closeButton);
    panel.append(title, status, textarea, row);
    document.documentElement.appendChild(panel);

    // 手动模式容器（当自动失败时显示）
    const manualContainer = document.createElement("div");
    manualContainer.style.cssText = "margin-top:10px;border-top:1px solid #e5e7eb;padding-top:8px;";
    panel.appendChild(manualContainer);

    function copyText(text) {
      if (typeof GM_setClipboard === "function") {
        GM_setClipboard(text, "text");
      } else {
        navigator.clipboard.writeText(text);
      }
    }

    function notify(msg) {
      if (typeof GM_notification === "function") {
        GM_notification({ title: "Baidu Pan Exporter", text: msg, timeout: 2500 });
      } else {
        alert(msg);
      }
    }

    async function refresh() {
      const auto = await autoReadCookies();
      const hasAuto = auto.BDUSS && auto.STOKEN;

      if (hasAuto) {
        // 自动成功，隐藏手动模式
        manualContainer.style.display = "none";
        textarea.value = JSON.stringify({ BDUSS: auto.BDUSS, STOKEN: auto.STOKEN }, null, 2);
        status.textContent = "✅ 自动获取成功 (HttpOnly Cookie)";
        status.style.color = "#166534";
      } else {
        // 自动失败，显示手动模式
        manualContainer.style.display = "block";
        // 如果部分字段有值，预填
        if (auto.BDUSS || auto.STOKEN) {
          textarea.value = JSON.stringify({ BDUSS: auto.BDUSS, STOKEN: auto.STOKEN }, null, 2);
          status.textContent = "⚠️ 部分自动获取（可能需手动补全）";
          status.style.color = "#b45309";
        } else {
          textarea.value = "{}";
          status.textContent = "❌ 自动读取失败，请使用下方手动模式";
          status.style.color = "#b45309";
        }
        // 如果手动模式尚未构建，则构建
        if (manualContainer.children.length === 0) {
          buildManualPanel(manualContainer, (jsonStr) => {
            textarea.value = jsonStr;
            copyText(jsonStr);
            notify("已导出并复制 JSON");
            status.textContent = "✅ 手动导出成功";
            status.style.color = "#166534";
          });
        } else {
          // 已经构建，无需重复
        }
      }
    }

    refreshButton.addEventListener("click", refresh);
    copyButton.addEventListener("click", () => {
      if (textarea.value && textarea.value !== "{}") {
        copyText(textarea.value);
        notify("JSON 已复制到剪贴板");
      } else {
        alert("没有有效数据，请先 Refresh 或手动填写");
      }
    });
    closeButton.addEventListener("click", () => panel.remove());

    refresh();
  }

  // 等待页面加载完成后显示面板
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", createPanel);
  } else {
    createPanel();
  }
})();