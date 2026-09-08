(function () {
  const COLORS = ["#6b7c93", "#8a939f", "#a8956e", "#7a9a82", "#9aa3ad"];
  const chartInstances = [];
  let activeJobId = null;
  let polling = false;
  let pageLeaving = false;

  function markPageLeaving() {
    pageLeaving = true;
  }

  function isLeaveHref(anchor) {
    if (!anchor || !anchor.getAttribute) return false;
    if (anchor.target && anchor.target !== "" && anchor.target !== "_self") return false;
    var href = anchor.getAttribute("href") || "";
    if (!href || href.charAt(0) === "#" || href.indexOf("javascript:") === 0) return false;
    try {
      var dest = new URL(anchor.href, window.location.href);
      if (dest.origin !== window.location.origin) return true;
      return dest.pathname !== window.location.pathname || dest.search !== window.location.search;
    } catch (e) {
      return true;
    }
  }

  document.addEventListener("click", function (ev) {
    if (ev.defaultPrevented || ev.button !== 0) return;
    if (ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.altKey) return;
    var target = ev.target;
    var anchor = target && target.closest ? target.closest("a[href]") : null;
    if (isLeaveHref(anchor)) markPageLeaving();
  }, true);

  document.addEventListener("submit", function () {
    markPageLeaving();
  }, true);

  window.addEventListener("pagehide", markPageLeaving);

  function currentEntityId() {
    if (window.ENTITY_ID != null && window.ENTITY_ID !== "") {
      return Number(window.ENTITY_ID) || null;
    }
    var drawer = document.getElementById("source-drawer");
    var raw = drawer && drawer.getAttribute("data-entity-id");
    if (raw) return Number(raw) || null;
    return null;
  }

  function collectEntityId() {
    // Modified by DingJiaye: 2026-08-27 — 主题评估的“刷新”应只处理当前选中的
    // 主体；此前 A 模块被强制置空 entity_id，误触发 18 个主体的全量采集并造成长时间等待。
    return currentEntityId();
  }

  function currentWindowHours() {
    if (typeof window.NEWS_WINDOW_HOURS === "number" && window.NEWS_WINDOW_HOURS > 0) {
      // 产品当前只提供近 24 小时和近 7 天；旧缓存/旧页面传来更大值时
      // 收敛为 168，避免 FastAPI 在请求校验阶段返回 422。
      return Math.min(Math.floor(window.NEWS_WINDOW_HOURS), 168);
    }
    return 24;
  }

  function currentModuleCodes() {
    if (window.PIPELINE_MODULES && window.PIPELINE_MODULES.length) {
      return window.PIPELINE_MODULES.slice();
    }
    return [];
  }

  /** 与后端 job_scope 对齐：近24小时 / 7×24 / 主体各自独立 */
  function collectScopeKey() {
    var eid = collectEntityId();
    if (eid) return "entity:" + eid;
    var codes = currentModuleCodes().map(function (c) { return String(c).toUpperCase(); }).sort();
    var hours = currentWindowHours();
    if (codes.length === 1 && codes[0] === "A") return "entity:all";
    if (codes.length && codes.every(function (c) { return c === "B" || c === "C" || c === "D"; })) {
      return "news:" + hours;
    }
    if (codes.length) return "mod:" + hours + ":" + codes.join(",");
    return "news:" + hours;
  }

  function jobStorageKey() {
    return "pipeline_active_job_id:" + collectScopeKey();
  }

  function refreshMsgKey() {
    return "pipeline_collect_status_msg:" + collectScopeKey();
  }

  function refreshAtKey() {
    return "pipeline_collect_finished_at:" + collectScopeKey();
  }

  function runningQueryUrl() {
    var params = [];
    var hours = currentWindowHours();
    var eid = collectEntityId();
    var codes = currentModuleCodes();
    if (hours) params.push("window_hours=" + encodeURIComponent(hours));
    if (eid) params.push("entity_id=" + encodeURIComponent(eid));
    if (codes.length) params.push("module_codes=" + encodeURIComponent(codes.join(",")));
    return "/api/v1/pipeline/running" + (params.length ? "?" + params.join("&") : "");
  }

  function sourceScope() {
    var drawer = document.getElementById("source-drawer");
    return (drawer && drawer.getAttribute("data-source-scope")) || "module";
  }

  function currentReportId() {
    var drawer = document.getElementById("source-drawer");
    return Number((drawer && drawer.getAttribute("data-report-id")) || 0) || null;
  }

  function requireIndustrySector() {
    var drawer = document.getElementById("source-drawer");
    if (sourceScope() === "industry" && drawer && drawer.getAttribute("data-sector-selected") === "0") {
      throw new Error("请选择行业");
    }
    var sector = currentIndustrySector();
    if (sourceScope() === "industry" && !sector) throw new Error("请选择行业");
    return sector;
  }

  function requireReportId() {
    var drawer = document.getElementById("source-drawer");
    if (sourceScope() === "industry" && drawer && drawer.getAttribute("data-sector-selected") === "0") {
      throw new Error("请选择行业");
    }
    var reportId = currentReportId();
    if (!reportId) throw new Error("请先在左侧创建报告草稿");
    return reportId;
  }

  function industrySectorReady() {
    var drawer = document.getElementById("source-drawer");
    if (!drawer || sourceScope() !== "industry") return true;
    return drawer.getAttribute("data-sector-selected") !== "0";
  }

  function industrySourcesEditable() {
    return industrySectorReady();
  }

  function industryLibraryUrl(suffix) {
    suffix = suffix || "";
    return industryRequestUrl("/api/v1/industry/library/data-sources" + suffix);
  }

  function currentIndustrySector() {
    var rail = document.querySelector(".page-rail[data-sector-key]");
    return rail ? (rail.getAttribute("data-sector-key") || "") : "";
  }

  function industryRequestHeaders(extra) {
    var headers = Object.assign({}, extra || {});
    if (sourceScope() === "industry") {
      var sector = currentIndustrySector();
      if (sector) headers["X-Industry-Sector"] = sector;
    }
    return headers;
  }

  function industryRequestUrl(url) {
    if (sourceScope() !== "industry") return url;
    var sector = currentIndustrySector();
    if (!sector) return url;
    return url + (url.indexOf("?") >= 0 ? "&" : "?") + "sector=" + encodeURIComponent(sector);
  }

  function dataSourcesUrl() {
    if (sourceScope() === "industry") {
      return industrySectorReady() ? industryLibraryUrl("") : null;
    }
    var eid = currentEntityId();
    return eid ? "/api/v1/data-sources?entity_id=" + eid : "/api/v1/data-sources";
  }

  function toEchartsOption(spec) {
    const chartType = spec.type || "bar";
    const labels = spec.labels || [];
    const seriesRaw = spec.series || [];
    const series = seriesRaw.map(function (s, idx) {
      return {
        name: s.name || "系列" + (idx + 1),
        type: chartType === "line" ? "line" : "bar",
        data: s.data || [],
        smooth: chartType === "line",
        itemStyle: { color: COLORS[idx % COLORS.length] },
      };
    });
    return {
      title: {
        text: spec.title || "",
        left: "center",
        textStyle: { color: "#4a5160", fontSize: 13, fontFamily: "Microsoft YaHei, 微软雅黑, sans-serif", fontWeight: 400 },
      },
      tooltip: { trigger: chartType === "line" ? "axis" : "item" },
      grid: { left: "8%", right: "4%", bottom: "12%", containLabel: true },
      xAxis: {
        type: "category",
        data: labels,
        axisLabel: { color: "#7a828e", rotate: labels.length > 6 ? 30 : 0, fontFamily: "Microsoft YaHei, 微软雅黑, sans-serif", fontWeight: 400 },
      },
      yAxis: { type: "value", axisLabel: { color: "#7a828e", fontFamily: "Microsoft YaHei, 微软雅黑, sans-serif", fontWeight: 400 } },
      series: series,
      color: COLORS,
    };
  }

  function renderEntryCharts() {
    if (!window.echarts || !window.ENTRY_CHARTS) return;
    window.ENTRY_CHARTS.forEach(function (item) {
      const host = document.getElementById("chart-entry-" + item.entry_id);
      if (!host || !item.specs || !item.specs.length) return;
      host.style.height = "220px";
      host.style.marginTop = "0.75rem";
      const chart = echarts.init(host);
      const spec = item.specs[0];
      chart.setOption(toEchartsOption(spec));
      chartInstances.push(chart);
    });
    if (!window.__riskChartsResizeBound) {
      window.__riskChartsResizeBound = true;
      window.addEventListener("resize", function () {
        chartInstances.forEach(function (c) {
          try { c.resize(); } catch (e) { /* ignore */ }
        });
      });
    }
  }

  /* ---------- 数据源面板 / 关于（仅深度研报页存在） ---------- */
  const drawer = document.getElementById("source-drawer");
  const toggleBtn = document.getElementById("btn-toggle-source-drawer");
  const saveIndustryLibraryBtn = document.getElementById("btn-save-industry-library");
  const sourceAddModal = document.getElementById("source-add-modal");
  const sourceAddClose = document.getElementById("btn-close-source-add");
  const addBox = document.getElementById("sources-add-box");
  const addToggle = document.getElementById("btn-toggle-add-sources");
  const aiSearchBtn = document.getElementById("btn-ai-search-sources");
  const aiSearchInput = document.getElementById("industry-source-search-query");
  const searchResultsModal = document.getElementById("search-results-modal");
  const searchResultsList = document.getElementById("search-results-list");
  const searchResultsQuery = document.getElementById("search-results-query");
  const searchResultsCount = document.getElementById("search-results-count");
  const addSearchResultsBtn = document.getElementById("btn-add-search-results");
  const closeSearchResultsBtn = document.getElementById("btn-close-search-results");
  const cancelSearchResultsBtn = document.getElementById("btn-cancel-search-results");
  const emptyAdd = document.getElementById("btn-empty-add-source");
  const STORAGE_KEY = "sources_panel_collapsed";
  const listWrap = document.getElementById("managed-source-list-wrap");
  const fileInput = document.getElementById("global-upload-file");
  const dropzone = document.getElementById("sources-dropzone");
  const uploadSubmitBtn = document.getElementById("global-upload-submit");
  const ALLOWED_EXTS = [".txt", ".xlsx", ".docx", ".pptx", ".pdf"];
  var searchCandidates = [];

  if (drawer) {
  function isDrawerOpen() {
    return !document.body.classList.contains("sources-collapsed");
  }

  function setDrawerOpen(open) {
    if (!drawer) return;
    document.body.classList.toggle("sources-collapsed", !open);
    drawer.setAttribute("aria-hidden", open ? "false" : "true");
    if (toggleBtn) {
      toggleBtn.setAttribute("aria-expanded", open ? "true" : "false");
      toggleBtn.title = open ? "收起数据源面板" : "展开数据源面板";
      toggleBtn.setAttribute("aria-label", open ? "收起数据源面板" : "展开数据源面板");
    }
    try {
      localStorage.setItem(STORAGE_KEY, open ? "0" : "1");
    } catch (e) { /* ignore */ }
  }

  function setAddBoxOpen(open) {
    if (!sourceAddModal || !addBox) return;
    sourceAddModal.classList.toggle("hidden", !open);
    sourceAddModal.hidden = !open;
    if (open) {
      window.setTimeout(function () {
        var firstInput = sourceAddModal.querySelector("input:not([type='file'])");
        if (firstInput) firstInput.focus();
      }, 0);
    }
  }

  function closeSearchResults() {
    if (!searchResultsModal) return;
    searchResultsModal.classList.add("hidden");
    searchResultsModal.hidden = true;
  }

  function selectedSearchCandidates() {
    if (!searchResultsList) return [];
    return Array.from(searchResultsList.querySelectorAll(".search-result-select:checked"))
      .map(function (input) { return searchCandidates[Number(input.value)]; })
      .filter(Boolean);
  }

  function updateSearchResultsCount() {
    var count = selectedSearchCandidates().length;
    if (searchResultsCount) searchResultsCount.textContent = "已选 " + count + " 条";
    if (addSearchResultsBtn) addSearchResultsBtn.disabled = count === 0;
  }

  function openSearchResults(items, query) {
    searchCandidates = Array.isArray(items) ? items.filter(function (item) {
      return item && /^https?:\/\//i.test(String(item.url || "")) && item.title;
    }) : [];
    if (!searchResultsModal || !searchResultsList) return;
    if (searchResultsQuery) searchResultsQuery.textContent = query ? "搜索：" + query : "搜索结果";
    searchResultsList.innerHTML = searchCandidates.length
      ? searchCandidates.map(function (item, index) {
          var domain = item.source_domain || "网页来源";
          var date = item.published_at ? " · " + item.published_at : "";
          return '<label class="search-result-item">' +
            '<input class="search-result-select" type="checkbox" value="' + index + '">' +
            '<span class="search-result-content"><strong>' + escapeHtml(item.title) + '</strong>' +
            '<span>' + escapeHtml(item.snippet || "未提供摘要") + '</span>' +
            '<small>' + escapeHtml((item.matched_term ? item.matched_term + " · " : "") + domain + date) + '</small></span>' +
            '<a href="' + escapeHtml(item.url) + '" target="_blank" rel="noopener" title="打开原网页">↗</a>' +
          '</label>';
        }).join("")
      : '<p class="sources-empty-desc">未找到可加入的网页来源，请调整关键词后重试。</p>';
    searchResultsList.querySelectorAll(".search-result-select").forEach(function (input) {
      input.addEventListener("change", updateSearchResultsCount);
    });
    updateSearchResultsCount();
    searchResultsModal.classList.remove("hidden");
    searchResultsModal.hidden = false;
  }

  try {
    if (localStorage.getItem(STORAGE_KEY) === "1") setDrawerOpen(false);
    else setDrawerOpen(true);
  } catch (e) {
    setDrawerOpen(true);
  }

  if (toggleBtn) {
    toggleBtn.addEventListener("click", function (event) {
      event.stopPropagation();
      setDrawerOpen(!isDrawerOpen());
    });
  }
  // 收起后整条窄栏都是展开入口，避免只剩图标时难以点击。
  var sourceCollapsedHeader = drawer.querySelector(".sources-panel-header");
  if (sourceCollapsedHeader) {
    sourceCollapsedHeader.addEventListener("click", function () {
      if (!isDrawerOpen()) setDrawerOpen(true);
    });
  }
  if (addToggle) addToggle.addEventListener("click", function () {
    setAddBoxOpen(sourceAddModal ? sourceAddModal.hidden : true);
  });
  document.querySelectorAll("[data-open-source-modal]").forEach(function (button) {
    button.addEventListener("click", function () {
      setDrawerOpen(true);
      setAddBoxOpen(true);
    });
  });
  if (aiSearchBtn) aiSearchBtn.addEventListener("click", async function () {
    if (sourceScope() !== "industry") return;
    aiSearchBtn.disabled = true;
    var original = aiSearchBtn.textContent;
    aiSearchBtn.textContent = "正在搜索…";
    try {
      var endpoint = industryLibraryUrl("/search");
      var resp = await fetch(endpoint, {
        method: "POST",
        headers: industryRequestHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ query: aiSearchInput ? aiSearchInput.value.trim() : "" }),
      });
      var data = await resp.json().catch(function () { return {}; });
      if (!resp.ok) throw new Error(data.detail || "AI 搜索失败");
      openSearchResults(data.items, data.query || (aiSearchInput ? aiSearchInput.value.trim() : ""));
    } catch (err) {
      showSourceToast("AI 搜索失败：" + err.message);
    } finally {
      aiSearchBtn.disabled = false;
      aiSearchBtn.textContent = original;
    }
  });
  if (aiSearchInput) aiSearchInput.addEventListener("keydown", function (event) {
    if (event.key === "Enter" && aiSearchBtn && !aiSearchBtn.disabled) {
      event.preventDefault();
      aiSearchBtn.click();
    }
  });
  if (emptyAdd) emptyAdd.addEventListener("click", function () {
    setDrawerOpen(true);
    setAddBoxOpen(true);
  });
  if (closeSearchResultsBtn) closeSearchResultsBtn.addEventListener("click", closeSearchResults);
  if (cancelSearchResultsBtn) cancelSearchResultsBtn.addEventListener("click", closeSearchResults);
  if (searchResultsModal) searchResultsModal.addEventListener("click", function (event) {
    if (event.target === searchResultsModal) closeSearchResults();
  });
  if (addSearchResultsBtn) addSearchResultsBtn.addEventListener("click", async function () {
    var selectedItems = selectedSearchCandidates();
    if (!selectedItems.length || sourceScope() !== "industry") return;
    addSearchResultsBtn.disabled = true;
    var original = addSearchResultsBtn.textContent;
    addSearchResultsBtn.textContent = "正在加入…";
    try {
      var endpoint = industryLibraryUrl("/search/add");
      var response = await fetch(endpoint, {
        method: "POST",
        headers: industryRequestHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ items: selectedItems }),
      });
      var result = await response.json().catch(function () { return {}; });
      if (!response.ok) throw new Error(result.detail || "加入来源失败");
      closeSearchResults();
      showSourceToast(result.message || "已加入来源");
      await refreshSourceList();
    } catch (error) {
      showSourceToast("加入来源失败：" + error.message);
      updateSearchResultsCount();
    } finally {
      addSearchResultsBtn.textContent = original;
    }
  });
  if (saveIndustryLibraryBtn) saveIndustryLibraryBtn.addEventListener("click", async function () {
    if (sourceScope() !== "industry") return;
    saveIndustryLibraryBtn.disabled = true;
    const original = saveIndustryLibraryBtn.textContent;
    saveIndustryLibraryBtn.textContent = "正在保存…";
    try {
      const endpoint = industryRequestUrl(
        "/api/v1/industry/reports/" + requireReportId() + "/save-library"
      );
      const resp = await fetch(endpoint, { method: "POST", headers: industryRequestHeaders() });
      const data = await resp.json().catch(function () { return {}; });
      if (!resp.ok) throw new Error(data.detail || "保存失败");
      showSourceToast(data.message || "已保存到行业资料库");
      saveIndustryLibraryBtn.textContent = "已保存到行业资料库";
      saveIndustryLibraryBtn.classList.add("is-saved");
    } catch (err) {
      saveIndustryLibraryBtn.disabled = false;
      saveIndustryLibraryBtn.textContent = original;
      showSourceToast("保存失败：" + (err.message || err));
    }
  });
  if (sourceAddClose) sourceAddClose.addEventListener("click", function () { setAddBoxOpen(false); });
  if (sourceAddModal) sourceAddModal.addEventListener("click", function (event) {
    if (event.target === sourceAddModal) setAddBoxOpen(false);
  });
  document.addEventListener("keydown", function (ev) {
    if (ev.key === "Escape") {
      setAddBoxOpen(false);
      closeSearchResults();
      var svm = document.getElementById("source-view-modal");
      if (svm) { svm.classList.add("hidden"); svm.hidden = true; }
    }
  });

  function escapeHtml(s) {
    return String(s || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function showSourceToast(text) {
    var el = document.getElementById("sources-toast");
    if (!el && listWrap) {
      el = document.createElement("p");
      el.id = "sources-toast";
      el.className = "sources-toast hint";
      listWrap.parentNode.insertBefore(el, listWrap);
    }
    if (!el) return;
    el.textContent = text;
    el.hidden = false;
    clearTimeout(el._hideTimer);
    el._hideTimer = setTimeout(function () {
      el.hidden = true;
    }, 4500);
  }

  function fileExtOk(name) {
    var lower = String(name || "").toLowerCase();
    return ALLOWED_EXTS.some(function (ext) { return lower.endsWith(ext); });
  }

  function syncUploadButton() {
    if (!uploadSubmitBtn || !fileInput) return;
    uploadSubmitBtn.disabled = !(fileInput.files && fileInput.files.length);
    if (fileInput.files && fileInput.files[0] && dropzone) {
      var title = dropzone.querySelector(".sources-dropzone-title");
      if (title) title.textContent = "已选择：" + fileInput.files[0].name;
    }
  }

  async function uploadSelectedFile(file, optionalName) {
    if (!file) throw new Error("请先选择文件");
    if (!fileExtOk(file.name)) {
      throw new Error("不支持的文件类型，允许: " + ALLOWED_EXTS.join(", "));
    }
    const fd = new FormData();
    fd.append("file", file);
    fd.append("name", optionalName || file.name);
    var endpoint = "/api/v1/data-sources/upload";
    var headers = {};
    if (sourceScope() === "industry") {
      endpoint = industryLibraryUrl("/upload");
      headers = industryRequestHeaders();
    } else {
      var eid = currentEntityId();
      if (eid) fd.append("entity_id", String(eid));
    }
    const resp = await fetch(endpoint, { method: "POST", body: fd, headers: headers });
    const data = await resp.json().catch(function () { return {}; });
    if (!resp.ok) {
      var detail = data.detail;
      if (Array.isArray(detail)) detail = detail.map(function (d) { return d.msg || d; }).join("; ");
      throw new Error(detail || "上传失败");
    }
    return data;
  }

  function bindDropTarget(el) {
    if (!el) return;
    var dragDepth = 0;
    el.addEventListener("dragenter", function (e) {
      e.preventDefault();
      e.stopPropagation();
      dragDepth += 1;
      el.classList.add("is-dragover");
    });
    el.addEventListener("dragover", function (e) {
      e.preventDefault();
      e.stopPropagation();
      if (e.dataTransfer) e.dataTransfer.dropEffect = "copy";
      el.classList.add("is-dragover");
    });
    el.addEventListener("dragleave", function (e) {
      e.preventDefault();
      e.stopPropagation();
      dragDepth = Math.max(0, dragDepth - 1);
      if (dragDepth === 0) el.classList.remove("is-dragover");
    });
    el.addEventListener("drop", function (e) {
      e.preventDefault();
      e.stopPropagation();
      dragDepth = 0;
      el.classList.remove("is-dragover");
      var fileList = e.dataTransfer && e.dataTransfer.files;
      var file = fileList && fileList.length ? fileList[0] : null;
      if (!file) return;
      setDrawerOpen(true);
      setAddBoxOpen(true);
      uploadSelectedFile(file)
        .then(function (data) {
          showSourceToast(data.message || "数据源已上传");
          if (fileInput) fileInput.value = "";
          syncUploadButton();
          if (dropzone) {
            var title = dropzone.querySelector(".sources-dropzone-title");
            if (title) title.textContent = "点击或拖拽文件到此处上传";
          }
          return refreshSourceList();
        })
        .catch(function (err) {
          alert(err.message || String(err));
        });
    });
  }

  if (fileInput) {
    fileInput.addEventListener("change", function () {
      syncUploadButton();
    });
  }
  if (dropzone && fileInput) {
    dropzone.addEventListener("click", function () {
      if (fileInput.disabled) return;
      fileInput.click();
    });
    dropzone.addEventListener("keydown", function (e) {
      if (e.key !== "Enter" && e.key !== " ") return;
      e.preventDefault();
      if (fileInput.disabled) return;
      fileInput.click();
    });
  }
  bindDropTarget(dropzone);
  bindDropTarget(listWrap);

  // 拖拽文件经过面板时，阻止浏览器直接打开文件
  if (drawer) {
    ["dragenter", "dragover"].forEach(function (evt) {
      drawer.addEventListener(evt, function (e) {
        if (!(e.dataTransfer && Array.prototype.indexOf.call(e.dataTransfer.types || [], "Files") >= 0)) {
          return;
        }
        e.preventDefault();
      });
    });
    drawer.addEventListener("drop", function (e) {
      if (!(e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files.length)) return;
      e.preventDefault();
    });
  }

  function bindSourceItemActions(root) {
    (root || document).querySelectorAll(".view-source").forEach(function (btn) {
      if (btn._bound) return;
      btn._bound = true;
      btn.addEventListener("click", onViewSource);
    });
    (root || document).querySelectorAll(".delete-source").forEach(function (btn) {
      if (btn._bound) return;
      btn._bound = true;
      btn.addEventListener("click", onDeleteSource);
    });
  }

  function updateSelectionSummary() {
    var count = document.getElementById("source-selection-count");
    var boxes = document.querySelectorAll(".source-select-checkbox:not(:disabled)");
    if (count) {
      var selected = document.querySelectorAll(".source-select-checkbox:not(:disabled):checked").length;
      count.textContent = "已选 " + selected + " / " + boxes.length + " 条可用来源";
    }
    var workspaceCount = document.getElementById("workspace-selected-count");
    if (workspaceCount) {
      workspaceCount.textContent = document.querySelectorAll(".source-select-checkbox:not(:disabled):checked").length;
    }
    var generate = document.getElementById("btn-generate-report");
    if (generate && industrySourcesEditable()) {
      generate.disabled = boxes.length === 0 || !document.querySelectorAll(".source-select-checkbox:not(:disabled):checked").length;
    }
  }

  async function saveIndustrySelection() {
    if (sourceScope() !== "industry" || !industrySourcesEditable()) return;
    var boxes = Array.prototype.slice.call(document.querySelectorAll(".source-select-checkbox:not(:disabled)"));
    var sourceIds = boxes.filter(function (box) { return box.checked; }).map(function (box) {
      return Number(box.getAttribute("data-id"));
    });
    boxes.forEach(function (box) { box.disabled = true; });
    try {
      var endpoint = industryLibraryUrl("/selection");
      var resp = await fetch(endpoint, {
        method: "PATCH",
        headers: industryRequestHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ source_ids: sourceIds }),
      });
      var data = await resp.json().catch(function () { return {}; });
      if (!resp.ok) throw new Error(data.detail || "保存选择失败");
      showSourceToast(data.message || "已保存数据源选择");
    } catch (err) {
      showSourceToast("保存选择失败：" + err.message);
      await refreshSourceList();
    } finally {
      boxes.forEach(function (box) { box.disabled = false; });
      updateSelectionSummary();
    }
  }

  function bindSourceSelection(root) {
    if (sourceScope() !== "industry") return;
    (root || document).querySelectorAll(".source-select-checkbox").forEach(function (box) {
      if (box._selectionBound) return;
      box._selectionBound = true;
      box.addEventListener("change", function () {
        updateSelectionSummary();
        saveIndustrySelection();
      });
    });
    var all = document.getElementById("btn-select-all-sources");
    if (all && !all._selectionBound) {
      all._selectionBound = true;
      all.addEventListener("click", function () {
        document.querySelectorAll(".source-select-checkbox:not(:disabled)").forEach(function (box) { box.checked = true; });
        updateSelectionSummary();
        saveIndustrySelection();
      });
    }
    var deleteSelected = document.getElementById("btn-delete-selected-sources");
    if (deleteSelected && !deleteSelected._selectionBound) {
      deleteSelected._selectionBound = true;
      deleteSelected.addEventListener("click", deleteSelectedIndustrySources);
    }
    updateSelectionSummary();
  }

  function renderSourceList(sources) {
    if (!listWrap) return;
    if (!sources || !sources.length) {
      var emptyAction = industrySourcesEditable()
        ? '<p class="sources-empty-hint">将文件拖放到此处，或 <button type="button" class="text-link" id="btn-empty-add-source-dyn">添加数据源</button></p>'
        : '<p class="sources-empty-hint">当前报告不可修改数据源。</p>';
      listWrap.innerHTML =
        '<div class="sources-empty" id="managed-source-list">' +
        '<p class="sources-empty-title">保存的数据源将显示在此处</p>' +
        '<p class="sources-empty-desc">当前报告的数据源组为空。</p>' + emptyAction +
        "</div>";
      var dyn = document.getElementById("btn-empty-add-source-dyn");
      if (dyn) {
        dyn.addEventListener("click", function () {
          setDrawerOpen(true);
          setAddBoxOpen(true);
        });
      }
      return;
    }
    var html = '<ul class="managed-source-list" id="managed-source-list">';
    if (sourceScope() === "industry" && industrySourcesEditable()) {
      var usableSources = sources.filter(function (src) { return Number(src.chars || 0) > 0; });
      var selectedCount = usableSources.filter(function (src) { return src.is_selected !== false; }).length;
      html = '<div class="source-selection-toolbar"><span id="source-selection-count">已选 ' + selectedCount + ' / ' + usableSources.length + ' 条可用来源</span>' +
        '<button type="button" class="text-link" id="btn-select-all-sources">全选</button>' +
        '<button type="button" class="text-link danger-link" id="btn-delete-selected-sources">删除所选</button></div>' + html;
    }
    sources.forEach(function (src) {
      var typeLabel = src.source_type === "network_search" ? "网络搜索功能" : (src.source_type || "-");
      var meta = [src.origin_label || typeLabel];
      if (src.original_filename) meta.push(src.original_filename);
      if (src.url) meta.push("网址");
      if (typeof src.chars === "number") meta.push(src.chars + " 字");
      else if (src.text_preview) meta.push("有正文");
      html +=
        '<li class="managed-source-item" data-id="' + src.id + '">' +
        ((sourceScope() === "industry" && industrySourcesEditable())
          ? '<label class="source-select-control" title="' + (Number(src.chars || 0) > 0 ? '纳入本次 AI 生成' : '该来源尚未提取到正文，不能用于生成') + '"><input type="checkbox" class="source-select-checkbox" data-id="' + src.id + '"' + (src.is_selected !== false && Number(src.chars || 0) > 0 ? ' checked' : '') + (Number(src.chars || 0) > 0 ? '' : ' disabled') + '><span class="sr-only">选择 ' + escapeHtml(src.name) + '</span></label>'
          : '') +
        '<button type="button" class="managed-source-main view-source" data-id="' + src.id + '" title="点击查看">' +
        '<span class="source-name">' + escapeHtml(src.name) + "</span>" +
        '<span class="source-meta">' + escapeHtml(meta.join(" · ")) + "</span>" +
        "</button>" +
        ((industrySourcesEditable()) ? '<button type="button" class="link-btn delete-source" data-id="' + src.id + '">删除</button>' : '') +
        "</li>";
    });
    html += "</ul>";
    listWrap.innerHTML = html;
    bindSourceItemActions(listWrap);
    bindSourceSelection(listWrap);
  }

  async function refreshSourceList() {
    // 行业页按来源类型分组渲染。变更后刷新整页，保持单一来源面板中的
    // 搜索结果、补充材料和勾选状态一致。
    if (sourceScope() === "industry") {
      window.location.reload();
      return;
    }
    try {
      const url = dataSourcesUrl();
      if (!url) {
        renderSourceList([]);
        return;
      }
      const resp = await fetch(url, { headers: industryRequestHeaders() });
      const data = await resp.json().catch(function () { return {}; });
      if (!resp.ok) throw new Error(data.detail || "刷新失败");
      renderSourceList(data);
    } catch (e) {
      showSourceToast("列表刷新失败: " + e.message);
    }
  }

  bindSourceSelection(document);

  async function handleSourceMutation(promiseFactory) {
    try {
      const data = await promiseFactory();
      showSourceToast(data.message || "已保存");
      await refreshSourceList();
      setAddBoxOpen(false);
    } catch (e) {
      alert(e.message || String(e));
    }
  }

  document.querySelectorAll(".upload-form").forEach(function (form) {
    form.addEventListener("submit", async function (ev) {
      ev.preventDefault();
      const moduleCode = form.getAttribute("data-module");
      const fd = new FormData(form);
      if (moduleCode) fd.append("module_code", moduleCode);
      await handleSourceMutation(async function () {
        const resp = await fetch("/api/v1/data-sources/upload", { method: "POST", body: fd });
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.detail || "上传失败");
        return data;
      });
      form.reset();
    });
  });

  const globalUpload = document.getElementById("global-upload-form");
  if (globalUpload) {
    globalUpload.addEventListener("submit", async function (ev) {
      ev.preventDefault();
      if (uploadSubmitBtn) uploadSubmitBtn.disabled = true;
      try {
        await handleSourceMutation(async function () {
          if (!fileInput || !fileInput.files || !fileInput.files[0]) {
            throw new Error("请先选择或拖入文件");
          }
          return uploadSelectedFile(fileInput.files[0]);
        });
        if (fileInput) fileInput.value = "";
        if (dropzone) {
          var title = dropzone.querySelector(".sources-dropzone-title");
          if (title) title.textContent = "点击或拖拽文件到此处上传";
        }
      } finally {
        syncUploadButton();
      }
    });
  }

  document.querySelectorAll(".url-form").forEach(function (form) {
    form.addEventListener("submit", async function (ev) {
      ev.preventDefault();
      const moduleCode = form.getAttribute("data-module");
      const name = form.querySelector('[name="name"]').value;
      const url = form.querySelector('[name="url"]').value;
      const body = { name: name, url: url, priority: 0 };
      if (moduleCode) body.module_code = moduleCode;
      await handleSourceMutation(async function () {
        const resp = await fetch("/api/v1/data-sources/url", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.detail || "添加失败");
        return data;
      });
      form.reset();
    });
  });

  const globalUrl = document.getElementById("global-url-form");
  if (globalUrl) {
    globalUrl.addEventListener("submit", async function (ev) {
      ev.preventDefault();
      const submitBtn = globalUrl.querySelector('button[type="submit"]');
      if (submitBtn) submitBtn.disabled = true;
      try {
        const url = globalUrl.querySelector('[name="url"]').value;
        let name = "网页来源";
        try {
          const parsed = new URL(url);
          name = parsed.hostname.replace(/^www\./i, "") || name;
        } catch (e) { /* URL 字段将由浏览器原生校验；此处保留安全兜底名称。 */ }
        await handleSourceMutation(async function () {
          const body = { name: name, url: url, priority: 0 };
          var endpoint = "/api/v1/data-sources/url";
          var headers = { "Content-Type": "application/json" };
          if (sourceScope() === "industry") {
            endpoint = industryLibraryUrl("/url");
            headers = industryRequestHeaders(headers);
          } else {
            var eid = currentEntityId();
            if (eid) body.entity_id = eid;
          }
          const resp = await fetch(endpoint, {
            method: "POST",
            headers: headers,
            body: JSON.stringify(body),
          });
          const data = await resp.json().catch(function () { return {}; });
          if (!resp.ok) throw new Error(data.detail || "添加失败");
          return data;
        });
        globalUrl.reset();
      } finally {
        if (submitBtn) submitBtn.disabled = false;
      }
    });
  }

  async function onDeleteSource(ev) {
    const btn = ev.currentTarget;
    const id = btn.getAttribute("data-id");
    if (!confirm("确认删除该数据源？")) return;
    await handleSourceMutation(async function () {
      const endpoint = sourceScope() === "industry"
        ? industryLibraryUrl("/" + id)
        : "/api/v1/data-sources/" + id;
      const resp = await fetch(endpoint, {
        method: "DELETE",
        headers: industryRequestHeaders(),
      });
      const data = await resp.json().catch(function () { return {}; });
      if (!resp.ok) throw new Error(data.detail || "删除失败");
      return data;
    });
  }

  async function deleteSelectedIndustrySources() {
    if (sourceScope() !== "industry") return;
    const ids = Array.from(document.querySelectorAll(".source-select-checkbox:checked"))
      .map(function (box) { return box.getAttribute("data-id"); })
      .filter(Boolean);
    if (!ids.length) {
      showSourceToast("请先勾选需要删除的来源");
      return;
    }
    if (!confirm("确认删除已勾选的 " + ids.length + " 条来源？此操作不可撤销。")) return;
    const btn = document.getElementById("btn-delete-selected-sources");
    if (btn) btn.disabled = true;
    try {
      for (const id of ids) {
        const endpoint = industryLibraryUrl("/" + id);
        const resp = await fetch(endpoint, {
          method: "DELETE",
          headers: industryRequestHeaders(),
        });
        const data = await resp.json().catch(function () { return {}; });
        if (!resp.ok) throw new Error(data.detail || "删除失败");
      }
      showSourceToast("已删除 " + ids.length + " 条来源");
      await refreshSourceList();
    } catch (err) {
      showSourceToast("删除失败：" + (err.message || err));
      if (btn) btn.disabled = false;
    }
  }

  document.querySelectorAll(".delete-source").forEach(function (btn) {
    btn._bound = true;
    btn.addEventListener("click", onDeleteSource);
  });

  const sourceViewModal = document.getElementById("source-view-modal");
  const sourceViewBody = document.getElementById("source-view-body");
  const sourceViewTitle = document.getElementById("source-view-title");
  const sourceViewClose = document.getElementById("btn-close-source-view");

  function setSourceViewOpen(open) {
    if (!sourceViewModal) return;
    sourceViewModal.classList.toggle("hidden", !open);
    sourceViewModal.hidden = !open;
  }

  if (sourceViewClose) sourceViewClose.addEventListener("click", function () { setSourceViewOpen(false); });

  async function onViewSource(ev) {
    const btn = ev.currentTarget;
    const id = btn.getAttribute("data-id");
    setSourceViewOpen(true);
    if (sourceViewBody) sourceViewBody.innerHTML = '<p class="hint">加载中…</p>';
    try {
      const endpoint = sourceScope() === "industry"
        ? industryLibraryUrl("/" + id)
        : "/api/v1/data-sources/item/" + id;
      const resp = await fetch(endpoint, { headers: industryRequestHeaders() });
      const data = await resp.json().catch(function () { return {}; });
      if (!resp.ok) throw new Error(data.detail || "加载失败");
      if (sourceViewTitle) sourceViewTitle.textContent = data.name || "数据详情";
      function externalHref(url) {
        var value = String(url || "").trim();
        if (!value) return "";
        if (/^https?:\/\//i.test(value)) return value;
        if (/^\/\//.test(value)) return "https:" + value;
        return "https://" + value;
      }
      const metaParts = [
        "类型：" + (data.source_type === "network_search" ? "网络搜索" : (data.source_type || "-")),
        "来源：" + (data.origin_label || "用户添加"),
        data.original_filename ? ("文件：" + data.original_filename) : "",
        "字数：" + ((data.extracted_text || "").length),
      ];
      let meta = metaParts.filter(Boolean).map(escapeHtml).join("<br/>");
      if (data.url) {
        const href = escapeHtml(externalHref(data.url));
        const label = escapeHtml(data.url);
        meta += '<br/>链接：<a href="' + href + '" target="_blank" rel="noopener noreferrer">' + label + "</a>";
      }
      const text = escapeHtml(data.extracted_text || data.text_preview || "（无提取正文）");
      if (sourceViewBody) {
        sourceViewBody.innerHTML =
          '<p class="meta">' + meta + "</p>" +
          '<pre class="source-text-preview">' + text + "</pre>";
      }
    } catch (e) {
      if (sourceViewBody) sourceViewBody.innerHTML = '<p class="empty-module">加载失败：' + escapeHtml(e.message) + "</p>";
    }
  }

  document.querySelectorAll(".view-source").forEach(function (btn) {
    btn._bound = true;
    btn.addEventListener("click", onViewSource);
  });

  } // end: source drawer present
  /* ---------- 流水线（异步 + 轮询，不阻断侧栏/上传） ---------- */
  const pipelineBtn = document.getElementById("btn-run-pipeline");

  function sleep(ms) {
    return new Promise(function (resolve) { setTimeout(resolve, ms); });
  }

  function setCollectBusy(busy) {
    // 仅禁用本页采集按钮，避免重复提交；其它界面操作保持可用
    if (pipelineBtn) pipelineBtn.disabled = !!busy;
  }

  function rememberJob(jobId) {
    activeJobId = jobId || null;
    try {
      if (jobId) sessionStorage.setItem(jobStorageKey(), jobId);
      else sessionStorage.removeItem(jobStorageKey());
    } catch (e) { /* ignore */ }
  }

  function pipelineMsgEl() {
    return document.getElementById("pipeline-msg");
  }

  /** 东京时间（Asia/Tokyo）24 小时制，精确到分钟：YYYY/M/D HH:mm */
  function formatRefreshTime(isoOrDate) {
    var d;
    if (isoOrDate instanceof Date) {
      d = isoOrDate;
    } else if (isoOrDate) {
      var s = String(isoOrDate).trim();
      // 后端返回东京墙钟；无时区后缀时按 +09:00 解析，避免被当作浏览器本地/UTC
      if (/^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}/.test(s) && !/(Z|[+-]\d{2}:?\d{2})$/i.test(s)) {
        s = s.replace(" ", "T") + "+09:00";
      }
      d = new Date(s);
    } else {
      d = new Date();
    }
    if (isNaN(d.getTime())) d = new Date();

    var parts = new Intl.DateTimeFormat("en-GB", {
      timeZone: "Asia/Tokyo",
      year: "numeric",
      month: "numeric",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
      hourCycle: "h23",
    }).formatToParts(d);

    var map = {};
    parts.forEach(function (p) {
      if (p.type !== "literal") map[p.type] = p.value;
    });
    return (
      map.year + "/" + String(parseInt(map.month, 10)) + "/" + String(parseInt(map.day, 10)) +
      " " + map.hour + ":" + map.minute
    );
  }

  function persistPipelineMsg(text) {
    try { sessionStorage.setItem(refreshMsgKey(), text); } catch (e) { /* ignore */ }
  }

  function persistRefreshAt(isoOrDate) {
    try {
      if (!isoOrDate) {
        sessionStorage.setItem(refreshAtKey(), new Date().toISOString());
        return;
      }
      if (isoOrDate instanceof Date) {
        sessionStorage.setItem(refreshAtKey(), isoOrDate.toISOString());
        return;
      }
      sessionStorage.setItem(refreshAtKey(), String(isoOrDate));
    } catch (e) { /* ignore */ }
  }

  function setPipelineMsg(msgEl, text) {
    if (msgEl) {
      msgEl.textContent = text;
      // Modified by DingJiaye: 2026-08-26 — 默认隐藏静态说明，仅在有运行结果时显示。
      msgEl.hidden = !text;
    }
    persistPipelineMsg(text);
  }

  function setCollectingMsg(msgEl) {
    setPipelineMsg(msgEl, "情报智能体正在检索与核验…");
  }

  function setRefreshSuccessMsg(msgEl, finishedAt) {
    setPipelineMsg(msgEl, "上次刷新时间 " + formatRefreshTime(finishedAt));
    persistRefreshAt(finishedAt);
  }

  function setCollectFailMsg(msgEl, reason) {
    var detail = String(reason || "").trim();
    setPipelineMsg(msgEl, detail ? ("采集失败：" + detail) : "采集失败，请重新尝试。");
  }

  function apiErrorMessage(detail, fallback) {
    if (typeof detail === "string" && detail.trim()) return detail.trim();
    if (detail && typeof detail.message === "string" && detail.message.trim()) return detail.message.trim();
    // FastAPI/Pydantic 的 422 结构：[{loc: [...], msg: "..."}]。
    // 只展示字段与校验说明，避免再次用笼统错误掩盖真实原因。
    if (Array.isArray(detail) && detail.length) {
      return detail.map(function (item) {
        var location = Array.isArray(item.loc) ? item.loc.filter(function (part) { return part !== "body"; }).join(".") : "";
        return (location ? location + "：" : "") + (item.msg || "参数格式不正确");
      }).join("；");
    }
    return fallback || "执行失败";
  }

  function whenPageGlobalsReady(fn) {
    // base.html 先加载本文件，再执行 {% block scripts %} 注入 window.*；
    // 依赖全局变量的初始化必须延后，否则补采/恢复轮询会静默跳过。
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", fn);
    } else {
      setTimeout(fn, 0);
    }
  }

  whenPageGlobalsReady(function () {
    (function restorePipelineMsg() {
      var msg = pipelineMsgEl();
      if (!msg) return;
      try {
        var at = sessionStorage.getItem(refreshAtKey());
        if (at) {
          setRefreshSuccessMsg(msg, at);
          return;
        }
        var saved = sessionStorage.getItem(refreshMsgKey());
        if (!saved) return;
        saved = saved.replace("最近更新时间", "最近刷新时间").replace("最近刷新时间", "上次刷新时间");
        var oldWrapped = saved.match(/^上次刷新时间[（(](\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?)[）)]$/);
        if (oldWrapped) {
          setRefreshSuccessMsg(msg, oldWrapped[1].replace(" ", "T") + "Z");
          return;
        }
        var slashForm = saved.match(/^上次刷新时间\s+(\d{4})\/(\d{1,2})\/(\d{1,2})\s+(\d{1,2}):(\d{2})$/);
        if (slashForm) {
          var iso =
            slashForm[1] + "-" +
            String(slashForm[2]).padStart(2, "0") + "-" +
            String(slashForm[3]).padStart(2, "0") + "T" +
            String(slashForm[4]).padStart(2, "0") + ":" +
            slashForm[5] + ":00+09:00";
          setRefreshSuccessMsg(msg, iso);
          return;
        }
        setPipelineMsg(msg, saved);
      } catch (e) { /* ignore */ }
    })();
  });

  async function pollJob(jobId, msgEl, maxRounds) {
    const rounds = maxRounds || 900; // ~30 分钟（2s 间隔），长任务不再误报超时
    for (let i = 0; i < rounds; i++) {
      if (pageLeaving) return { status: "left", skipped: true };
      const resp = await fetch("/api/v1/pipeline/jobs/" + jobId);
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.detail || "无法查询任务状态");
      const status = data.status || "";
      if (msgEl && !pageLeaving) setCollectingMsg(msgEl);
      if (status === "completed" || status === "failed") {
        return data;
      }
      // 前 30 次 2s，之后 3s，减轻轮询压力
      await sleep(i < 30 ? 2000 : 3000);
    }
    throw new Error("等待超时，任务可能仍在后台运行，请稍后刷新页面查看结果");
  }

  async function trackJob(jobId, msgEl) {
    if (polling) return;
    polling = true;
    rememberJob(jobId);
    setCollectBusy(true);
    const target = msgEl || pipelineMsgEl();
    try {
      const done = await pollJob(jobId, target);
      if (pageLeaving || (done && done.skipped)) {
        return done;
      }
      rememberJob(null);
      if (done.status === "failed") {
        setCollectFailMsg(target);
      } else {
        setRefreshSuccessMsg(target, done.finished_at || null);
      }
      // 仅在采集结束后、且用户未跳转时刷新当前页；不打断正在进行的页面跳转
      setTimeout(function () {
        if (!pageLeaving) window.location.reload();
      }, 1200);
      return done;
    } catch (e) {
      if (pageLeaving) return null;
      rememberJob(null);
      setCollectBusy(false);
      setCollectFailMsg(target, e && e.message);
      throw e;
    } finally {
      polling = false;
    }
  }

  async function runPipeline(moduleCodes, msgEl) {
    const target = msgEl || pipelineMsgEl();
    setCollectingMsg(target);
    const payload = { report_date: window.REPORT_DATE || null, async_mode: true };
    payload.window_hours = currentWindowHours();
    if (moduleCodes && moduleCodes.length) {
      payload.module_codes = moduleCodes;
    } else if (window.PIPELINE_MODULES && window.PIPELINE_MODULES.length) {
      payload.module_codes = window.PIPELINE_MODULES;
    }
    var eid = collectEntityId();
    if (eid) payload.entity_id = eid;
    const resp = await fetch("/api/v1/pipeline/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await resp.json();
    if (resp.status === 409) {
      const detail = data.detail || {};
      const existingId = detail.job_id;
      // 仅跟随本作用域冲突任务，不接管其它界面的采集
      if (existingId) {
        setCollectingMsg(target);
        return trackJob(existingId, target);
      }
      throw new Error(detail.message || data.detail || "任务冲突");
    }
    if (!resp.ok) {
      throw new Error(apiErrorMessage(data.detail, "执行失败"));
    }
    if (data.async_mode && data.job_id) {
      setCollectingMsg(target);
      return trackJob(data.job_id, target);
    }
    setRefreshSuccessMsg(target, null);
    setTimeout(function () { window.location.reload(); }, 1200);
    return data;
  }

  if (pipelineBtn) {
    pipelineBtn.addEventListener("click", async function () {
      const msg = pipelineMsgEl();
      setCollectBusy(true);
      try {
        await runPipeline(null, msg);
      } catch (e) {
        setCollectFailMsg(msg, e && e.message);
        setCollectBusy(false);
      }
    });
  }

  document.querySelectorAll(".btn-reload-module").forEach(function (btn) {
    btn.addEventListener("click", async function () {
      const code = btn.getAttribute("data-module");
      const panel = document.getElementById("module-panel-" + code);
      const statusBox = panel ? panel.querySelector(".module-status") : null;
      setCollectBusy(true);
      if (statusBox) {
        const p = statusBox.querySelector(".empty-module");
        if (p) p.textContent = "正在重新加载…";
      }
      try {
        await runPipeline([code], pipelineMsgEl());
      } catch (e) {
        setCollectBusy(false);
        if (statusBox) {
          const p = statusBox.querySelector(".empty-module");
          if (p) p.textContent = "请求失败，请重新加载";
        }
        alert("重新加载失败: " + e.message);
      }
    });
  });

  function setActiveModuleJump(code) {
    document.querySelectorAll("a.mod-jump").forEach(function (link) {
      link.classList.toggle("active", link.getAttribute("data-module") === code);
    });
  }

  function scrollToModulePanel(code) {
    if (window.ACTIVE_PAGE === "news_7x24" && typeof window.filterNewsTimeline === "function") {
      window.filterNewsTimeline(code);
      var timeline = document.querySelector(".news-timeline");
      if (timeline) timeline.scrollIntoView({ behavior: "smooth", block: "start" });
      return true;
    }
    var panel = document.getElementById("module-panel-" + code);
    if (!panel) return false;
    // 从侧栏切换板块时优先恢复全量视图，避免先前的风险筛选将目标板块
    // 隐藏后，只剩空白内容区。
    if (panel.hidden) {
      if (typeof window.clearNewsRiskFilters === "function") window.clearNewsRiskFilters();
      panel.hidden = false;
    }
    panel.scrollIntoView({ behavior: "smooth", block: "start" });
    return true;
  }

  function moduleJumpUrl(code) {
    var rail = document.querySelector(".page-rail[data-page-path]");
    var pagePath = (rail && rail.getAttribute("data-page-path")) || window.location.pathname;
    var reportDate = window.REPORT_DATE || "";
    var qs = reportDate ? "?report_date=" + encodeURIComponent(reportDate) : "";
    return pagePath + qs + "#module-panel-" + encodeURIComponent(code);
  }

  document.querySelectorAll("a.mod-jump").forEach(function (link) {
    link.addEventListener("click", function (ev) {
      var code = link.getAttribute("data-module");
      if (!code) return;
      if (scrollToModulePanel(code)) {
        ev.preventDefault();
        setActiveModuleJump(code);
        if (history.replaceState) {
          history.replaceState(null, "", "#module-panel-" + code);
        }
        return;
      }
      // 当前筛选未展示该模块时，回到全量视图再定位
      ev.preventDefault();
      window.location.href = moduleJumpUrl(code);
    });
  });

  (function scrollToHashModulePanel() {
    var hash = window.location.hash || "";
    var match = hash.match(/^#module-panel-(.+)$/);
    var first = document.querySelector("a.mod-jump");
    if (match) {
      var code = decodeURIComponent(match[1]);
      setActiveModuleJump(code);
      setTimeout(function () {
        scrollToModulePanel(code);
      }, 50);
      return;
    }
    if (first) setActiveModuleJump(first.getAttribute("data-module"));
  })();

  // 页面加载时若有本作用域未完成任务，恢复轮询（不阻断侧栏/其它界面）
  whenPageGlobalsReady(async function resumeActiveJob() {
    try {
      const resp = await fetch(runningQueryUrl());
      const cur = await resp.json();
      let jobId = cur && cur.job_id ? cur.job_id : null;
      if (!jobId) {
        try { jobId = sessionStorage.getItem(jobStorageKey()); } catch (e) { jobId = null; }
      }
      // 兼容旧版全局 key，避免遗留“采集中”幽灵轮询
      if (!jobId) {
        try {
          jobId = sessionStorage.getItem("pipeline_active_job_id");
          if (jobId) sessionStorage.removeItem("pipeline_active_job_id");
        } catch (e) { jobId = null; }
      }
      if (!jobId) return;

      // 确认任务仍在排队/运行（服务重启后遗留 id 会被清掉）
      const jr = await fetch("/api/v1/pipeline/jobs/" + jobId);
      const jd = await jr.json();
      if (!jr.ok || (jd.status !== "queued" && jd.status !== "running")) {
        rememberJob(null);
        // 任务已结束：清掉可能残留的「采集中...」文案
        try {
          var stale = sessionStorage.getItem(refreshMsgKey());
          if (stale && stale.indexOf("采集中") === 0) {
            sessionStorage.removeItem(refreshMsgKey());
            var msgDone = pipelineMsgEl();
            if (msgDone && msgDone.textContent && msgDone.textContent.indexOf("采集中") === 0) {
              if (jd.status === "completed" && jd.finished_at) {
                setRefreshSuccessMsg(msgDone, jd.finished_at);
              } else if (jd.status === "failed") {
                setCollectFailMsg(msgDone);
              }
            }
          }
        } catch (e2) { /* ignore */ }
        return;
      }
      // 任务作用域须与当前页一致，避免接管其它界面采集
      var expected = collectScopeKey();
      var jobScope = jd.scope || (jd.snapshot && jd.snapshot.scope) || null;
      if (jobScope && jobScope !== expected) {
        rememberJob(null);
        return;
      }
      if (!jobScope) {
        var jobHours = Number(jd.window_hours || 24);
        var jobEntity = jd.entity_id != null ? Number(jd.entity_id) : null;
        if (jobEntity && jobEntity !== currentEntityId()) {
          rememberJob(null);
          return;
        }
        if (!jobEntity && jobHours !== currentWindowHours()) {
          rememberJob(null);
          return;
        }
      }

      const msg = pipelineMsgEl();
      setCollectingMsg(msg);
      trackJob(jobId, msg).catch(function () { /* 已在 trackJob 内提示 */ });
    } catch (e) { /* ignore */ }
  });

  // 后台整点采集：数据采完后刷新主内容；手动采集中则不打断
  whenPageGlobalsReady(function syncBackgroundRefreshTime() {
    var page = window.ACTIVE_PAGE || "";
    if (page !== "daily_news" && page !== "news_7x24") return;
    if (currentEntityId()) return;

    var lastFinishedAt = null;
    var bootstrapped = false;
    var CONTENT_REFRESH_KEY = "pipeline_content_refreshed_at:" + collectScopeKey();

    function isUserCollectingMsg(text) {
      var t = String(text || "");
      return t.indexOf("采集中") === 0 || t.indexOf("采集失败") === 0;
    }

    function userCollectBusy() {
      if (polling) return true;
      var msg = pipelineMsgEl();
      return !!(msg && isUserCollectingMsg(msg.textContent));
    }

    function softReloadForNewData(finishedAt) {
      try {
        var prev = sessionStorage.getItem(CONTENT_REFRESH_KEY);
        if (prev && prev === String(finishedAt)) return;
        sessionStorage.setItem(CONTENT_REFRESH_KEY, String(finishedAt));
      } catch (e) { /* ignore */ }
      // 仅刷新当前页主内容；不改 URL，不抢手动采集，不打断跳转
      if (!pageLeaving) window.location.reload();
    }

    async function tick() {
      if (userCollectBusy()) return;
      try {
        var hours = currentWindowHours() || 24;
        var codes = (currentModuleCodes() || []).join(",") || "B,C,D";
        var resp = await fetch(
          "/api/v1/pipeline/last-refresh?window_hours=" +
            encodeURIComponent(hours) +
            "&module_codes=" +
            encodeURIComponent(codes)
        );
        var data = await resp.json();
        if (!resp.ok || !data) return;
        // 后台整点任务进行中：不锁按钮、不改「采集中」文案，等结束后再同步
        if (data.running) return;
        if (!data.finished_at) return;
        if (userCollectBusy()) return;

        var finishedAt = String(data.finished_at);
        var msg = pipelineMsgEl();
        setRefreshSuccessMsg(msg, finishedAt);

        if (!bootstrapped) {
          bootstrapped = true;
          lastFinishedAt = finishedAt;
          return;
        }
        if (lastFinishedAt && lastFinishedAt !== finishedAt) {
          lastFinishedAt = finishedAt;
          softReloadForNewData(finishedAt);
          return;
        }
        lastFinishedAt = finishedAt;
      } catch (e) { /* ignore */ }
    }

    tick();
    setInterval(tick, 30000);
  });

  // 7×24：某日无快照时自动补采并写回数据库
  whenPageGlobalsReady(function autoBackfillDaySnapshot() {
    if (!window.AUTO_BACKFILL) return;
    if (window.ACTIVE_PAGE !== "news_7x24") return;
    var key = "news_7x24_backfill:" + (window.REPORT_DATE || "");
    try {
      if (sessionStorage.getItem(key) === "1") return;
      sessionStorage.setItem(key, "1");
    } catch (e) { /* ignore */ }
    var msg = pipelineMsgEl();
    if (msg) setCollectingMsg(msg);
    setCollectBusy(true);
    runPipeline(null, msg).catch(function () {
      setCollectBusy(false);
      setCollectFailMsg(msg, e && e.message);
      try { sessionStorage.removeItem(key); } catch (e2) { /* ignore */ }
    });
  });

  renderEntryCharts();

  function positionSidebarFooter() {
    var rail = document.querySelector(".app-shell > .page-rail");
    var footer = document.querySelector(".daily-sidebar-footer");
    if (!rail || !footer || window.innerWidth <= 640) return;
    var rect = rail.getBoundingClientRect();
    rail.style.setProperty("--daily-sidebar-footer-left", rect.left + "px");
    rail.style.setProperty("--daily-sidebar-footer-width", rect.width + "px");
  }
  positionSidebarFooter();
  window.addEventListener("resize", positionSidebarFooter);

  document.querySelectorAll(".daily-index-title[aria-controls]").forEach(function (btn) {
    if (btn.id === "btn-toggle-sectors") return;
    if (btn.classList.contains("entity-index-group-head")) return;
    var list = document.getElementById(btn.getAttribute("aria-controls"));
    if (!list) return;
    btn.addEventListener("click", function () {
      var collapsed = list.classList.toggle("is-collapsed");
      btn.classList.toggle("is-collapsed", collapsed);
      btn.setAttribute("aria-expanded", String(!collapsed));
    });
  });
})();
