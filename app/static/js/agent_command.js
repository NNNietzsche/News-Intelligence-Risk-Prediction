/*
 * InfoRisk Command Center。
 * 负责：全局指令入口、主体快速定位，以及主动风险发现面板；不伪装为客服聊天。
 * Modified by DingJiaye: 2026-08-28.
 */
(function () {
  "use strict";
  const dialog = document.getElementById("agent-command-dialog");
  const opener = document.getElementById("agent-command-open");
  const input = document.getElementById("agent-command-input");
  const result = document.getElementById("agent-command-result");
  const close = () => { if (dialog?.open) dialog.close(); };
  if (dialog && opener && input && result) {
    opener.addEventListener("click", () => { dialog.showModal?.(); window.setTimeout(() => input.focus(), 70); });
    dialog.querySelector("[data-agent-command-close]")?.addEventListener("click", close);
    dialog.addEventListener("click", (event) => { if (event.target === dialog) close(); });
    dialog.querySelectorAll("[data-agent-command]").forEach((button) => button.addEventListener("click", () => { input.value = button.dataset.agentCommand || ""; input.focus(); }));
    document.getElementById("agent-command-form")?.addEventListener("submit", async (event) => {
      event.preventDefault();
      const query = input.value.trim();
      if (!query) return;
      result.textContent = "InfoRisk Agent 正在读取已核验情报并推演风险…";
      const quickRoutes = [[/新闻|日报|情报/, "/daily-news"], [/国际评级|评级/, "/intl-ratings"], [/行业|授信/, "/deep-reports"]];
      const quick = quickRoutes.find(([pattern]) => pattern.test(query));
      if (quick) { window.location.assign(quick[1]); return; }
      try {
        const response = await fetch("/api/v1/agent/command", {
          method: "POST",
          headers: { "Content-Type": "application/json", Accept: "application/json" },
          body: JSON.stringify({ query: query }),
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.detail || "Agent 暂时不可用");
        result.textContent = data.answer || "未生成可用回答。";
        result.classList.add("has-answer");
      } catch (error) {
        result.textContent = "Agent 执行失败：" + (error && error.message ? error.message : "请稍后重试。");
        result.classList.remove("has-answer");
      }
    });
  }
  const panel = document.getElementById("agent-proactive-panel");
  const list = document.getElementById("agent-proactive-list");
  const link = document.getElementById("agent-proactive-link");
  // 子页面会在本脚本之后注入当前页风险数据，延后一帧读取以避免首屏竞态。
  // Modified by DingJiaye: 2026-08-28.
  window.setTimeout(() => {
    const findings = Array.isArray(window.AGENT_PROACTIVE_FINDINGS) ? window.AGENT_PROACTIVE_FINDINGS : [];
    if (panel && list && findings.length) {
      findings.slice(0, 2).forEach((finding) => { const item = document.createElement("li"); item.textContent = finding.title; list.append(item); });
      if (window.AGENT_PROACTIVE_URL) link.href = window.AGENT_PROACTIVE_URL;
      panel.hidden = false;
      window.setTimeout(() => panel.classList.add("is-visible"), 350);
    }
  }, 0);
  document.getElementById("agent-proactive-close")?.addEventListener("click", () => { panel?.classList.remove("is-visible"); window.setTimeout(() => { if (panel) panel.hidden = true; }, 180); });
}());
