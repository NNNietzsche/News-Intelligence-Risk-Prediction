/*
 * RiskIntel motion controller
 * 负责：卡片进入视区时的柔和呈现、主要控件的微交互反馈。
 * 不依赖第三方库，且尊重 prefers-reduced-motion。
 * Modified by DingJiaye: 2026-08-27 — 使用空闲帧分批挂载动效，避免智能体结果写入时阻塞交互。
 */
(function () {
  "use strict";

  var reduceMotion = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (reduceMotion) return;

  var selectors = [
    ".daily-summary-report",
    ".module-panel",
    ".risk-event-card",
    ".timeline-item",
    ".entity-latest-panel",
    ".entity-risk-card",
    ".entity-financial-card",
    ".industry-document-workspace",
    ".report-preview",
    ".ir-overview",
    ".ir-filter-bar",
    ".ir-table-wrap"
  ];

  function uniqueTargets() {
    var seen = new Set();
    var targets = [];
    document.querySelectorAll(selectors.join(",")).forEach(function (node) {
      if (!seen.has(node) && !node.hidden) {
        seen.add(node);
        targets.push(node);
      }
    });
    return targets;
  }

  function revealContent(root) {
    var targets = uniqueTargets().filter(function (node) {
      return !root || root === document || root.contains(node) || node === root;
    });
    if (!targets.length) return;
    targets.forEach(function (node, index) {
      if (node.classList.contains("is-visible")) return;
      node.classList.add("motion-reveal");
      node.style.setProperty("--motion-delay", Math.min(index, 7) * 38 + "ms");
    });

    if (!("IntersectionObserver" in window)) {
      requestAnimationFrame(function () {
        targets.forEach(function (node) { node.classList.add("is-visible"); });
      });
      return;
    }

    var observer = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (!entry.isIntersecting) return;
        entry.target.classList.add("is-visible");
        observer.unobserve(entry.target);
      });
    }, { threshold: 0.06, rootMargin: "0px 0px -24px 0px" });
    targets.forEach(function (node) { observer.observe(node); });
  }

  function addPressFeedback(root) {
    (root || document).querySelectorAll("button, .btn, .sidebar-action-primary, .sidebar-action-secondary").forEach(function (node) {
      if (node.dataset.motionBound === "1") return;
      node.dataset.motionBound = "1";
      node.classList.add("motion-press");
      node.addEventListener("pointerdown", function () {
        if (!node.disabled) node.classList.add("is-pressed");
      });
      ["pointerup", "pointercancel", "pointerleave"].forEach(function (eventName) {
        node.addEventListener(eventName, function () { node.classList.remove("is-pressed"); });
      });
    });
  }

  function refresh(root) {
    addPressFeedback(root || document);
    revealContent(root || document);
  }

  function initialise() {
    requestAnimationFrame(function () {
      document.documentElement.classList.add("agent-ui-ready");
      refresh(document);
    });

    // 智能体异步返回结果时，仅为新增内容绑定效果；避免整页重绘。
    var queuedRoot = null;
    var refreshTimer = null;
    var observer = new MutationObserver(function (records) {
      records.forEach(function (record) {
        if (record.addedNodes && record.addedNodes.length) queuedRoot = record.target;
      });
      if (!queuedRoot || refreshTimer) return;
      refreshTimer = window.setTimeout(function () {
        refresh(queuedRoot);
        queuedRoot = null;
        refreshTimer = null;
      }, 180);
    });
    observer.observe(document.body, { childList: true, subtree: true });
  }

  window.RiskIntelMotion = { refresh: refresh };

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", initialise, { once: true });
  else initialise();
}());
