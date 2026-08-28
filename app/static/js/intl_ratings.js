/**
 * 国际评级页面：从 /api/v1/intl-ratings 拉取快照；手动更新触发后台流水线。
 */
(function () {
  "use strict";

  var CATEGORY_SIMPLE = "简易分类债券";
  var CATEGORY_NON_SIMPLE = "非简易分类债券";
  var API = "/api/v1/intl-ratings";

  var rows = [];
  var selectedId = null;
  var activeFilter = "all";
  var groupCollapsed = {};
  groupCollapsed[CATEGORY_SIMPLE] = false;
  groupCollapsed[CATEGORY_NON_SIMPLE] = false;
  var pollTimer = null;

  var els = {
    search: document.getElementById("ir-search-input"),
    clear: document.getElementById("ir-search-clear"),
    tree: document.getElementById("ir-tree"),
    empty: document.getElementById("ir-search-empty"),
    tbody: document.getElementById("ir-table-body"),
    wrap: document.getElementById("ir-table-wrap"),
    msg: document.getElementById("ir-action-msg"),
    btnImport: document.getElementById("btn-ir-import"),
    inputExcel: document.getElementById("ir-excel-file"),
    btnRefresh: document.getElementById("btn-ir-refresh"),
    btnExport: document.getElementById("btn-ir-export"),
    headerStatus: document.getElementById("ir-header-status"),
    statTotal: document.getElementById("ir-stat-total"),
    statRated: document.getElementById("ir-stat-rated"),
    statChanges: document.getElementById("ir-stat-changes"),
    statAlerts: document.getElementById("ir-stat-alerts"),
  };

  function setMsg(text, isError) {
    if (!els.msg) return;
    els.msg.textContent = text || "";
    els.msg.classList.toggle("is-error", !!isError);
  }

  function setLoading(on) {
    [els.btnRefresh, els.btnImport].forEach(function (button) {
      if (!button) return;
      button.classList.toggle("is-loading", !!on);
      button.disabled = !!on;
    });
  }

  function getQuery() {
    return (els.search && els.search.value ? els.search.value : "").trim().toLowerCase();
  }

  function filteredRows() {
    var q = getQuery();
    return rows.filter(function (r) {
      var matchesSearch = !q || (r.issuer || "").toLowerCase().indexOf(q) !== -1;
      return matchesSearch && matchesFilter(r);
    });
  }

  function text(v) { return String(v == null ? "" : v).trim(); }
  function isRated(r) { return [r.moodys, r.sp, r.fitch].some(function (v) { return text(v) && text(v).toUpperCase() !== "NR" && text(v) !== "—"; }); }
  function isChanged(r) { return /是|yes|变动|上调|下调|调整/i.test(text(r.ratingChanged)); }
  function hasMarketAlert(r) {
    var value = text(r.priceDrop);
    // “未见市场预警”是正常状态，不能因包含“预警”二字被误计入市场预警。
    if (!value || /^(未见|无公开|暂无|待接入|需人工复核)/.test(value)) return false;
    return /是|yes|下跌|跌幅|风险|预警|近\s*\d+\s*[日月].*-/i.test(value);
  }
  function matchesFilter(r) {
    if (activeFilter === "rated") return isRated(r);
    if (activeFilter === "changed") return isChanged(r);
    if (activeFilter === "market") return hasMarketAlert(r);
    return true;
  }

  function groupRows(list) {
    var groups = [
      { name: CATEGORY_SIMPLE, items: [] },
      { name: CATEGORY_NON_SIMPLE, items: [] },
    ];
    list.forEach(function (r) {
      if (r.category === CATEGORY_SIMPLE) groups[0].items.push(r);
      else groups[1].items.push(r);
    });
    return groups;
  }

  function escapeHtml(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function cellHtml(text, extraClass) {
    var t = text == null || text === "" ? "—" : String(text);
    var cls = "ir-cell" + (extraClass ? " " + extraClass : "");
    return (
      '<td class="' +
      cls +
      '" title="' +
      escapeHtml(t) +
      '"><span class="ir-cell-text">' +
      escapeHtml(t) +
      "</span></td>"
    );
  }

  function ratingPackHtml(r) {
    var values = [["穆", r.moodys], ["标", r.sp], ["惠", r.fitch]];
    return '<td class="ir-cell col-rating-pack"><div class="ir-rating-pack">' + values.map(function (pair) {
      var rating = text(pair[1]) || "NR";
      var isNr = rating.toUpperCase() === "NR" || rating === "—";
      return '<span class="ir-rating-pill' + (isNr ? ' is-nr' : '') + '"><b>' + pair[0] + '</b>' + escapeHtml(rating) + '</span>';
    }).join("") + "</div></td>";
  }

  function signalHtml(value, emptyText) {
    var v = text(value);
    if (!v || v === "否" || v.toLowerCase() === "no" || v === "无") return '<span class="ir-signal muted">' + escapeHtml(emptyText || "无异常") + "</span>";
    if (/^(未见|无公开交易数据|暂无公开行情|待接入|需人工复核)/i.test(v)) return '<span class="ir-signal muted">' + escapeHtml(v) + "</span>";
    return '<span class="ir-signal alert">' + escapeHtml(v) + "</span>";
  }

  function marketSignalHtml(value) {
    var v = text(value);
    if (!v || v === "否" || v.toLowerCase() === "no" || v === "无") return '<span class="ir-signal muted">未见异常</span>';
    if (/^(未见|无公开交易数据|暂无公开行情|待接入|需人工复核)/i.test(v)) return '<span class="ir-signal muted">' + escapeHtml(v) + "</span>";
    if (/跌幅超过|风险|预警|近\s*\d+\s*[日月].*-/i.test(v)) return '<span class="ir-signal alert">' + escapeHtml(v) + "</span>";
    return '<span class="ir-signal muted">' + escapeHtml(v) + "</span>";
  }

  function renderTable(list) {
    if (!els.tbody) return;
    if (!list.length) {
      els.tbody.innerHTML =
        '<tr class="ir-empty-row"><td colspan="5">暂无匹配数据</td></tr>';
      return;
    }
    els.tbody.innerHTML = list
      .map(function (r) {
        var active = r.id === selectedId ? " is-active" : "";
        return (
          '<tr class="ir-row' +
          active +
          '" data-issuer-id="' +
          escapeHtml(r.id) +
          '" id="row-' +
          escapeHtml(r.id) +
          '">' +
          cellHtml(r.issuer, "col-issuer") +
          ratingPackHtml(r) +
          '<td class="ir-cell col-change">' + signalHtml(r.ratingChanged, "未见变动") + "</td>" +
          '<td class="ir-cell col-signal">' + marketSignalHtml(r.priceDrop) + "</td>" +
          '<td class="ir-cell col-rss">' +
          (r.ratingSourceUrl ? '<button type="button" class="btn small ir-rating-source-btn" data-url="' + escapeHtml(r.ratingSourceUrl) + '">评级来源</button>' : '') +
          (r.rssUrl ? '<button type="button" class="btn small ir-rss-btn" data-issuer-id="' + escapeHtml(r.id) + '">市场来源</button>' : '') +
          (!r.ratingSourceUrl && !r.rssUrl ? '<span class="ir-source-label" title="' + escapeHtml(r.sourceLabel || "待接入") + '">' + (r.sourceLabel ? 'Excel 导入' : '待接入') + '</span>' : '') +
          "</td>" +
          "</tr>"
        );
      })
      .join("");
  }

  function renderTree(list) {
    if (!els.tree) return;
    var groups = groupRows(list);
    var total = list.length;
    if (els.empty) {
      els.empty.hidden = total !== 0;
    }
    if (!total) {
      els.tree.innerHTML = "";
      return;
    }

    els.tree.innerHTML = groups
      .map(function (g) {
        if (!g.items.length && getQuery()) return "";
        var collapsed = !!groupCollapsed[g.name];
        var count = g.items.length;
        var itemsHtml = g.items
          .map(function (r) {
            var active = r.id === selectedId ? " active" : "";
            return (
              '<li role="none">' +
              '<button type="button" class="ir-tree-item' +
              active +
              '" role="treeitem" data-issuer-id="' +
              escapeHtml(r.id) +
              '" title="' +
              escapeHtml(r.issuer) +
              '">' +
              escapeHtml(r.issuer) +
              "</button></li>"
            );
          })
          .join("");
        return (
          '<div class="ir-tree-group' +
          (collapsed ? " is-collapsed" : "") +
          '" data-group="' +
          escapeHtml(g.name) +
          '">' +
          '<button type="button" class="ir-tree-group-title" data-toggle-group="' +
          escapeHtml(g.name) +
          '">' +
          '<span class="ir-tree-caret" aria-hidden="true"></span>' +
          escapeHtml(g.name) +
          ' <span class="ir-tree-count">' +
          count +
          "</span></button>" +
          '<ul class="ir-tree-list" role="group">' +
          itemsHtml +
          "</ul></div>"
        );
      })
      .join("");
  }

  function refresh() {
    var list = filteredRows();
    renderTree(list);
    renderTable(list);
    if (els.clear) {
      els.clear.hidden = !getQuery();
    }
  }

  function renderOverview() {
    var total = rows.length;
    var rated = rows.filter(isRated).length;
    var changed = rows.filter(isChanged).length;
    var alerts = rows.filter(hasMarketAlert).length;
    if (els.statTotal) els.statTotal.textContent = total;
    if (els.statRated) els.statRated.textContent = rated;
    if (els.statChanges) els.statChanges.textContent = changed;
    if (els.statAlerts) els.statAlerts.textContent = alerts;
  }

  function selectIssuer(issuerId, scroll) {
    selectedId = issuerId;
    document.querySelectorAll(".ir-tree-item").forEach(function (el) {
      el.classList.toggle("active", el.getAttribute("data-issuer-id") === issuerId);
    });
    document.querySelectorAll(".ir-row").forEach(function (el) {
      el.classList.toggle("is-active", el.getAttribute("data-issuer-id") === issuerId);
    });
    if (scroll) {
      var row = document.getElementById("row-" + issuerId);
      if (row) row.scrollIntoView({ behavior: "smooth", block: "center" });
    }
  }

  function handleOpenRssFeed(issuerId) {
    var row = rows.find(function (r) {
      return r.id === issuerId;
    });
    var name = row ? row.issuer : issuerId;
    var url = row && row.rssUrl ? row.rssUrl : "";
    if (url) {
      window.open(url, "_blank", "noopener,noreferrer");
      return;
    }
    setMsg("「" + name + "」暂无官方 RSS 链接，请后续在映射表补充。", true);
  }

  function exportExcel() {
    if (typeof XLSX === "undefined") {
      setMsg("导出组件未加载", true);
      return;
    }
    var list = filteredRows();
    if (!list.length) {
      setMsg("无数据可导出", true);
      return;
    }
    var header = [
      "发行体",
      "分类",
      "穆迪评级",
      "标普评级",
      "惠誉评级",
      "评级变动",
      "市场信号（近 1 个月）",
      "信息源",
    ];
    var data = [header].concat(
      list.map(function (r) {
        return [
          r.issuer,
          r.category,
          r.moodys,
          r.sp,
          r.fitch,
          r.ratingChanged,
          r.priceDrop,
          r.rssUrl || "",
        ];
      })
    );
    var ws = XLSX.utils.aoa_to_sheet(data);
    var wb = XLSX.utils.book_new();
    XLSX.utils.book_append_sheet(wb, ws, "国际评级");
    var stamp = new Date();
    var pad = function (n) {
      return n < 10 ? "0" + n : String(n);
    };
    var filename =
      "国际评级_" +
      stamp.getFullYear() +
      pad(stamp.getMonth() + 1) +
      pad(stamp.getDate()) +
      "_" +
      pad(stamp.getHours()) +
      pad(stamp.getMinutes()) +
      ".xlsx";
    XLSX.writeFile(wb, filename);
    setMsg("已导出 " + list.length + " 条记录：" + filename);
  }

  function applySnapshot(data) {
    rows = (data.rows || []).slice();
    renderOverview();
    refresh();
    var tip = data.message || "";
    if (data.updated_at) {
      tip = (tip ? tip + " " : "") + "更新于 " + formatUpdatedAt(data.updated_at) + " · 共 " + rows.length + " 家";
    } else if (!tip) {
      tip = "共 " + rows.length + " 家发行体";
    }
    setMsg(tip, data.source === "skeleton");
    if (els.headerStatus) {
      els.headerStatus.textContent = data.source === "manual_excel" ? "Excel 数据已导入" : (data.updated_at ? "快照已更新" : "等待评级数据接入");
    }
  }

  function formatUpdatedAt(value) {
    var time = new Date(value);
    if (isNaN(time.getTime())) return text(value);
    try {
      return new Intl.DateTimeFormat("sv-SE", {
        timeZone: "Asia/Tokyo",
        year: "numeric", month: "2-digit", day: "2-digit",
        hour: "2-digit", minute: "2-digit", hour12: false
      }).format(time).replace(" ", " ");
    } catch (_) {
      return time.getFullYear() + "-" + String(time.getMonth() + 1).padStart(2, "0") + "-" + String(time.getDate()).padStart(2, "0") + " " + String(time.getHours()).padStart(2, "0") + ":" + String(time.getMinutes()).padStart(2, "0");
    }
  }

  function loadSnapshot() {
    return fetch(API)
      .then(function (res) {
        if (!res.ok) throw new Error("HTTP " + res.status);
        return res.json();
      })
      .then(applySnapshot)
      .catch(function (err) {
        setMsg("加载评级数据失败：" + (err.message || err), true);
      });
  }

  function stopPoll() {
    if (pollTimer) {
      window.clearInterval(pollTimer);
      pollTimer = null;
    }
  }

  function pollJob(jobId) {
    stopPoll();
    pollTimer = window.setInterval(function () {
      fetch(API + "/jobs/" + encodeURIComponent(jobId))
        .then(function (res) {
          if (!res.ok) throw new Error("HTTP " + res.status);
          return res.json();
        })
        .then(function (job) {
          setMsg(job.message || job.status);
          if (job.status === "succeeded" || job.status === "failed") {
            stopPoll();
            setLoading(false);
            if (job.status === "failed") {
              setMsg("更新失败：" + (job.error || job.message), true);
            }
            return loadSnapshot().then(function () {
              if (job.status === "succeeded") {
                setMsg(job.message || "评级数据已更新");
              }
            });
          }
        })
        .catch(function (err) {
          stopPoll();
          setLoading(false);
          setMsg("查询任务失败：" + (err.message || err), true);
        });
    }, 2000);
  }

  function manualRefresh() {
    setLoading(true);
    setMsg("正在启动评级流水线（快速模式）…");
    // quick=true：网页刷新跳过 Playwright，避免长时间阻塞
    fetch(API + "/refresh?quick=true", { method: "POST" })
      .then(function (res) {
        if (!res.ok) throw new Error("HTTP " + res.status);
        return res.json();
      })
      .then(function (data) {
        setMsg(data.message || "任务已启动");
        if (data.job_id) {
          pollJob(data.job_id);
        } else {
          setLoading(false);
        }
      })
      .catch(function (err) {
        setLoading(false);
        setMsg("启动刷新失败：" + (err.message || err), true);
      });
  }

  function importExcel(file) {
    if (!file) return;
    if (!/\.xlsx$/i.test(file.name || "")) {
      setMsg("请上传 .xlsx 格式的评级台账", true);
      return;
    }
    var body = new FormData();
    body.append("file", file, file.name);
    setLoading(true);
    setMsg("正在读取 Excel 并生成评级快照…");
    fetch(API + "/import-excel", { method: "POST", body: body })
      .then(function (res) {
        return res.json().then(function (data) {
          if (!res.ok) throw new Error(data.detail || ("HTTP " + res.status));
          return data;
        });
      })
      .then(function (data) {
        return loadSnapshot().then(function () {
          setMsg(data.message || "Excel 导入完成");
        });
      })
      .catch(function (err) {
        setMsg("导入失败：" + (err.message || err), true);
      })
      .finally(function () {
        setLoading(false);
        if (els.inputExcel) els.inputExcel.value = "";
      });
  }

  function bindEvents() {
    if (els.search) {
      els.search.addEventListener("input", function () {
        refresh();
      });
    }
    if (els.clear) {
      els.clear.addEventListener("click", function () {
        if (els.search) els.search.value = "";
        refresh();
        if (els.search) els.search.focus();
      });
    }
    if (els.tree) {
      els.tree.addEventListener("click", function (ev) {
        var toggle = ev.target.closest("[data-toggle-group]");
        if (toggle) {
          var g = toggle.getAttribute("data-toggle-group");
          groupCollapsed[g] = !groupCollapsed[g];
          renderTree(filteredRows());
          return;
        }
        var item = ev.target.closest(".ir-tree-item");
        if (item) {
          selectIssuer(item.getAttribute("data-issuer-id"), true);
        }
      });
    }
    if (els.tbody) {
      els.tbody.addEventListener("click", function (ev) {
        var ratingBtn = ev.target.closest(".ir-rating-source-btn");
        if (ratingBtn) {
          ev.stopPropagation();
          window.open(ratingBtn.getAttribute("data-url"), "_blank", "noopener,noreferrer");
          return;
        }
        var rssBtn = ev.target.closest(".ir-rss-btn");
        if (rssBtn) {
          ev.stopPropagation();
          handleOpenRssFeed(rssBtn.getAttribute("data-issuer-id"));
          return;
        }
        var row = ev.target.closest(".ir-row");
        if (row) {
          selectIssuer(row.getAttribute("data-issuer-id"), false);
        }
      });
    }
    if (els.btnRefresh) {
      els.btnRefresh.addEventListener("click", manualRefresh);
    }
    if (els.btnImport && els.inputExcel) {
      els.btnImport.addEventListener("click", function () { els.inputExcel.click(); });
      els.inputExcel.addEventListener("change", function () { importExcel(els.inputExcel.files && els.inputExcel.files[0]); });
    }
    if (els.btnExport) {
      els.btnExport.addEventListener("click", exportExcel);
    }
    document.querySelectorAll(".ir-filter").forEach(function (button) {
      button.addEventListener("click", function () {
        activeFilter = button.getAttribute("data-ir-filter") || "all";
        document.querySelectorAll(".ir-filter").forEach(function (item) {
          item.classList.toggle("active", item === button);
        });
        refresh();
      });
    });
  }

  bindEvents();
  setMsg("正在加载评级数据…");
  loadSnapshot();
})();
