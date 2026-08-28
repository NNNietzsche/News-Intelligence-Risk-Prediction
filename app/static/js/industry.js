(function () {
  var LAST_SECTOR_KEY = "industry-last-sector";

  function apiError(detail, fallback) {
    if (!detail) return fallback;
    if (typeof detail === "string") return detail;
    return [detail.code, detail.message, detail.next_step ? "下一步：" + detail.next_step : ""]
      .filter(Boolean).join(" · ");
  }

  async function readApiJson(resp) {
    var text = await resp.text();
    if (!text) return {};
    try {
      return JSON.parse(text);
    } catch (e) {
      var snippet = String(text).replace(/\s+/g, " ").trim().slice(0, 160);
      throw new Error((resp.status ? resp.status + " " : "") + (snippet || "响应不是有效 JSON"));
    }
  }

  var bootstrap = window.INDUSTRY_BOOTSTRAP || {};
  if (bootstrap.redirect && Array.isArray(bootstrap.keys) && bootstrap.keys.length) {
    var last = "";
    try { last = window.localStorage.getItem(LAST_SECTOR_KEY) || ""; } catch (e) {}
    var target = (last && bootstrap.keys.indexOf(last) >= 0) ? last : bootstrap.keys[0];
    window.location.replace("/deep-reports/" + encodeURIComponent(target));
    return;
  }

  function currentSector() {
    var rail = document.querySelector(".page-rail[data-sector-key]");
    return rail ? (rail.getAttribute("data-sector-key") || "") : "";
  }

  function sectorSelected() {
    var rail = document.querySelector(".page-rail[data-sector-selected]");
    if (rail) return rail.getAttribute("data-sector-selected") === "1";
    return !!currentSector();
  }

  function rememberSector(sectorKey) {
    if (!sectorKey) return;
    try { window.localStorage.setItem(LAST_SECTOR_KEY, sectorKey); } catch (e) {}
  }

  rememberSector(currentSector());

  function sectorBasePath() {
    var sector = currentSector();
    return sector ? "/deep-reports/" + encodeURIComponent(sector) : "/deep-reports";
  }

  function industryApiHeaders(extra) {
    var headers = Object.assign({}, extra || {});
    if (!headers["X-Industry-Sector"]) {
      var sector = currentSector();
      if (sector) headers["X-Industry-Sector"] = sector;
    }
    return headers;
  }

  function industryFetch(url, options) {
    options = options || {};
    var headers = industryApiHeaders(options.headers || {});
    if (options.sector) {
      headers["X-Industry-Sector"] = options.sector;
    }
    if (options.body && !headers["Content-Type"]) {
      headers["Content-Type"] = "application/json";
    }
    return fetch(url, Object.assign({}, options, { headers: headers }));
  }

  const form = document.getElementById("analysis-form");

  function setMainStatus(text, isError) {
    const el = document.getElementById("industry-main-status");
    if (!el) return;
    if (!text) {
      el.hidden = true;
      el.textContent = "";
      el.classList.remove("is-error");
      return;
    }
    el.hidden = false;
    el.textContent = text;
    el.classList.toggle("is-error", !!isError);
  }

  if (form) {
    form.addEventListener("submit", async function (ev) {
      ev.preventDefault();
      if (!sectorSelected()) {
        setMainStatus("请先选择或新建行业", true);
        return;
      }
      const submitBtn = document.getElementById("btn-create-report-draft")
        || form.querySelector('button[type="submit"]');
      const fd = new FormData(form);
      const rail = document.querySelector(".page-rail[data-default-industry-name]");
      const industryName = ((rail && rail.getAttribute("data-default-industry-name")) || "").trim();
      if (!industryName) {
        setMainStatus("当前行业缺少默认名称，请重新选择行业", true);
        return;
      }
      const reportName = String(fd.get("report_name") || "").trim().replace(/\s+/g, " ");
      if (!reportName) {
        setMainStatus("请先为新文档填写名称。", true);
        var nameInput = document.getElementById("report-name-input");
        if (nameInput) nameInput.focus();
        return;
      }
      if (reportName.length > 256) {
        setMainStatus("报告名称不能超过 256 个字符", true);
        return;
      }
      const body = {
        industry_name: industryName,
        company_name: industryName,
        // Generation is deliberately source-led: the user selects sources in
        // step 3 instead of receiving unreviewed search results automatically.
        supplement_search: false,
      };
      if (submitBtn) submitBtn.disabled = true;
      setMainStatus("正在创建报告草稿…");
      try {
        const resp = await industryFetch("/api/v1/industry/reports/drafts", {
          method: "POST",
          body: JSON.stringify(body),
        });
        const data = await readApiJson(resp);
        if (!resp.ok) throw new Error(apiError(data.detail, "创建草稿失败"));
        if (reportName) {
          const renameResp = await industryFetch("/api/v1/industry/reports/" + data.id + "/name", {
            method: "PATCH",
            body: JSON.stringify({ report_name: reportName }),
          });
          const renameData = await readApiJson(renameResp);
          if (!renameResp.ok) throw new Error(apiError(renameData.detail, "设置报告名称失败"));
        }
        setMainStatus("文档已创建。请在右侧添加并勾选数据源，再开始 AI 生成。");
        window.location.href = sectorBasePath() + "?report_id=" + data.id;
      } catch (e) {
        if (submitBtn) submitBtn.disabled = false;
        setMainStatus("错误：" + e.message, true);
      }
    });
  }

  const addReportBtn = document.getElementById("btn-add-report");
  const reportModal = document.getElementById("report-create-modal");
  const reportNameInput = document.getElementById("report-name-input");
  const reportCloseBtn = document.getElementById("btn-close-report-create");
  const reportCancelBtn = document.getElementById("btn-cancel-report-create");

  function openReportModal() {
    if (!reportModal) return;
    reportModal.classList.remove("hidden");
    reportModal.hidden = false;
    if (reportNameInput) {
      window.setTimeout(function () { reportNameInput.focus(); reportNameInput.select(); }, 0);
    }
  }

  function closeReportModal() {
    if (!reportModal) return;
    reportModal.classList.add("hidden");
    reportModal.hidden = true;
  }

  if (addReportBtn) addReportBtn.addEventListener("click", openReportModal);
  document.querySelectorAll("[data-open-report-modal]").forEach(function (button) {
    button.addEventListener("click", openReportModal);
  });
  if (reportCloseBtn) reportCloseBtn.addEventListener("click", closeReportModal);
  if (reportCancelBtn) reportCancelBtn.addEventListener("click", closeReportModal);
  if (reportModal) reportModal.addEventListener("click", function (event) {
    if (event.target === reportModal) closeReportModal();
  });
  if (reportNameInput) reportNameInput.addEventListener("keydown", function (event) {
    if (event.key === "Escape") closeReportModal();
  });

  const generateBtn = document.getElementById("btn-generate-report");
  if (generateBtn) {
    function pollIndustryAgent(jobId, reportId, demoFixed) {
      var attempts = 0;
      var maxAttempts = 600; // 最长约 10 分钟，避免无止境轮询。
      var timer = window.setInterval(async function () {
        attempts += 1;
        try {
          var resp = await industryFetch("/api/v1/industry/agent-jobs/" + encodeURIComponent(jobId));
          var job = await readApiJson(resp);
          if (!resp.ok) throw new Error(apiError(job.detail, "无法读取智能体状态"));
          setMainStatus("智能体：" + (job.stage || "处理中") + "（" + (job.progress || 0) + "%）");
          if (job.status === "completed") {
            window.clearInterval(timer);
            var targetId = job.result_report_id || reportId;
            window.location.href = sectorBasePath() + "?report_id=" + encodeURIComponent(targetId);
          } else if (job.status === "failed") {
            window.clearInterval(timer);
            generateBtn.disabled = false;
            setMainStatus("生成失败：" + (job.error || "请稍后重试"), true);
          } else if (attempts >= maxAttempts) {
            window.clearInterval(timer);
            generateBtn.disabled = false;
            setMainStatus("任务仍在后台运行，可稍后打开该报告查看结果。", false);
          }
        } catch (error) {
          window.clearInterval(timer);
          generateBtn.disabled = false;
          setMainStatus("读取智能体状态失败：" + (error.message || error), true);
        }
      }, demoFixed ? 250 : 900);
    }

    generateBtn.addEventListener("click", async function () {
      const reportId = generateBtn.getAttribute("data-report-id");
      if (!reportId) return;
      const selectedSources = document.querySelectorAll(".source-select-checkbox:checked");
      const sourceDrawer = document.getElementById("source-drawer");
      // Modified by DingJiaye: 2026-08-26 — 航空、能源/电力演示样稿固定使用
      // 已核验材料，允许不选择资料直接生成；其他行业仍必须选择正文来源。
      const demoFixed = document.querySelector(".industry-content")?.getAttribute("data-demo-fixed") === "1";
      if (sourceDrawer && !selectedSources.length && !demoFixed) {
        setMainStatus("请先在右侧勾选至少一条含正文的数据源。", true);
        sourceDrawer.scrollIntoView({ behavior: "smooth", block: "start" });
        return;
      }
      const generationMsg = document.getElementById("generation-msg");
      generateBtn.disabled = true;
      setMainStatus(demoFixed ? "智能体正在载入演示报告…" : "智能体已接收任务，页面仍可继续操作。");
      if (generationMsg) generationMsg.textContent = "";
      try {
        const sector = currentSector();
        const resp = await industryFetch(
          "/api/v1/industry/reports/" + reportId + "/agent-run?sector=" + encodeURIComponent(sector),
          { method: "POST" }
        );
        const data = await readApiJson(resp);
        if (!resp.ok) throw new Error(apiError(data.detail, "生成失败"));
        pollIndustryAgent(data.job_id, reportId, demoFixed);
      } catch (e) {
        generateBtn.disabled = false;
        setMainStatus("错误：" + e.message, true);
        if (generationMsg) generationMsg.textContent = "错误：" + e.message;
      }
    });
  }

  // Modified by DingJiaye: 2026-08-27 — 页面刷新后继续轻量轮询后台智能体，
  // 不阻塞用户切换行业、添加资料等操作。
  const runningReport = document.querySelector(".industry-agent-running[data-agent-report-id]");
  if (runningReport) {
    const runningReportId = runningReport.getAttribute("data-agent-report-id");
    const runningMessage = document.getElementById("industry-running-message");
    let runningAttempts = 0;
    const runningTimer = window.setInterval(async function () {
      runningAttempts += 1;
      try {
        const sector = currentSector();
        const response = await industryFetch(
          "/api/v1/industry/reports/" + encodeURIComponent(runningReportId) + "?sector=" + encodeURIComponent(sector)
        );
        const report = await readApiJson(response);
        if (!response.ok) throw new Error(apiError(report.detail, "无法读取报告状态"));
        if (report.status === "completed" || report.status === "awaiting_approval" || report.status === "failed") {
          window.clearInterval(runningTimer);
          window.location.reload();
        } else if (runningMessage && runningAttempts % 5 === 0) {
          runningMessage.textContent = "仍在后台生成，您可继续使用其它功能；报告完成后会自动显示。";
        }
      } catch (error) {
        // 短暂网络失败不打断当前工作区；后续轮询会自动恢复。
        if (runningAttempts >= 20) window.clearInterval(runningTimer);
      }
      if (runningAttempts >= 300) window.clearInterval(runningTimer);
    }, 1500);
  }

  const revisionForm = document.getElementById("report-revision-form");
  if (revisionForm) {
    const revisionChat = document.querySelector(".report-revision-chat");
    const content = document.querySelector(".industry-content");
    const mainPanel = document.querySelector(".app-main");
    function positionRevisionChat() {
      if (!revisionChat || !mainPanel) return;
      const bounds = mainPanel.getBoundingClientRect();
      revisionChat.style.setProperty("--revision-left", Math.max(12, bounds.left + 20) + "px");
      revisionChat.style.setProperty("--revision-right", Math.max(12, window.innerWidth - bounds.right + 20) + "px");
    }
    if (content) content.classList.add("has-floating-revision-chat");
    positionRevisionChat();
    window.addEventListener("resize", positionRevisionChat);
    if (window.ResizeObserver && mainPanel) {
      new ResizeObserver(positionRevisionChat).observe(mainPanel);
    }
    const revisionInput = document.getElementById("report-revision-input");
    if (revisionInput) revisionInput.addEventListener("keydown", function (event) {
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        revisionForm.requestSubmit();
      }
    });
    revisionForm.addEventListener("submit", async function (event) {
      event.preventDefault();
      const input = document.getElementById("report-revision-input");
      const submit = document.getElementById("btn-revise-report");
      const message = document.getElementById("report-revision-msg");
      const reportId = revisionForm.getAttribute("data-report-id");
      const instruction = String(input ? input.value : "").trim();
      if (instruction.length < 2) {
        if (message) message.textContent = "请写明需要调整的内容。";
        if (input) input.focus();
        return;
      }
      if (submit) submit.disabled = true;
      if (message) message.textContent = "DeepSeek 正在生成修订版本，请稍候…";
      try {
        const response = await industryFetch("/api/v1/industry/reports/" + reportId + "/revise", {
          method: "POST",
          body: JSON.stringify({ instruction: instruction }),
        });
        const data = await readApiJson(response);
        if (!response.ok) throw new Error(apiError(data.detail, "修改报告失败"));
        window.location.href = sectorBasePath() + "?report_id=" + data.id;
      } catch (error) {
        if (submit) submit.disabled = false;
        if (message) message.textContent = "修改失败：" + error.message;
      }
    });
  }

  const focusSourcesBtn = document.getElementById("btn-focus-data-sources");
  if (focusSourcesBtn) {
    focusSourcesBtn.addEventListener("click", function () {
      const drawer = document.getElementById("source-drawer");
      if (!drawer) return;
      document.body.classList.remove("sources-collapsed");
      const addModal = document.getElementById("source-add-modal");
      if (addModal) {
        addModal.hidden = false;
        addModal.classList.remove("hidden");
      }
      const addBtn = document.getElementById("btn-toggle-add-sources");
      if (addBtn) addBtn.focus();
    });
  }

  const promoteBtn = document.getElementById("btn-promote-grounded");
  if (promoteBtn) {
    promoteBtn.addEventListener("click", async function () {
      const noteInput = document.getElementById("promotion-note");
      const promotionMsg = document.getElementById("promotion-msg");
      const note = noteInput ? noteInput.value.trim() : "";
      if (!note) {
        if (promotionMsg) promotionMsg.textContent = "请先填写审批备注。";
        if (noteInput) noteInput.focus();
        return;
      }
      promoteBtn.disabled = true;
      if (promotionMsg) promotionMsg.textContent = "正在复核证据快照并晋升候选…";
      try {
        const reportId = promoteBtn.getAttribute("data-report-id");
        const runId = promoteBtn.getAttribute("data-run-id");
        const resp = await industryFetch(
          "/api/v1/industry/reports/" + reportId + "/grounded-runs/" + runId + "/promote",
          {
            method: "POST",
            body: JSON.stringify({ promotion_note: note }),
          }
        );
        const data = await readApiJson(resp);
        if (!resp.ok) throw new Error(apiError(data.detail, "晋升失败"));
        window.location.href = sectorBasePath() + "?report_id=" + data.id;
      } catch (e) {
        promoteBtn.disabled = false;
        if (promotionMsg) promotionMsg.textContent = "错误：" + e.message;
      }
    });
  }

  function addDetailRow(host, label, value) {
    if (value === null || value === undefined || value === "") return;
    const row = document.createElement("div");
    row.className = "citation-detail-row";
    const term = document.createElement("strong");
    term.textContent = label;
    const content = document.createElement("span");
    content.textContent = String(value);
    row.append(term, content);
    host.appendChild(row);
  }

  function addList(host, title, items, formatter) {
    if (!Array.isArray(items) || !items.length) return;
    const heading = document.createElement("h3");
    heading.textContent = title;
    const list = document.createElement("ul");
    items.forEach(function (item) {
      const li = document.createElement("li");
      li.textContent = formatter ? formatter(item) : String(item);
      list.appendChild(li);
    });
    host.append(heading, list);
  }

  function normalizeExternalUrl(url) {
    var value = String(url || "").trim();
    if (!value) return "";
    if (/^https?:\/\//i.test(value)) return value;
    if (/^\/\//.test(value)) return "https:" + value;
    return "https://" + value;
  }

  const citationDialog = document.getElementById("citation-dialog");
  const citationDetail = document.getElementById("citation-detail");
  const reportHost = document.querySelector(".industry-content[data-report-id]");
  if (citationDialog && citationDetail && reportHost) {
    const closeButton = citationDialog.querySelector(".citation-close");
    if (closeButton) closeButton.addEventListener("click", function () { citationDialog.close(); });
    citationDialog.addEventListener("click", function (event) {
      if (event.target === citationDialog) citationDialog.close();
    });
    document.addEventListener("click", async function (event) {
      const button = event.target.closest(".citation-ref");
      if (!button) return;
      event.preventDefault();
      const reportId = reportHost.getAttribute("data-report-id");
      const status = reportHost.getAttribute("data-report-status");
      const runId = reportHost.getAttribute("data-run-id");
      const code = button.getAttribute("data-evidence-code");
      let url = "/api/v1/industry/reports/" + reportId + "/citations/" + encodeURIComponent(code);
      if (status === "awaiting_approval" && runId) {
        url = "/api/v1/industry/reports/" + reportId + "/grounded-runs/" + runId
          + "/citations/" + encodeURIComponent(code);
      }
      citationDetail.replaceChildren();
      const loading = document.createElement("p");
      loading.textContent = "正在读取证据详情…";
      citationDetail.appendChild(loading);
      citationDialog.showModal();
      try {
        const resp = await industryFetch(url);
        const data = await readApiJson(resp);
        if (!resp.ok) throw new Error(apiError(data.detail, "证据详情不可用"));
        citationDetail.replaceChildren();
        addDetailRow(citationDetail, "引用编号", "[" + data.display_number + "]");
        addDetailRow(citationDetail, "来源", data.source_name);
        addDetailRow(citationDetail, "发布机构", data.source_publisher);
        addDetailRow(citationDetail, "发布日期", data.published_at);
        addDetailRow(citationDetail, "获取时间", data.retrieved_at);
        addDetailRow(citationDetail, "来源类型", data.source_origin);
        addDetailRow(citationDetail, "证据等级", data.evidence_grade);
        addDetailRow(citationDetail, "原文位置", data.locator);
        addDetailRow(citationDetail, "原文摘录", data.original_quote);
        addDetailRow(citationDetail, "标准化事实", data.normalized_claim);
        if (data.url) {
          const linkRow = document.createElement("div");
          linkRow.className = "citation-detail-row";
          const label = document.createElement("strong");
          label.textContent = "网页地址";
          const link = document.createElement("a");
          const href = normalizeExternalUrl(data.url);
          link.href = href;
          link.target = "_blank";
          link.rel = "noopener noreferrer";
          link.textContent = data.url;
          linkRow.append(label, link);
          citationDetail.appendChild(linkRow);
        }
        addList(citationDetail, "资料限制", data.limitations);
        addList(citationDetail, "相关冲突", data.related_conflicts, function (item) {
          return [item.conflict_code, item.status, item.description].filter(Boolean).join(" · ");
        });
      } catch (e) {
        citationDetail.replaceChildren();
        const error = document.createElement("p");
        error.className = "citation-warning";
        error.textContent = "错误：" + e.message;
        citationDetail.appendChild(error);
      }
    });
  }

  document.querySelectorAll(".report-rename-btn").forEach(function (button) {
    button.addEventListener("click", async function (event) {
      event.preventDefault();
      event.stopPropagation();
      const reportId = button.getAttribute("data-report-id");
      const reportSector = button.getAttribute("data-sector-key") || currentSector();
      const currentName = button.getAttribute("data-report-name") || button.getAttribute("data-default-name") || "";
      const reportName = window.prompt("请输入新的报告名称（最多 256 个字符）：", currentName);
      if (reportName === null) return;
      const normalized = reportName.trim().replace(/\s+/g, " ");
      if (!normalized) {
        window.alert("报告名称不能为空");
        return;
      }
      if (normalized.length > 256) {
        window.alert("报告名称不能超过 256 个字符");
        return;
      }
      button.disabled = true;
      try {
        const resp = await industryFetch("/api/v1/industry/reports/" + reportId + "/name", {
          method: "PATCH",
          sector: reportSector,
          body: JSON.stringify({ report_name: normalized }),
        });
        const data = await readApiJson(resp);
        if (!resp.ok) throw new Error(apiError(data.detail, "修改名称失败"));
        window.location.reload();
      } catch (e) {
        button.disabled = false;
        window.alert("错误：" + e.message);
      }
    });
  });

  document.querySelectorAll(".report-delete-btn").forEach(function (button) {
    button.addEventListener("click", async function (event) {
      event.preventDefault();
      event.stopPropagation();
      const reportId = button.getAttribute("data-report-id");
      const reportSector = button.getAttribute("data-sector-key") || currentSector();
      if (!window.confirm("确认删除这份历史报告及其数据源、证据记录吗？此操作不可撤销。")) return;
      button.disabled = true;
      try {
        const resp = await industryFetch("/api/v1/industry/reports/" + reportId, {
          method: "DELETE",
          sector: reportSector,
        });
        const data = await readApiJson(resp);
        if (!resp.ok) throw new Error(apiError(data.detail, "删除报告失败"));
        window.location.href = sectorBasePath();
      } catch (e) {
        button.disabled = false;
        window.alert(e.message);
      }
    });
  });

  document.querySelectorAll(".sector-rename-btn").forEach(function (button) {
    button.addEventListener("click", async function (event) {
      event.preventDefault();
      event.stopPropagation();
      const sectorKey = button.getAttribute("data-sector-key");
      const currentLabel = button.getAttribute("data-sector-label") || "";
      const label = window.prompt("请输入新的行业名称：", currentLabel);
      if (label === null) return;
      const trimmed = String(label).trim();
      if (!trimmed) {
        window.alert("行业名称不能为空");
        return;
      }
      button.disabled = true;
      try {
        const resp = await fetch("/api/v1/industry/sectors/" + encodeURIComponent(sectorKey), {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ label: trimmed }),
        });
        const data = await readApiJson(resp);
        if (!resp.ok) throw new Error(apiError(data.detail, "重命名失败"));
        window.location.reload();
      } catch (e) {
        button.disabled = false;
        window.alert(e.message);
      }
    });
  });

  document.querySelectorAll(".sector-delete-btn").forEach(function (button) {
    button.addEventListener("click", async function (event) {
      event.preventDefault();
      event.stopPropagation();
      const sectorKey = button.getAttribute("data-sector-key");
      const sectorLabel = button.getAttribute("data-sector-label") || sectorKey;
      if (!window.confirm("确认删除行业「" + sectorLabel + "」？将永久删除该行业数据库及其中全部报告，且不可恢复。")) {
        return;
      }
      button.disabled = true;
      try {
        const resp = await fetch("/api/v1/industry/sectors/" + encodeURIComponent(sectorKey), {
          method: "DELETE",
        });
        const data = await readApiJson(resp);
        if (!resp.ok) throw new Error(apiError(data.detail, "删除行业失败"));
        if (data.next_sector_key) {
          window.location.href = "/deep-reports/" + encodeURIComponent(data.next_sector_key);
        } else {
          window.location.href = "/deep-reports";
        }
      } catch (e) {
        button.disabled = false;
        window.alert(e.message);
      }
    });
  });

  const addSectorBtn = document.getElementById("btn-add-sector");
  const sectorModal = document.getElementById("sector-create-modal");
  const sectorNameInput = document.getElementById("sector-create-name");
  const sectorCreateMsg = document.getElementById("sector-create-msg");
  const sectorConfirmBtn = document.getElementById("btn-confirm-sector-create");
  const sectorCancelBtn = document.getElementById("btn-cancel-sector-create");
  const sectorCloseBtn = document.getElementById("btn-close-sector-create");

  function openSectorModal() {
    if (!sectorModal) return;
    sectorModal.classList.remove("hidden");
    sectorModal.hidden = false;
    if (sectorCreateMsg) sectorCreateMsg.textContent = "";
    if (sectorNameInput) {
      sectorNameInput.value = "";
      setTimeout(function () { sectorNameInput.focus(); }, 0);
    }
  }

  function closeSectorModal() {
    if (!sectorModal) return;
    sectorModal.classList.add("hidden");
    sectorModal.hidden = true;
    if (sectorCreateMsg) sectorCreateMsg.textContent = "";
    if (addSectorBtn) addSectorBtn.disabled = false;
  }

  async function submitNewSector() {
    if (!sectorNameInput) return;
    const trimmed = String(sectorNameInput.value || "").trim();
    if (!trimmed) {
      if (sectorCreateMsg) sectorCreateMsg.textContent = "行业名称不能为空";
      return;
    }
    if (sectorConfirmBtn) sectorConfirmBtn.disabled = true;
    if (sectorCreateMsg) sectorCreateMsg.textContent = "正在创建…";
    try {
      const resp = await fetch("/api/v1/industry/sectors", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ label: trimmed }),
      });
      const data = await readApiJson(resp);
      if (!resp.ok) throw new Error(apiError(data.detail, "添加行业失败"));
      rememberSector(data.key);
      window.location.href = "/deep-reports/" + encodeURIComponent(data.key);
    } catch (e) {
      if (sectorConfirmBtn) sectorConfirmBtn.disabled = false;
      if (sectorCreateMsg) sectorCreateMsg.textContent = e.message;
    }
  }

  if (addSectorBtn) {
    addSectorBtn.addEventListener("click", function () {
      openSectorModal();
    });
  }
  if (sectorConfirmBtn) {
    sectorConfirmBtn.addEventListener("click", function () { submitNewSector(); });
  }
  if (sectorCancelBtn) {
    sectorCancelBtn.addEventListener("click", closeSectorModal);
  }
  if (sectorCloseBtn) {
    sectorCloseBtn.addEventListener("click", closeSectorModal);
  }
  if (sectorModal) {
    sectorModal.addEventListener("click", function (ev) {
      if (ev.target === sectorModal) closeSectorModal();
    });
  }
  if (sectorNameInput) {
    sectorNameInput.addEventListener("keydown", function (ev) {
      if (ev.key === "Enter") {
        ev.preventDefault();
        submitNewSector();
      } else if (ev.key === "Escape") {
        closeSectorModal();
      }
    });
  }

  const toggleBtn = document.getElementById("btn-toggle-sectors");
  const indexBlock = document.querySelector(".industry-index-block");
  if (toggleBtn && indexBlock) {
    var collapsedStored = false;
    try {
      collapsedStored = window.localStorage.getItem("industry-index-collapsed") === "1";
    } catch (e) {}
    function applyCollapsed(collapsed) {
      indexBlock.setAttribute("data-collapsed", collapsed ? "true" : "false");
      toggleBtn.classList.toggle("is-collapsed", collapsed);
      toggleBtn.setAttribute("aria-expanded", collapsed ? "false" : "true");
      try {
        window.localStorage.setItem("industry-index-collapsed", collapsed ? "1" : "0");
      } catch (e) {}
    }
    applyCollapsed(collapsedStored);
    toggleBtn.addEventListener("click", function () {
      var collapsed = indexBlock.getAttribute("data-collapsed") === "true";
      applyCollapsed(!collapsed);
    });
  }

  var industrySearch = document.getElementById("industry-sidebar-search");
  if (industrySearch) {
    industrySearch.addEventListener("input", function () {
      var needle = industrySearch.value.trim().toLowerCase();
      document.querySelectorAll(".sector-bubble-row[data-search], .report-list-item[data-search]").forEach(function (row) {
        var hay = (row.getAttribute("data-search") || "").toLowerCase();
        row.hidden = !!(needle && hay.indexOf(needle) === -1);
      });
    });
  }

  if (window.echarts && window.INDUSTRY_CHARTS) {
    const grid = document.getElementById("industry-charts");
    if (grid) {
      window.INDUSTRY_CHARTS.forEach(function (item, idx) {
        const host = document.createElement("div");
        host.style.height = "260px";
        host.style.marginTop = "1rem";
        grid.appendChild(host);
        const chart = echarts.init(host);
        const labels = item.labels || [];
        const series = (item.series || []).map(function (s) {
          return { name: s.name, type: item.type || "bar", data: s.data || [] };
        });
        chart.setOption({
          title: {
            text: item.title || ("图 " + (idx + 1)),
            left: "center",
            textStyle: { fontSize: 13, fontFamily: "Microsoft YaHei, 微软雅黑, sans-serif", fontWeight: 400 },
          },
          tooltip: { trigger: "axis" },
          xAxis: {
            type: "category",
            data: labels,
            axisLabel: { fontFamily: "Microsoft YaHei, 微软雅黑, sans-serif", fontWeight: 400 },
          },
          yAxis: {
            type: "value",
            axisLabel: { fontFamily: "Microsoft YaHei, 微软雅黑, sans-serif", fontWeight: 400 },
          },
          series: series,
        });
      });
    }
  }
})();
