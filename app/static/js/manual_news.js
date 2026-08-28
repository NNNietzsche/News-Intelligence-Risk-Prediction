/*
 * 手动补录与单条发送新闻。
 * 负责：日报“新闻”与主体评估“主体报道”使用独立录入界面，避免业务口径混用。
 * Modified by DingJiaye: 2026-08-28.
 */
(function () {
  "use strict";
  // 页面上下文由子模板在本脚本之后注入，点击时读取，不能在模块加载时固化。
  // Modified by DingJiaye: 2026-08-28.
  const getContext = () => window.MANUAL_NEWS_CONTEXT || {};
  const $ = (root, selector) => root?.querySelector(selector);
  const dailyDialog = document.getElementById("daily-news-dialog");
  const entityDialog = document.getElementById("entity-report-dialog");
  const open = (dialog) => { if (dialog?.showModal) dialog.showModal(); else dialog?.classList.add("is-open"); };
  const close = (dialog) => { if (dialog?.open && dialog.close) dialog.close(); dialog?.classList.remove("is-open"); };
  const say = (root, message, state = "") => { const node = $(root, ".alert-settings-feedback"); if (node) { node.textContent = message || ""; node.className = `alert-settings-feedback ${state}`.trim(); } };
  const value = (root, selector) => ($(root, selector)?.value || "").trim();

  document.querySelectorAll("[data-manual-news-open]").forEach((button) => button.addEventListener("click", () => {
    const context = getContext();
    if (context.kind === "entity") {
      $(entityDialog, "#entity-report-form")?.reset();
      $(entityDialog, "#entity-report-subject").textContent = window.ENTITY_NAME || "当前监测主体";
      say(entityDialog, ""); open(entityDialog);
    } else {
      $(dailyDialog, "#daily-news-form")?.reset(); say(dailyDialog, ""); open(dailyDialog);
    }
  }));
  document.querySelectorAll("[data-manual-close='daily']").forEach((button) => button.addEventListener("click", () => close(dailyDialog)));
  document.querySelectorAll("[data-manual-close='entity']").forEach((button) => button.addEventListener("click", () => close(entityDialog)));
  [dailyDialog, entityDialog].forEach((dialog) => dialog?.addEventListener("click", (event) => { if (event.target === dialog) close(dialog); }));

  $(dailyDialog, "#daily-news-form")?.addEventListener("submit", async (event) => {
    const context = getContext();
    event.preventDefault(); const submit = $(dailyDialog, "#daily-news-submit"); const published = value(dailyDialog, "#daily-news-published-at");
    const entry = { "标题": value(dailyDialog, "#daily-news-title"), "核心摘要": value(dailyDialog, "#daily-news-summary"), "来源名称": value(dailyDialog, "#daily-news-source-name"), "来源链接": value(dailyDialog, "#daily-news-source-url"), "风险类别": value(dailyDialog, "#daily-news-category"), "风险等级": ({ "低": "低", "关注": "中", "风险": "高" })[value(dailyDialog, "#daily-news-signal")], "发布时间": published ? new Date(published).toISOString() : "" };
    try {
      submit.disabled = true; say(dailyDialog, "正在保存新闻…");
      const response = await fetch("/api/v1/entries/manual", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ module_code: value(dailyDialog, "#daily-news-module"), report_date: context.reportDate || new Date().toISOString().slice(0, 10), entries: [entry] }) });
      const payload = await response.json().catch(() => ({})); if (!response.ok) throw new Error(payload.detail || "保存失败");
      say(dailyDialog, payload.message || "新闻已保存。", "success"); window.setTimeout(() => window.location.reload(), 450);
    } catch (error) { say(dailyDialog, error.message || "保存失败", "error"); } finally { submit.disabled = false; }
  });

  $(entityDialog, "#entity-report-form")?.addEventListener("submit", async (event) => {
    const context = getContext();
    event.preventDefault(); const submit = $(entityDialog, "#entity-report-submit"); const published = value(entityDialog, "#entity-report-published-at");
    const payload = { title: value(entityDialog, "#entity-report-title"), summary: value(entityDialog, "#entity-report-summary"), source_name: value(entityDialog, "#entity-report-source-name"), source_url: value(entityDialog, "#entity-report-source-url"), risk_category: value(entityDialog, "#entity-report-category"), risk_signal: value(entityDialog, "#entity-report-signal"), published_at: published ? new Date(published).toISOString() : null };
    try {
      if (!context.entityId) throw new Error("请先在索引中选择主体。");
      submit.disabled = true; say(entityDialog, "正在将报道关联至当前主体…");
      const response = await fetch(`/api/v1/entities/${context.entityId}/risks/manual`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
      const data = await response.json().catch(() => ({})); if (!response.ok) throw new Error(data.detail || "保存失败");
      say(entityDialog, data.message || "主体报道已保存。", "success"); window.setTimeout(() => window.location.reload(), 450);
    } catch (error) { say(entityDialog, error.message || "保存失败", "error"); } finally { submit.disabled = false; }
  });

  const selectedIds = (selector) => Array.from(document.querySelectorAll(`${selector}:checked`)).map((input) => Number(input.value));
  function bindSelection(selector, toolbarId, countId, buttonId, endpoint) {
    const toolbar = document.getElementById(toolbarId); const count = document.getElementById(countId); const send = document.getElementById(buttonId);
    if (!toolbar || !send) return;
    const refresh = () => { const ids = selectedIds(selector); const context = getContext(); toolbar.hidden = !ids.length; count.textContent = `已选择 ${ids.length} 条${context.kind === "entity" ? "报道" : "新闻"}`; };
    document.querySelectorAll(selector).forEach((input) => input.addEventListener("change", refresh));
    send.addEventListener("click", async () => {
      const ids = selectedIds(selector); const context = getContext(); if (!ids.length) return; send.disabled = true; send.textContent = "正在发送…";
      try { const response = await fetch(typeof endpoint === "function" ? endpoint() : endpoint, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ids }) }); const payload = await response.json().catch(() => ({})); if (!response.ok) throw new Error(payload.detail || "发送失败"); send.textContent = payload.message || "发送完成"; } catch (error) { send.textContent = error.message || "发送失败"; }
      window.setTimeout(() => { send.disabled = false; send.textContent = context.kind === "entity" ? "发送选中报道" : "发送选中新闻"; }, 2400);
    });
  }
  bindSelection(".daily-news-select", "daily-news-selection-toolbar", "daily-news-selection-count", "daily-news-send-selected", "/api/v1/entries/send");
  bindSelection(".entity-news-select", "entity-news-selection-toolbar", "entity-news-selection-count", "entity-news-send-selected", () => `/api/v1/entities/${getContext().entityId}/risks/send`);
}());
