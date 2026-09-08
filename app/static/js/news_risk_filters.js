/* 新闻汇总：按风险标签、等级、关键词、来源与发布时间组合筛选。 */
(function () {
  "use strict";
  var STATE_KEY = "riskintel:news-filter-state:v1:" + window.location.pathname;
  var activeRiskTypes = new Set();
  var activeLevels = new Set();
  var searchTerm = "";
  var riskButtons = Array.prototype.slice.call(document.querySelectorAll(".news-risk-filter"));
  var levelButtons = Array.prototype.slice.call(document.querySelectorAll(".news-level-filter"));
  var searchInput = document.getElementById("daily-module-search");
  if (!riskButtons.length && !levelButtons.length && !searchInput) return;

  // Modified by DingJiaye: 2026-08-28 — 日报首屏必须默认展示全部新闻。
  // 旧版会记住上一次的“关注/风险”筛选，造成普通新闻和整个大型企业板块被无提示隐藏。
  // 筛选仅在当前页面会话内生效，刷新后恢复为“全部”。
  try { window.localStorage.removeItem(STATE_KEY); } catch (e) { /* ignore */ }
  if (searchInput) searchInput.value = searchTerm;

  function save() { /* 当前会话筛选不持久化，避免下次打开日报时误隐藏资讯。 */ }

  function reorderGroup(buttons, isActive) {
    if (!buttons.length) return;
    var parent = buttons[0].parentNode;
    var selected = [];
    var rest = [];
    buttons.forEach(function (button) {
      if (isActive(button)) selected.push(button);
      else rest.push(button);
    });
    selected.concat(rest).forEach(function (button) {
      parent.appendChild(button);
    });
  }

  function syncButtons() {
    riskButtons.forEach(function (button) {
      button.classList.toggle("active", activeRiskTypes.has(button.getAttribute("data-news-risk")));
    });
    levelButtons.forEach(function (button) {
      button.classList.toggle("active", activeLevels.has(button.getAttribute("data-news-level")));
    });
    reorderGroup(riskButtons, function (button) {
      return activeRiskTypes.has(button.getAttribute("data-news-risk"));
    });
    reorderGroup(levelButtons, function (button) {
      return activeLevels.has(button.getAttribute("data-news-level"));
    });
  }

  function cardMatchesFilters(card) {
    var tags = String(card.getAttribute("data-risk-tags") || "").split("|").filter(Boolean);
    var riskMatches = !activeRiskTypes.size || tags.some(function (tag) { return activeRiskTypes.has(tag); });
    var levelMatches = !activeLevels.size || activeLevels.has(card.getAttribute("data-risk-level") || "");
    var searchText = String(card.getAttribute("data-news-search") || card.textContent || "").toLowerCase();
    var searchMatches = !searchTerm || searchText.indexOf(searchTerm) !== -1;
    return riskMatches && levelMatches && searchMatches;
  }

  function updateIndexCounts() {
    var counts = {};
    document.querySelectorAll("[data-risk-tags]").forEach(function (card) {
      if (!cardMatchesFilters(card)) return;
      var code = card.getAttribute("data-module");
      if (!code) return;
      counts[code] = (counts[code] || 0) + 1;
    });
    document.querySelectorAll("[data-module-count]").forEach(function (badge) {
      var n = counts[badge.getAttribute("data-module-count")] || 0;
      badge.textContent = String(n);
      badge.setAttribute("aria-label", n + "条新闻");
    });
  }

  function apply() {
    var visible = 0;
    document.querySelectorAll("[data-risk-tags]").forEach(function (card) {
      var moduleMatches = card.getAttribute("data-module-match") !== "false";
      card.hidden = !cardMatchesFilters(card) || !moduleMatches;
      if (!card.hidden) visible += 1;
    });

    var hasFilter = Boolean(searchTerm) || activeRiskTypes.size > 0 || activeLevels.size > 0;
    document.querySelectorAll(".module-panel").forEach(function (panel) {
      var cards = panel.querySelectorAll("[data-risk-tags]");
      if (cards.length) {
        panel.hidden = hasFilter && !Array.prototype.some.call(cards, function (card) { return !card.hidden; });
      }
    });
    var empty = document.getElementById("news-search-empty");
    if (empty) empty.hidden = !hasFilter || visible > 0;
    updateIndexCounts();
  }

  window.applyNewsRiskFilter = apply;
  window.clearNewsRiskFilters = function () {
    activeRiskTypes.clear();
    activeLevels.clear();
    searchTerm = "";
    if (searchInput) searchInput.value = "";
    syncButtons();
    apply();
  };
  // Modified by DingJiaye: 2026-09-07 — 风险总览统计卡与左侧筛选共用同一状态，
  // 点击“需关注 / 风险”只影响当前日报页面已汇总的资讯。
  window.applyDailyRiskLevelFilter = function (scope) {
    var levels = scope === "risk" ? ["高", "极高"] : ["中", "高", "极高"];
    var current = Array.from(activeLevels);
    var same = current.length === levels.length && levels.every(function (level) { return activeLevels.has(level); });
    activeLevels.clear();
    if (!same) levels.forEach(function (level) { activeLevels.add(level); });
    document.querySelectorAll("[data-agent-news-level]").forEach(function (button) {
      var on = !same && button.getAttribute("data-agent-news-level") === scope;
      button.classList.toggle("is-active", on);
      button.setAttribute("aria-pressed", on ? "true" : "false");
    });
    syncButtons();
    apply();
    document.getElementById("global-risk-overview")?.scrollIntoView({ behavior: "smooth", block: "start" });
  };
  document.querySelectorAll("[data-agent-news-level]").forEach(function (button) {
    button.addEventListener("click", function () { window.applyDailyRiskLevelFilter(button.getAttribute("data-agent-news-level")); });
  });
  syncButtons();
  apply();
  riskButtons.forEach(function (button) {
    button.addEventListener("click", function () {
      var value = button.getAttribute("data-news-risk");
      if (activeRiskTypes.has(value)) activeRiskTypes.delete(value);
      else activeRiskTypes.add(value);
      syncButtons();
      save();
      apply();
    });
  });
  levelButtons.forEach(function (button) {
    button.addEventListener("click", function () {
      var value = button.getAttribute("data-news-level");
      if (activeLevels.has(value)) activeLevels.delete(value);
      else activeLevels.add(value);
      syncButtons();
      save();
      apply();
    });
  });
  if (searchInput) {
    searchInput.addEventListener("input", function () {
      searchTerm = searchInput.value.trim().toLowerCase();
      save();
      apply();
    });
  }
})();
