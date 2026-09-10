/*
 * 主体风险工作台
 * 负责：风险趋势、关系图谱与财务 Excel 导入。
 * Modified by DingJiaye: 2026-09-01.
 */
(function () {
  "use strict";
  var entityId = window.ENTITY_ID;
  if (!entityId) return;

  var api = "/api/v1/entities/" + encodeURIComponent(entityId);
  var $ = function (selector, root) { return (root || document).querySelector(selector); };
  var $$ = function (selector, root) { return Array.prototype.slice.call((root || document).querySelectorAll(selector)); };

  function escapeHtml(value) {
    return String(value == null ? "" : value).replace(/[&<>'"]/g, function (ch) {
      return {"&":"&amp;", "<":"&lt;", ">":"&gt;", "'":"&#39;", '"':"&quot;"}[ch];
    });
  }
  function severityClass(value) { return value === "风险" ? "risk" : value === "关注" ? "watch" : "neutral"; }
  async function fetchJson(url, options) {
    var resp = await fetch(url, options);
    var data = await resp.json().catch(function () { return {}; });
    if (!resp.ok) throw new Error(data.detail || data.message || ("请求失败 (" + resp.status + ")"));
    return data;
  }
  function showDialog(id) { var d = document.getElementById(id); if (d) d.showModal(); }
  function closeDialog(id) { var d = document.getElementById(id); if (d) d.close(); }
  $$('[data-dialog-close]').forEach(function (button) { button.addEventListener("click", function () { closeDialog(button.getAttribute("data-dialog-close")); }); });

  var trendChart;
  function trendSeries(data, key) {
    return (data.points || []).map(function (point) { return Number(point[key] || 0); });
  }
  function updateTrendMetrics(data, days) {
    var points = (data && data.points) || [];
    var events = points.reduce(function (sum, point) { return sum + Number(point.events || 0); }, 0);
    var watch = points.reduce(function (sum, point) { return sum + Number(point.watch || 0); }, 0);
    var risk = points.reduce(function (sum, point) { return sum + Number(point.risk || 0); }, 0);
    var set = function (id, value) { var node = document.getElementById(id); if (node) node.textContent = String(value); };
    set("entity-risk-events-count", events);
    set("entity-risk-watch-count", watch);
    set("entity-risk-red-count", risk);
    set("entity-risk-period-label", "近 " + days + " 天公开事件");
    var status = document.getElementById("entity-risk-status-text");
    var findings = watch + risk;
    if (status) status.textContent = findings > 0 ? ("发现 " + findings + " 条需关注事件") : "当前未发现需关注事件";
  }
  function trendFallback(holder, data, errorMessage) {
    var points = (data && data.points) || [];
    var normal = trendSeries(data || {}, "normal");
    var watch = trendSeries(data || {}, "watch");
    var risk = trendSeries(data || {}, "risk");
    var max = Math.max.apply(null, [1].concat(normal, watch, risk));
    var width = 760, height = 190, padX = 24, padY = 22;
    function polyline(values, color) {
      if (!values.length) return "";
      var step = values.length > 1 ? (width - padX * 2) / (values.length - 1) : 0;
      return '<polyline fill="none" stroke="' + color + '" stroke-width="2" points="' + values.map(function (value, index) {
        return (padX + step * index).toFixed(1) + ',' + (height - padY - (value / max) * (height - padY * 2)).toFixed(1);
      }).join(" ") + '" />';
    }
    var hasEvents = normal.concat(watch, risk).some(function (value) { return value > 0; });
    if (!hasEvents) {
      holder.innerHTML = '<p class="workbench-empty">当前时间范围内暂无可展示的公开信息事件</p>';
      return;
    }
    holder.innerHTML = '<div class="trend-fallback-legend"><span class="normal">普通</span><span class="watch">关注</span><span class="risk">风险</span></div>' +
      '<svg class="trend-fallback-svg" viewBox="0 0 ' + width + ' ' + height + '" preserveAspectRatio="none" role="img" aria-label="主体风险趋势图">' +
      '<line x1="' + padX + '" y1="' + (height - padY) + '" x2="' + (width - padX) + '" y2="' + (height - padY) + '" stroke="#d9e5ee" />' +
      polyline(normal, "#9aa8b7") + polyline(watch, "#d9a72d") + polyline(risk, "#ca5c5c") + '</svg>' +
      '<div class="trend-fallback-labels"><span>' + escapeHtml(String(points[0] && points[0].date || "").slice(5)) + '</span><span>' + escapeHtml(String(points[points.length - 1] && points[points.length - 1].date || "").slice(5)) + '</span></div>' +
      (errorMessage ? '<p class="workbench-note">图表组件未加载，已显示基础趋势。</p>' : '');
  }
  async function loadTrend(days) {
    var holder = $("#entity-risk-trend-chart");
    if (!holder) return;
    holder.classList.add("is-loading");
    try {
      var query = "?days=" + encodeURIComponent(days);
      if (window.REPORT_DATE) query += "&report_date=" + encodeURIComponent(window.REPORT_DATE);
      var data = await fetchJson(api + "/risk-trends" + query);
      updateTrendMetrics(data, days);
      if (!window.echarts) {
        trendFallback(holder, data, true);
        return;
      }
      trendChart = trendChart || window.echarts.init(holder);
      var labels = data.points.map(function (p) { return String(p.date).slice(5); });
      var normal = trendSeries(data, "normal");
      var watch = trendSeries(data, "watch");
      var risk = trendSeries(data, "risk");
      var hasEvents = normal.concat(watch, risk).some(function (value) { return value > 0; });
      trendChart.setOption({
        animationDuration: 420,
        animationEasing: "cubicOut",
        grid: {left: 40, right: 18, top: 36, bottom: 32, containLabel: true},
        tooltip: {trigger: "axis", backgroundColor: "rgba(17,43,68,.95)", textStyle: {color: "#fff"}},
        legend: {top: 0, right: 4, itemWidth: 10, itemHeight: 10, textStyle: {color: "#637d96", fontSize: 11}, data: ["普通", "关注", "风险"]},
        xAxis: {type: "category", boundaryGap: false, data: labels, axisLine: {lineStyle: {color: "#d7e3ed"}}, axisLabel: {color: "#70869b", fontSize: 10}},
        yAxis: {type: "value", minInterval: 1, splitLine: {lineStyle: {color: "#edf2f6"}}, axisLabel: {color: "#70869b", fontSize: 10}},
        series: [
          {name: "普通", type: "line", smooth: true, showSymbol: false, lineStyle: {width: 2, color: "#9aa8b7"}, areaStyle: {color: "rgba(154,168,183,.08)"}, data: normal},
          {name: "关注", type: "line", smooth: true, showSymbol: false, lineStyle: {width: 2, color: "#d9a72d"}, areaStyle: {color: "rgba(217,167,45,.08)"}, data: watch},
          {name: "风险", type: "line", smooth: true, showSymbol: false, lineStyle: {width: 2, color: "#ca5c5c"}, areaStyle: {color: "rgba(202,92,92,.08)"}, data: risk}
        ],
        graphic: hasEvents ? [] : [{type: "text", left: "center", top: "middle", style: {text: "当前时间范围内暂无可展示的公开信息事件", fill: "#8193a3", fontSize: 12}}]
      }, true);
      requestAnimationFrame(function () { if (trendChart) trendChart.resize(); });
    } catch (err) { holder.innerHTML = '<p class="workbench-error">趋势加载失败：' + escapeHtml(err.message) + "</p>"; }
    finally { holder.classList.remove("is-loading"); }
  }
  $$("[data-trend-days]").forEach(function (button) { button.addEventListener("click", function () {
    $$("[data-trend-days]").forEach(function (item) { item.classList.toggle("active", item === button); });
    loadTrend(button.getAttribute("data-trend-days"));
  }); });

  var relationChart;
  function renderRelations(rows) {
    var holder = $("#entity-relation-graph"), list = $("#entity-relation-list"); if (!holder || !list) return;
    list.innerHTML = rows.length ? rows.map(function (row) { return '<article class="relation-list-item"><div><span class="review-severity ' + severityClass(row.risk_signal) + '">' + escapeHtml(row.related_type) + '</span><strong>' + escapeHtml(row.related_name) + '</strong><p>' + escapeHtml(row.relationship_type) + (row.ownership_pct != null ? ' · ' + row.ownership_pct + '%' : '') + (row.country_or_region ? ' · ' + escapeHtml(row.country_or_region) : '') + '</p></div><button type="button" data-relation-delete="' + row.id + '">删除</button></article>'; }).join("") : '<p class="workbench-empty">尚未维护关联关系。可添加股东、供应商、融资银行或国家地区。</p>';
    $$('[data-relation-delete]', list).forEach(function (button) { button.addEventListener("click", async function () { if (!confirm("删除这条关联关系？")) return; await fetchJson(api + "/relationships/" + button.getAttribute("data-relation-delete"), {method:"DELETE"}); loadRelations(); }); });
    if (!window.echarts) return;
    relationChart = relationChart || window.echarts.init(holder);
    var centerName = window.ENTITY_NAME || "监测主体";
    var categories = ["监测主体", "股东", "关联方", "供应商", "融资银行", "国家地区", "子公司"];
    var nodes = [{name:centerName, value:centerName, symbolSize:56, category:0, itemStyle:{color:"#285f8f", borderColor:"#bdd7ed", borderWidth:2}, label:{color:"#143a5b", fontWeight:700, fontSize:10}}];
    rows.forEach(function (row) { nodes.push({name:row.related_name, value:row.related_type, symbolSize:30, category:Math.max(1, categories.indexOf(row.related_type)), itemStyle:{color: row.risk_signal === "风险" ? "#ca6a6a" : row.risk_signal === "关注" ? "#d6ad46" : "#7b9bb8"}}); });
    relationChart.setOption({
      animationDuration: 420,
      animationEasing: "cubicOut",
      tooltip: {formatter: function (p) { return p.data.name + "<br/>" + (p.data.value || ""); }},
      series: [{
        type: "graph", layout: "force", roam: true, draggable: true, data: nodes,
        links: rows.map(function (row) {
          return {source: centerName, target: row.related_name, label: {show: true, formatter: row.relationship_type, color: "#66829b", fontSize: 9}};
        }),
        categories: categories.map(function (name) { return {name: name}; }),
        force: {repulsion: 155, edgeLength: [48, 98]},
        lineStyle: {color: "#9eb8cf", width: 1.2, curveness: .08},
        label: {show: true, position: "bottom", color: "#355b79", fontSize: 10},
        emphasis: {focus: "adjacency"}
      }]
    }, true);
  }
  async function loadRelations() { try { renderRelations(await fetchJson(api + "/relationships")); } catch (err) { var list = $("#entity-relation-list"); if (list) list.innerHTML='<p class="workbench-error">关系图谱加载失败：' + escapeHtml(err.message) + "</p>"; } }
  var relationshipOpen = $("#relationship-open"); if (relationshipOpen) relationshipOpen.addEventListener("click", function () { showDialog("relationship-dialog"); });
  var relationshipForm = $("#relationship-form"); if (relationshipForm) relationshipForm.addEventListener("submit", async function (event) { event.preventDefault(); var fd=new FormData(relationshipForm), payload={}; ["related_name","related_type","relationship_type","country_or_region","risk_signal","notes","source_url"].forEach(function(k){var v=fd.get(k);if(v)payload[k]=v;}); var pct=fd.get("ownership_pct"); if(pct)payload.ownership_pct=Number(pct); try { await fetchJson(api + "/relationships", {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)}); closeDialog("relationship-dialog");relationshipForm.reset();await loadRelations(); } catch(err){alert("保存失败："+err.message);} });

  var financeOpen = $("#financial-import-open"); if (financeOpen) financeOpen.addEventListener("click", function(){showDialog("financial-import-dialog");});
  var financeForm = $("#financial-import-form"); if (financeForm) financeForm.addEventListener("submit", async function (event) { event.preventDefault(); var message=$("#financial-import-message"), submit=$("[type=submit]",financeForm); submit.disabled=true; if(message){message.hidden=false;message.textContent="正在读取并导入…";} try { var resp=await fetch(api+"/financial-records/import",{method:"POST",body:new FormData(financeForm)});var data=await resp.json().catch(function(){return{};});if(!resp.ok)throw new Error(data.detail||"导入失败");if(message)message.textContent=data.message;window.setTimeout(function(){window.location.reload();},600); } catch(err){if(message)message.textContent="导入失败："+err.message;} finally{submit.disabled=false;} });
  $$('[data-financial-record-delete]').forEach(function(button){button.addEventListener("click",async function(){if(!confirm("删除该导入指标？"))return;try{await fetchJson(api+"/financial-records/"+button.getAttribute("data-financial-record-delete"),{method:"DELETE"});window.location.reload();}catch(err){alert("删除失败："+err.message);}});});

  window.addEventListener("resize", function(){ if(trendChart)trendChart.resize();if(relationChart)relationChart.resize(); });
  loadTrend(30); loadRelations();
})();
