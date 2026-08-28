/*
 * 外部预警通知设置面板。
 * 负责：读取与保存企业微信、邮件通知设置及关联企业筛选；敏感值不从接口回填。
 * Modified by DingJiaye: 2026-08-27.
 */
(function () {
  "use strict";

  const dialog = document.getElementById("alert-settings-dialog");
  const opener = document.getElementById("alert-settings-open");
  const closer = document.getElementById("alert-settings-close");
  const feedback = document.getElementById("alert-settings-feedback");
  if (!dialog || !opener) return;

  const $ = (selector) => dialog.querySelector(selector);
  const put = async (path, data) => {
    const response = await fetch(`/api/v1${path}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.detail || "保存失败，请稍后重试。");
    return payload;
  };
  const say = (message, type = "") => {
    feedback.textContent = message || "";
    feedback.className = `alert-settings-feedback ${type}`.trim();
  };
  const bool = (id, value) => { const node = $(`#${id}`); if (node) node.checked = Boolean(value); };
  const value = (id, next) => { const node = $(`#${id}`); if (node) node.value = next ?? ""; };

  function applySettings(payload) {
    const policy = payload.policy || {};
    bool("alert-high-risk-news", policy.high_risk_news);
    const selectedIds = new Set((policy.entity_ids || []).map(String));
    const entitySelect = $("#alert-entity-select");
    if (entitySelect) {
      entitySelect.replaceChildren(...(payload.entities || []).map((entity) => {
        const label = document.createElement("label");
        label.className = "alert-entity-choice";
        const input = document.createElement("input");
        input.type = "checkbox";
        input.value = entity.id;
        input.checked = selectedIds.has(String(entity.id));
        const text = document.createElement("span");
        text.textContent = entity.industry ? `${entity.name} · ${entity.industry}` : entity.name;
        label.append(input, text);
        return label;
      }));
    }
    const channels = payload.channels || {};
    const email = channels.email || {};
    const emailConfig = email.config || {};
    bool("email-enabled", email.enabled);
    value("email-to", emailConfig.alert_email_to);
    value("smtp-host", emailConfig.smtp_host);
    value("smtp-port", emailConfig.smtp_port);
    value("smtp-username", emailConfig.smtp_username);
    value("smtp-from", emailConfig.smtp_from);
    bool("smtp-tls", emailConfig.smtp_use_starttls !== false);
    value("smtp-password", "");
    $("#smtp-password").placeholder = email.secret_configured ? "已保存；留空保持不变" : "输入 SMTP 密码";
    const wecom = channels.wecom || {};
    bool("wecom-enabled", wecom.enabled);
    value("wecom-webhook", "");
    $("#wecom-webhook").placeholder = wecom.secret_configured ? "已保存；留空保持不变" : "粘贴 Webhook 地址";
  }

  async function loadSettings() {
    say("正在读取设置…");
    const response = await fetch("/api/v1/alert-settings");
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.detail || "无法读取设置。");
    applySettings(payload);
    say("");
  }

  function open() {
    if (typeof dialog.showModal === "function") dialog.showModal();
    else dialog.classList.add("is-open");
    loadSettings().catch((error) => say(error.message, "error"));
  }
  function close() {
    if (typeof dialog.close === "function" && dialog.open) dialog.close();
    dialog.classList.remove("is-open");
  }

  opener.addEventListener("click", open);
  closer?.addEventListener("click", close);
  dialog.addEventListener("click", (event) => { if (event.target === dialog) close(); });
  document.addEventListener("keydown", (event) => { if (event.key === "Escape") close(); });

  dialog.querySelectorAll("[data-alert-tab]").forEach((tab) => tab.addEventListener("click", () => {
    const current = tab.dataset.alertTab;
    dialog.querySelectorAll("[data-alert-tab]").forEach((node) => {
      const active = node === tab;
      node.classList.toggle("active", active);
      node.setAttribute("aria-selected", String(active));
    });
    dialog.querySelectorAll("[data-alert-panel]").forEach((panel) => {
      const active = panel.dataset.alertPanel === current;
      panel.hidden = !active;
      panel.classList.toggle("active", active);
    });
  }));

  $("#alert-policy-save")?.addEventListener("click", async () => {
    const entityIds = Array.from($("#alert-entity-select").querySelectorAll("input:checked")).map((input) => Number(input.value));
    try {
      await put("/alert-settings/policy", { high_risk_news: $("#alert-high-risk-news").checked, entity_ids: entityIds });
      say("推送规则已保存。", "success");
    } catch (error) { say(error.message, "error"); }
  });

  $("#alert-daily-brief-send")?.addEventListener("click", async (event) => {
    const button = event.currentTarget;
    try {
      button.disabled = true;
      const date = window.REPORT_DATE ? `?report_date=${encodeURIComponent(window.REPORT_DATE)}` : "";
      const response = await fetch(`/api/v1/alert-settings/daily-brief/send${date}`, { method: "POST" });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(payload.detail || "简报发送失败，请稍后重试。");
      say(payload.message || "今日风险情报概览已发送。", "success");
    } catch (error) { say(error.message, "error"); }
    finally { button.disabled = false; }
  });

  dialog.querySelectorAll("[data-alert-save]").forEach((button) => button.addEventListener("click", async () => {
    const channel = button.dataset.alertSave;
    let config;
    if (channel === "email") {
      config = {
        alert_email_to: $("#email-to").value.trim(), smtp_host: $("#smtp-host").value.trim(),
        smtp_port: Number($("#smtp-port").value || 587), smtp_username: $("#smtp-username").value.trim(),
        smtp_from: $("#smtp-from").value.trim(), smtp_password: $("#smtp-password").value,
        smtp_use_starttls: $("#smtp-tls").checked,
      };
    } else {
      config = { webhook_url: $(`#${channel}-webhook`).value.trim() };
    }
    try {
      button.disabled = true;
      await put(`/alert-settings/${channel}`, { enabled: $(`#${channel}-enabled`).checked, config });
      await loadSettings();
      say(`${channel === "wecom" ? "企业微信" : "邮件"}配置已保存。`, "success");
    } catch (error) { say(error.message, "error"); }
    finally { button.disabled = false; }
  }));
}());
