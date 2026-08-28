/*
 * 主体评估财务对比图表
 * 负责：将后端已核验的同口径披露数据按指标组渲染为“本次 vs 上次”柱状图。
 * 修改记录：2026-08-25 | DingJiaye
 */
(function () {
  "use strict";

  var instances = [];

  function readMetrics() {
    var node = document.getElementById("entity-financial-chart-data");
    if (!node) return [];
    try { return JSON.parse(node.textContent || "[]"); } catch (_) { return []; }
  }

  function groupMetrics(metrics) {
    return metrics.reduce(function (groups, item) {
      var name = item.chart_group || "财务指标";
      (groups[name] = groups[name] || []).push(item);
      return groups;
    }, {});
  }

  function optionFor(rows, chartType) {
    return {
      grid: { left: 54, right: 16, top: 34, bottom: 50 },
      tooltip: { trigger: "axis", axisPointer: { type: chartType === "line" ? "line" : "shadow" } },
      legend: { top: 4, right: 2, textStyle: { color: "#728196", fontSize: 11 } },
      xAxis: {
        type: "category",
        data: rows.map(function (item) { return item.label; }),
        axisLabel: { color: "#68788c", fontSize: 10, interval: 0, rotate: rows.length > 4 ? 20 : 0 },
        axisLine: { lineStyle: { color: "#dbe3ec" } }
      },
      yAxis: {
        type: "value",
        axisLabel: { color: "#8491a1", fontSize: 10 },
        splitLine: { lineStyle: { color: "#edf1f5" } }
      },
      series: [
        {
          name: rows[0].previous_as_of || "上次披露",
          type: chartType,
          data: rows.map(function (item) { return item.previous_numeric; }),
          smooth: chartType === "line",
          symbol: chartType === "line" ? "circle" : "none",
          itemStyle: { color: "#b9c9dd", borderRadius: [3, 3, 0, 0] },
          lineStyle: { width: 2 },
          barMaxWidth: 34
        },
        {
          name: rows[0].as_of || "本次披露",
          type: chartType,
          data: rows.map(function (item) { return item.current_numeric; }),
          smooth: chartType === "line",
          symbol: chartType === "line" ? "circle" : "none",
          itemStyle: { color: "#557fae", borderRadius: [3, 3, 0, 0] },
          lineStyle: { width: 2 },
          barMaxWidth: 34
        }
      ]
    };
  }

  function chartModes(rows) {
    // 两期横向披露适合柱状图；仅连续三期及以上的同一时间序列才提供折线切换。
    // 目前主体配置是“本次 vs 上次”两期比较，不应把不同财务科目误画成趋势线。
    return rows.length === 1 && Array.isArray(rows[0].series) && rows[0].series.length >= 3
      ? ["bar", "line"]
      : ["bar"];
  }

  function render() {
    var root = document.getElementById("entity-financial-charts");
    var metrics = readMetrics();
    if (!root || !metrics.length || !window.echarts) return;
    root.innerHTML = "";
    var groups = groupMetrics(metrics);
    Object.keys(groups).forEach(function (groupName) {
      var rows = groups[groupName];
      var card = document.createElement("section");
      var modes = chartModes(rows);
      card.className = "fin-chart-card";
      card.innerHTML = '<header class="fin-chart-card-head"><h5></h5>' + (modes.length > 1 ? '<div class="fin-chart-toggle" role="group" aria-label="图表类型"><button type="button" class="active" data-type="bar">柱状</button><button type="button" data-type="line">折线</button></div>' : '') + '</header><div class="fin-chart-host"></div>';
      card.querySelector("h5").textContent = groupName;
      root.appendChild(card);
      var host = card.querySelector(".fin-chart-host");
      var chart = window.echarts.init(host);
      instances.push(chart);
      chart.setOption(optionFor(rows, "bar"));
      card.querySelectorAll("button[data-type]").forEach(function (button) {
        button.addEventListener("click", function () {
          var type = button.getAttribute("data-type") || "bar";
          card.querySelectorAll("button[data-type]").forEach(function (item) {
            item.classList.toggle("active", item === button);
          });
          chart.setOption(optionFor(rows, type), true);
        });
      });
    });
  }

  window.addEventListener("resize", function () {
    instances.forEach(function (chart) { chart.resize(); });
  });
  // Modified by DingJiaye: 2026-08-26 — 财务面板异步替换后重新绘制图表。
  window.renderEntityFinancialCharts = render;
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", render);
  else render();
})();
