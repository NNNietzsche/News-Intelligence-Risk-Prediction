"""双模型采集与分析流水线（分阶段 + staging 替换 + 降级入库）。"""

from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.config import (
    MODULE_CODES,
    NEWS_WINDOW_HOURS_24,
    PAGE_MODULES,
    get_settings,
    module_search_queries,
    news_window_label,
)
from app.database.models import (
    ContentFingerprint,
    DailyRiskEntry,
    EntityRisk,
    NewsArticle,
    PipelineArtifact,
    ReportRun,
    SearchLog,
)
from app.services.chart_generator import extract_and_build_charts
from app.services.content_extractor import enrich_items_with_body
from app.services.dedup import content_fingerprint, dedupe_by_title_similarity, titles_similar
from app.services.deepseek_analyzer import DeepSeekAnalyzer
from app.services.domain_rules import get_active_blacklist, get_active_whitelist
from app.services.entity_credit import refresh_entity_credit, resolve_entity
from app.services.entity_catalog import EntityProfile, configured_entity_catalog
from app.services.entity_relevance import (
    classify_entity_relevance,
    prepare_ai_entity_row,
    prepare_entity_row,
)
from app.services.llm_cache import get_cached_items, material_hash, set_cached_items
from app.services.risk_intelligence_agent import RiskIntelligenceAgent
from app.services.ddg_search import DuckDuckGoNewsClient
from app.services.mita_search import (
    MitaSearchClient,
    MitaSearchResponse,
    apply_local_domain_filter,
)
from app.services.search_fallback import (
    is_empty_search_error,
    rewrite_ddg_news_query,
    should_fallback_to_ddg,
)
from app.services.news_quality import (
    filter_publishable_rows,
    is_reference_only_item,
    is_substantive_news_item,
)
from app.services.news_section_router import (
    filter_candidate_items,
    filter_rows_for_module,
    item_in_module_scope,
    route_news_sections,
)
from app.services.recency import is_within_hours, parse_published_at
from app.services.rss_news import (
    GOOGLE_LOOKBACK_DAYS,
    RssNewsCollector,
    strip_google_day_scope,
    with_google_day_scope,
)
from app.services.scrapers.direct_site_collector import DirectSiteCollector
from app.services.scrapers.godiva_source_collector import GodivaSourceCollector
from app.services.scrapers.entity_source_collector import EntitySourceCollector
from app.services.scrapers.official_portals import OfficialPortalScraper
from app.services.scrapers.sina_724_collector import Sina724Collector
from app.services.scrapers.tdnet_collector import TdnetCollector
from app.timeutil import TOKYO, tokyo_now, tokyo_today

logger = logging.getLogger(__name__)

_HTML_INDUSTRY_HINT = re.compile(
    r"cocobod\.gh|icco\.org|agriculture\.ec\.europa\.eu|depart\.or\.jp",
    re.I,
)

RISK_LEVEL_ORDER = {"极高": 4, "高": 3, "中": 2, "低": 1}
NEWS_MODULES = set(PAGE_MODULES.get("daily_news", ("B", "C", "D"))) | set(
    PAGE_MODULES.get("news_7x24", ())
)
ENTITY_MODULES = set(PAGE_MODULES.get("entity_assessment", ("A",)))


def _profile_needs_html_industry_sources(profile: EntityProfile | None) -> bool:
    """无 RSS 的行业背景页按主体信源配置启用，不按主体名称特判。"""
    if profile is None:
        return False
    blob = " ".join(
        f"{src.url} {src.label}"
        for src in (*profile.sources, *profile.briefing_sources)
    )
    return bool(_HTML_INDUSTRY_HINT.search(blob))


def _llm_failure_reason(exc: BaseException, *, provider: str = "DeepSeek") -> str:
    """将 LLM/网络异常归纳为卡片可展示的短原因。"""
    text = str(exc or "").strip()
    low = text.lower()
    if not text:
        return "模型调用异常"
    if "api key" in low or "鉴权" in text or "401" in text or "403" in text:
        return "API密钥或鉴权失败"
    if "429" in text or "rate" in low or "限流" in text:
        return "接口限流"
    if "timeout" in low or "timed out" in low or "超时" in text:
        return "网络超时"
    if "connect" in low or "dns" in low or "name resolution" in low or "连接" in text:
        return "网络连接失败"
    if "json" in low or "解析" in text or "parse" in low:
        return "模型返回无法解析"
    if "placeholder" in low or "未配置" in text:
        return f"{provider}未配置"
    short = text.replace("\n", " ")
    if len(short) > 48:
        short = short[:48] + "…"
    return short


def _degraded_impact_text(reason: str) -> str:
    r = (reason or "").strip() or "模型分析失败"
    return f"结构化分析暂不可用（原因：{r}），已按原始摘要入库，请人工复核。"


class RiskPipeline:
    def __init__(
        self,
        db: Session,
        mita: Optional[MitaSearchClient] = None,
        deepseek: Optional[DeepSeekAnalyzer] = None,
        rss: Optional[RssNewsCollector] = None,
        job_id: Optional[str] = None,
        authority_text: Optional[str] = None,
        entity_id: Optional[int] = None,
        window_hours: Optional[int] = None,
        direct_sites_config=None,
        domain_whitelist: Optional[list[str]] = None,
        domain_blacklist: Optional[list[str]] = None,
        ddg: Optional[DuckDuckGoNewsClient] = None,
    ) -> None:
        self.db = db
        self.mita = mita or MitaSearchClient()
        self.ddg = ddg
        self.deepseek = deepseek or DeepSeekAnalyzer()
        # Modified by DingJiaye: 2026-08-27 — 所有新闻模块统一经情报智能体规划检索，
        # 再由原有官方/RSS/搜索工具抓取正文与核验，不直接采信模型生成的事实。
        self.intelligence_agent = RiskIntelligenceAgent(self.db, deepseek=self.deepseek)
        self.settings = get_settings()
        default_hours = int(getattr(self.settings, "news_window_hours", NEWS_WINDOW_HOURS_24) or NEWS_WINDOW_HOURS_24)
        self.window_hours = int(window_hours if window_hours is not None else default_hours)
        if self.window_hours < 1:
            self.window_hours = NEWS_WINDOW_HOURS_24
        # 入库时效键；补采历史日时可与采集窗不同（采集窗放大，仍按 24 写入按日快照）
        self._persist_window_hours: Optional[int] = None
        self.calendar_day: Optional[date] = None
        self._backfill_adjusted = False
        self.job_id = job_id
        # None = 实时读取数据源；非 None（含空串）= 任务启动时冻结的快照，运行中变更不影响本次
        self._authority_snapshot = authority_text
        self.entity_id = entity_id
        self._entity_cache = None
        self._entity_profile_cache: EntityProfile | None = None
        self._entity_profile_loaded = False
        self._direct_sites_config = direct_sites_config
        # 非 None = 任务启动时冻结；运行中改白/黑名单不影响本次采集
        self._frozen_whitelist = domain_whitelist
        self._frozen_blacklist = domain_blacklist
        self.llm_top_k = int(getattr(self.settings, "pipeline_llm_top_k", 12) or 12)
        self.cache_hours = int(getattr(self.settings, "pipeline_llm_cache_hours", 168) or 168)
        self.llm_concurrency = max(
            1, int(getattr(self.settings, "pipeline_llm_concurrency", 3) or 1)
        )
        self.mita_pause = float(getattr(self.settings, "pipeline_mita_query_pause_seconds", 0.8) or 0)
        raw_cap = int(getattr(self.settings, "pipeline_collect_max_items", 80) or 80)
        self.collect_max_items = raw_cap if raw_cap > 0 else 80
        # Session / funnel 非线程安全：LLM 并发时串行化缓存读写与计数
        self._llm_db_lock = threading.Lock()
        if rss is not None:
            self.rss = rss
        else:
            self.rss = RssNewsCollector(
                retry_attempts=int(getattr(self.settings, "network_retry_attempts", 3) or 3),
                retry_backoff=float(getattr(self.settings, "network_retry_backoff_seconds", 1.5) or 1.5),
                fetch_workers=int(getattr(self.settings, "pipeline_rss_fetch_workers", 6) or 6),
            )

    @property
    def persist_hours(self) -> int:
        if self._persist_window_hours is not None:
            return int(self._persist_window_hours)
        return int(self.window_hours)

    def _prepare_day_snapshot(self, module_code: str, report_date: date) -> None:
        """历史日缺快照补采：放大采集窗，入库仍写 window_hours=24，并按东京日历日过滤。"""
        if self._backfill_adjusted:
            return
        if module_code not in NEWS_MODULES:
            return
        if int(self.window_hours) != NEWS_WINDOW_HOURS_24:
            return
        if report_date >= tokyo_today():
            return
        start = datetime.combine(report_date, datetime.min.time())
        hours = int((tokyo_now() - start).total_seconds() / 3600) + 1
        hours = max(NEWS_WINDOW_HOURS_24, min(168, hours))
        self.window_hours = hours
        self._persist_window_hours = NEWS_WINDOW_HOURS_24
        self.calendar_day = report_date
        self._backfill_adjusted = True

    def _published_on_calendar_day(self, published_at: Optional[datetime]) -> bool:
        """无 calendar_day 约束时放行；有约束时要求发布时间落在该东京日。"""
        if self.calendar_day is None:
            return True
        if published_at is None:
            return False
        if published_at.tzinfo is not None:
            local = published_at.astimezone(TOKYO).replace(tzinfo=None)
        else:
            local = published_at
        return local.date() == self.calendar_day

    def run_module(self, module_code: str, report_date: date) -> int:
        module_code = module_code.upper()
        if module_code not in MODULE_CODES:
            raise ValueError(f"未知模块: {module_code}")

        self._prepare_day_snapshot(module_code, report_date)
        run = self._ensure_run(report_date, module_code)
        # 先清掉历史误入库的检索入口占位，避免页面继续展示假新闻
        purged_early = self._purge_non_news_placeholders(report_date, module_code)
        purged_scope = self._purge_out_of_scope_entries(report_date, module_code)
        previous_count = self._count_existing(report_date, module_code)
        funnel: dict[str, Any] = {
            "previous_count": previous_count,
            "rss_fetched": 0,
            "rss_feeds_ok": 0,
            "rss_feeds_fail": 0,
            "mita_fetched": 0,
            "mita_skipped": None,
            "mita_forced": False,
            "mita_target": 0,
            "ddg_fallback": 0,
            "ddg_fetched": 0,
            "primary_valid": 0,
            "portal_fetched": 0,
            "authority": 0,
            "after_dedup": 0,
            "sent_llm": 0,
            "llm_cached": 0,
            "structured": 0,
            "degraded": 0,
            "saved": 0,
            "kept_previous": False,
            "source_fail": [],
            "purged_placeholders": purged_early,
            "purged_out_of_scope": purged_scope,
        }

        whitelist = (
            list(self._frozen_whitelist)
            if self._frozen_whitelist is not None
            else get_active_whitelist(self.db, module_code)
        )
        blacklist = (
            list(self._frozen_blacklist)
            if self._frozen_blacklist is not None
            else get_active_blacklist(self.db)
        )
        source_ok = 0
        source_fail = 0
        fail_notes: list[str] = []

        try:
            # ---- phase: collect（不清空旧数据）----
            run.phase = "collect"
            run.status = "running"
            run.notes = "情报智能体正在检索并核验信源…"
            self.db.commit()
            t_collect0 = time.perf_counter()

            batches: list[dict[str, Any]] = []
            if self._authority_snapshot is not None:
                # 空字符串快照表示本任务不使用权威上传材料（风险日报/主体评估）
                authority_text = self._authority_snapshot
            else:
                authority_text = ""

            if authority_text.strip():
                batches.append(
                    {
                        "source": "authority",
                        "items": [],
                        "authority_text": authority_text[:50000],
                        "metadata": {"source": "authority"},
                    }
                )
                funnel["authority"] = 1
                source_ok += 1

            channel_batches, ch_ok, ch_fail, ch_notes, authority_text = (
                self._collect_primary_channels(
                    module_code=module_code,
                    report_date=report_date,
                    funnel=funnel,
                    authority_text=authority_text,
                )
            )
            batches.extend(channel_batches)
            source_ok += ch_ok
            source_fail += ch_fail
            fail_notes.extend(ch_notes)

            mita_ok = 0
            mita_fail = 0
            mita_api_errors = 0
            entity_targets = self._entity_search_targets() if module_code in ENTITY_MODULES else None
            from app.services.api_keys import is_placeholder_key

            primary_valid = self._count_primary_valid(batches, module_code)
            mita_target = self._mita_target(module_code, batches)

            def _fill_google(reason: str, *, allow_llm: bool = False) -> None:
                nonlocal source_ok, source_fail
                collected = self._count_collect_valid(batches, module_code)
                if collected >= mita_target:
                    return
                try:
                    g_batch = self._collect_google_query_supplement(
                        module_code=module_code,
                        report_date=report_date,
                        funnel=funnel,
                        fill_budget=max(1, mita_target - collected),
                        entity_targets=entity_targets,
                        allow_llm=allow_llm,
                    )
                    if g_batch.get("items"):
                        batches.append(g_batch)
                        source_ok += 1
                        funnel["google_supplement"] = int(
                            funnel.get("google_supplement") or 0
                        ) + len(g_batch["items"])
                        fail_notes.append(
                            f"Google 补缺 {len(g_batch['items'])} 条（{reason}）"
                        )
                    elif g_batch.get("metadata", {}).get("error") and allow_llm:
                        funnel["source_fail"].append("google_supplement")
                        source_fail += 1
                        fail_notes.append("Google 补缺失败")
                except Exception as exc:
                    logger.warning("Google 补缺失败: %s", exc)
                    funnel["source_fail"].append("google_supplement")
                    source_fail += 1
                    fail_notes.append("Google 补缺失败")

            if module_code in ENTITY_MODULES:
                _fill_google("主源不足")
                funnel["collected_valid_after_google"] = self._count_collect_valid(
                    batches, module_code
                )

            skip_mita = is_placeholder_key(getattr(self.settings, "mita_api_key", None))
            collected_valid = self._count_collect_valid(batches, module_code)
            force_mita = self._should_force_mita(module_code, funnel, primary_valid=primary_valid)
            funnel["primary_valid"] = primary_valid
            funnel["mita_target"] = mita_target
            funnel["mita_forced"] = force_mita
            if self.calendar_day is not None:
                funnel["calendar_day"] = self.calendar_day.isoformat()

            if skip_mita:
                funnel["source_fail"].append("mita_unconfigured")
                funnel["mita_skipped"] = "unconfigured"
                fail_notes.append("秘塔未配置，已跳过检索")
            elif not force_mita and collected_valid >= mita_target:
                funnel["mita_skipped"] = "enough"
                logger.info(
                    "模块 %s 已有有效候选 %s ≥ 目标 %s，跳过秘塔补缺",
                    module_code,
                    collected_valid,
                    mita_target,
                )
            else:
                if force_mita and primary_valid >= mita_target:
                    funnel["mita_skipped"] = None
                    fail_notes.append("主源硬故障或历史日补采，强制秘塔补缺")
                baseline_queries = module_search_queries(
                    module_code,
                    report_date.isoformat(),
                    entity_targets=entity_targets,
                    window_hours=self.window_hours,
                    calendar_day=self.calendar_day,
                )
                agent_plan = self.intelligence_agent.plan_search(
                    module_code=module_code,
                    report_date=report_date.isoformat(),
                    window_hours=self.window_hours,
                    fallback_queries=baseline_queries,
                )
                queries = agent_plan.queries
                funnel["intelligence_agent"] = agent_plan.audit_payload()
                gap = max(1, mita_target - primary_valid) if not force_mita else max(
                    mita_target, 1
                )
                # 去重余量：约缺口 1.5 倍即停；查询条数按缺口限流
                fill_budget = max(gap, int(math.ceil(gap * 1.5)))
                max_queries = self._mita_query_budget(module_code, len(queries), gap, force_mita)
                # 部分 RSS 失败时略增查询预算，仍受模块 soft_cap 约束
                if int(funnel.get("rss_feeds_fail") or 0) > 0 and not force_mita:
                    max_queries = min(len(queries), max_queries + 1)
                queries = queries[:max_queries]
                funnel["mita_fill_budget"] = fill_budget
                funnel["mita_queries_planned"] = len(queries)
                mita_valid_added = 0
                for idx, qcfg in enumerate(queries):
                    if mita_valid_added >= fill_budget:
                        funnel["mita_early_stop"] = "fill_budget"
                        break
                    try:
                        per_query = min(12, max(3, fill_budget - mita_valid_added + 2))
                        batch = self._collect_mita(
                            module_code=module_code,
                            query=qcfg["query"],
                            metadata=qcfg.get("metadata") or {},
                            whitelist=whitelist,
                            blacklist=blacklist,
                            funnel=funnel,
                            max_results=per_query,
                        )
                        if batch["items"]:
                            batches.append(batch)
                            mita_ok += 1
                            mita_valid_added += self._count_valid_items(
                                batch["items"], module_code
                            )
                        else:
                            mita_fail += 1
                        if self.mita_pause > 0 and idx + 1 < len(queries):
                            time.sleep(self.mita_pause)
                    except Exception as exc:
                        mita_fail += 1
                        mita_api_errors += 1
                        logger.warning("单条检索失败，已跳过: %s | %s", qcfg.get("query", ""), exc)
                    # 连续 API 失败时提前结束，避免 4 次打同一不可用接口。
                    if (
                        mita_ok == 0
                        and mita_api_errors >= 2
                    ) or (
                        module_code in ENTITY_MODULES
                        and self.entity_id
                        and mita_ok == 0
                        and mita_fail >= 2
                    ):
                        fail_notes.append("秘塔连续失败，提前结束检索")
                        break
                if mita_ok:
                    source_ok += 1
                if mita_fail:
                    source_fail += 1
                    if not mita_ok:
                        fail_notes.append(f"秘塔: {mita_fail}/{mita_fail + mita_ok} 条查询失败")
                        funnel["source_fail"].append("mita")
                if mita_api_errors:
                    funnel["mita_api_errors"] = mita_api_errors
                if int(funnel.get("ddg_fetched") or 0) > 0:
                    fail_notes.append(f"DuckDuckGo 补缺 {funnel['ddg_fetched']} 条")

            # 秘塔全空/不足且候选仍不够：Google News RSS 可补充；不再调用
            # Gemini/DeepSeek 联网搜索，保证日报的检索口径固定。
            primary_after = self._count_primary_valid(batches, module_code)
            collected_after = self._count_collect_valid(batches, module_code)
            funnel["primary_valid_after_mita"] = primary_after
            funnel["collected_valid_after_mita"] = collected_after
            if collected_after < mita_target:
                _fill_google("秘塔不足", allow_llm=bool(
                    getattr(self.settings, "search_llm_fallback_enabled", False)
                ))
            self._save_artifact(
                report_date, module_code, "collect", "all", {"batches": batches}, item_count=_batch_item_total(batches)
            )
            funnel["t_collect_ms"] = int((time.perf_counter() - t_collect0) * 1000)

            # 演示回填必须显式开启；正常模式为空时保留真实历史结果。
            from app.services.api_keys import is_placeholder_key as _ph
            # 所有新闻与主体评估的结构化、摘要、风险分析统一由 DeepSeek 执行。
            llm_key = getattr(self.settings, "deepseek_api_key", None)
            external_offline = _ph(getattr(self.settings, "mita_api_key", None)) and _ph(llm_key)
            has_external_items = any(
                (b.get("items") or b.get("source") == "authority") for b in batches
            )
            if (
                module_code in ENTITY_MODULES
                and self.entity_id
                and external_offline
                and not has_external_items
                and bool(getattr(self.settings, "entity_demo_mode", False))
            ):
                from app.services.entity_mock import refresh_entity_demo_for_collect

                saved = refresh_entity_demo_for_collect(
                    self.db, entity_id=self.entity_id, report_date=report_date
                )
                funnel["saved"] = saved
                funnel["demo_fallback"] = True
                run.entry_count = saved
                run.kept_previous = False
                run.status = "completed" if saved > 0 else "empty"
                run.phase = "done"
                run.notes = f"外部检索未配置，显式演示模式已写入近{news_window_label(self.window_hours)}样本"
                run.finished_at = tokyo_now()
                self._set_funnel(run, funnel)
                self.db.commit()
                return saved

            # ---- phase: analyze ----
            run.phase = "analyze"
            run.notes = "情报智能体正在提取事件与风险信号…"
            self._set_funnel(run, funnel)
            self.db.commit()
            t_analyze0 = time.perf_counter()

            pending_rows: list[dict[str, Any]] = []
            merge_llm = bool(getattr(self.settings, "pipeline_merge_llm", True))
            if merge_llm:
                pending_rows = self._analyze_merged(
                    batches,
                    module_code=module_code,
                    report_date=report_date,
                    authority_text=authority_text,
                    funnel=funnel,
                )
            else:
                for batch in batches:
                    rows = self._analyze_batch(
                        batch,
                        module_code=module_code,
                        report_date=report_date,
                        authority_text=authority_text,
                        funnel=funnel,
                    )
                    pending_rows.extend(rows)

            if module_code in ENTITY_MODULES and self.entity_id:
                pending_rows = self._filter_entity_rows(pending_rows, funnel)
            pending_rows = self._dedupe_structured(pending_rows, module_code, funnel)
            pending_rows = filter_publishable_rows(pending_rows)
            funnel["after_quality_filter"] = len(pending_rows)
            if module_code in NEWS_MODULES:
                # Modified by DingJiaye: 2026-08-27 — AI 已根据正文完成主体/板块相关性
                # 审核，不能再由关键词白名单把有效资讯整个板块误删。模型不可用时，仍用
                # 原静态路由做安全兜底，保证页面不会混入明显跨板块内容。
                ai_screening = bool(
                    getattr(self.settings, "intelligence_agent_ai_screening_enabled", True)
                ) and not any(bool(row.get("_degraded")) for row in pending_rows)
                if ai_screening:
                    funnel["after_section_route"] = len(pending_rows)
                    funnel["section_route"] = {
                        "mode": "ai_agent",
                        "routed_in": len(pending_rows),
                        "routed_out": 0,
                        "excluded_topic": 0,
                        "dual": 0,
                    }
                else:
                    pending_rows, route_stats = filter_rows_for_module(
                        pending_rows, module_code, keep_unmatched=False
                    )
                    funnel["after_section_route"] = len(pending_rows)
                    funnel["section_route"] = route_stats
                    if route_stats.get("routed_out") or route_stats.get("excluded_topic"):
                        logger.info(
                            "模块 %s 分类路由兜底剔除 %s 条（主题排除 %s，双投 %s）",
                            module_code,
                            route_stats.get("routed_out"),
                            route_stats.get("excluded_topic"),
                            route_stats.get("dual"),
                        )
            funnel["t_analyze_ms"] = int((time.perf_counter() - t_analyze0) * 1000)
            self._save_artifact(
                report_date,
                module_code,
                "analyze",
                "structured",
                {"rows": pending_rows, "funnel": funnel},
                item_count=len(pending_rows),
            )

            # ---- phase: publish（仅成功产出时替换；否则保留旧数据）----
            run.phase = "publish"
            run.notes = "情报智能体正在生成页面情报…"
            self._set_funnel(run, funnel)
            self.db.commit()

            # 清理历史误入库的「披露检索」占位（再扫一次，防分析阶段写入）
            purged = self._purge_non_news_placeholders(report_date, module_code)
            if purged:
                funnel["purged_placeholders"] = int(funnel.get("purged_placeholders") or 0) + purged
                previous_count = self._count_existing(report_date, module_code)

            if pending_rows:
                self._replace_module_entries(report_date, module_code)
                saved = self._save_structured_entries(
                    pending_rows,
                    module_code=module_code,
                    report_date=report_date,
                    metadata={},
                    search_log_id=None,
                    raw_context=json.dumps(
                        {"funnel": funnel, "count": len(pending_rows)}, ensure_ascii=False
                    ),
                    source_items=None,
                    register_fingerprints=True,
                )
                funnel["saved"] = saved
                run.entry_count = saved
                run.kept_previous = False
                run.status = "completed" if saved > 0 else "empty"
                note = f"近{news_window_label(self.window_hours)}重要资讯采集"
                if source_fail:
                    note += f"（部分源失败 {source_fail}）"
                if funnel.get("degraded"):
                    note += f"（降级入库 {funnel['degraded']}）"
                if funnel.get("llm_cached"):
                    note += f"（缓存命中 {funnel['llm_cached']}）"
                if funnel.get("section_route", {}).get("routed_out"):
                    note += f"（路由剔除 {funnel['section_route']['routed_out']}）"
                t_c = funnel.get("t_collect_ms")
                t_a = funnel.get("t_analyze_ms")
                if t_c is not None or t_a is not None:
                    note += f"（耗时 采集{int(t_c or 0)}ms/分析{int(t_a or 0)}ms）"
                run.notes = note
            else:
                if (
                    module_code in ENTITY_MODULES
                    and self.entity_id
                    and bool(getattr(self.settings, "entity_demo_mode", False))
                ):
                    from app.services.entity_mock import refresh_entity_demo_for_collect

                    saved = refresh_entity_demo_for_collect(
                        self.db, entity_id=self.entity_id, report_date=report_date
                    )
                    funnel["saved"] = saved
                    funnel["demo_fallback"] = True
                    run.entry_count = saved
                    run.kept_previous = False
                    run.status = "completed" if saved > 0 else "empty"
                    run.notes = (
                        f"近{news_window_label(self.window_hours)}采集无新增源数据，显式演示模式已写入样本"
                        if saved
                        else "今日无动态"
                    )
                else:
                    # 无新产出：保留旧数据，但先清掉越界旧条目（娱乐/非本板块等）
                    purged_scope = self._purge_out_of_scope_entries(report_date, module_code)
                    if purged_scope:
                        funnel["purged_out_of_scope"] = purged_scope
                        previous_count = self._count_existing(report_date, module_code)
                    funnel["kept_previous"] = True
                    run.kept_previous = True
                    run.entry_count = previous_count
                    if source_fail and not source_ok:
                        run.status = "failed"
                        run.notes = (
                            "请求失败，已保留上次结果｜"
                            + "；".join(fail_notes[:3])
                        )
                    elif previous_count > 0:
                        run.status = "completed"
                        run.notes = "今日无新增动态，已保留上次结果"
                    else:
                        run.status = "empty"
                        run.notes = "今日无动态"

            run.phase = "done"
            run.finished_at = tokyo_now()
            self._set_funnel(run, funnel)
            self.db.commit()
            return int(run.entry_count or 0)

        except Exception as exc:
            logger.exception("模块 %s 流水线失败", module_code)
            funnel["kept_previous"] = True
            run.kept_previous = True
            run.entry_count = previous_count
            run.status = "failed"
            run.phase = "done"
            run.notes = f"请求失败，已保留上次结果｜{exc}"
            run.finished_at = tokyo_now()
            self._set_funnel(run, funnel)
            self.db.commit()
            raise

    def run_all_modules(self, report_date: date) -> dict[str, int]:
        results: dict[str, int] = {}
        for code in MODULE_CODES:
            try:
                results[code] = self.run_module(code, report_date)
            except Exception as exc:
                logger.error("模块 %s 运行失败: %s", code, exc)
                results[code] = -1
        return results

    # ------------------------------------------------------------------
    # collect helpers
    # ------------------------------------------------------------------

    def _collect_primary_channels(
        self,
        *,
        module_code: str,
        report_date: date,
        funnel: dict[str, Any],
        authority_text: str,
    ) -> tuple[list[dict[str, Any]], int, int, list[str], str]:
        """并行拉取同模块主源通道；各通道独立 Session 写 SearchLog。"""
        # 主线程预热主体配置，避免工作线程争用 self.db
        if module_code in ENTITY_MODULES:
            self._entity_profile()
        profile = self._entity_profile() if module_code in ENTITY_MODULES else None
        jobs: list[tuple[str, Any]] = [
            ("rss", lambda db, f: self._collect_rss(module_code, f, db=db)),
            ("direct_site", lambda db, f: self._collect_direct_sites(module_code, f, db=db)),
        ]
        if module_code == "A" and profile is not None:
            jobs.append(("entity_site", lambda db, f: self._collect_entity_sites(module_code, f, db=db)))
        if module_code == "A" and _profile_needs_html_industry_sources(profile):
            jobs.append(
                (
                    "godiva_source",
                    lambda db, f: self._collect_godiva_sources(module_code, f, db=db),
                )
            )
        if module_code in ("B", "D"):
            jobs.append(
                ("sina_724", lambda db, f: self._collect_sina_724(module_code, f, db=db))
            )
        if module_code == "C" or (
            module_code == "A" and profile is not None and profile.stock_code
        ):
            jobs.append(("tdnet", lambda db, f: self._collect_tdnet(module_code, f, db=db)))
        if module_code == "C":
            jobs.append(
                (
                    "portal_notes",
                    lambda db, f: {
                        "source": "portal_notes",
                        "items": [],
                        "portal_note": self._collect_portals_as_notes(
                            module_code, report_date, f, db=db
                        ),
                        "error": None,
                    },
                )
            )

        parallel = bool(getattr(self.settings, "pipeline_channel_parallel", True))
        workers = min(len(jobs), 4) if parallel and len(jobs) > 1 else 1

        def _run_one(name: str, fn) -> tuple[str, Optional[dict], dict, Optional[str]]:
            from app.database.session import SessionLocal

            local_funnel: dict[str, Any] = {}
            db = SessionLocal()
            try:
                batch = fn(db, local_funnel)
                db.commit()
                return name, batch, local_funnel, None
            except Exception as exc:
                db.rollback()
                logger.warning("模块 %s 通道 %s 采集失败（已跳过）: %s", module_code, name, exc)
                return name, None, local_funnel, str(exc)
            finally:
                db.close()

        results: dict[str, tuple[Optional[dict], dict, Optional[str]]] = {}
        if workers <= 1:
            for name, fn in jobs:
                n, batch, lf, err = _run_one(name, fn)
                results[n] = (batch, lf, err)
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(_run_one, name, fn): name for name, fn in jobs}
                for fut in as_completed(futures):
                    name, batch, lf, err = fut.result()
                    results[name] = (batch, lf, err)

        # 合并 funnel 计数（主线程）
        for _name, (_batch, lf, _err) in results.items():
            for k, v in lf.items():
                if isinstance(v, (int, float)) and isinstance(funnel.get(k), (int, float)):
                    funnel[k] = int(funnel.get(k) or 0) + int(v)
                elif k not in funnel or funnel.get(k) in (None, 0, False, [], {}):
                    funnel[k] = v

        out_batches: list[dict[str, Any]] = []
        source_ok = 0
        source_fail = 0
        fail_notes: list[str] = []
        auth = authority_text

        def _absorb(
            name: str,
            *,
            label: str,
            soft_errors: tuple[str, ...] = (),
            fail_key: Optional[str] = None,
        ) -> None:
            nonlocal source_ok, source_fail
            if name not in results:
                return
            batch, _lf, err = results[name]
            if err:
                source_fail += 1
                fail_notes.append(f"{label}: {err}")
                funnel["source_fail"].append(fail_key or name)
                return
            if not batch:
                return
            if name == "portal_notes":
                note = (batch.get("portal_note") or "").strip()
                if note:
                    auth_extra = (
                        f"{auth}\n\n=== EDINET 核对入口（非新闻正文）===\n{note}"
                    ).strip()
                    nonlocal_auth[0] = auth_extra
                return
            if batch.get("items"):
                out_batches.append(batch)
                source_ok += 1
                return
            err_msg = str(batch.get("error") or "")
            if err_msg and not any(s in err_msg for s in soft_errors):
                source_fail += 1
                fail_notes.append(f"{label}: {err_msg}")
                funnel["source_fail"].append(fail_key or name)

        # portal 用可变盒更新 auth（避免嵌套 nonlocal 困扰）
        nonlocal_auth = [auth]
        _absorb("rss", label="RSS", soft_errors=(), fail_key="rss")
        _absorb(
            "direct_site",
            label="直连站点",
            soft_errors=("未配置", "无条目"),
            fail_key="direct_site",
        )
        _absorb("entity_site", label="主体官网/监管/交易所", soft_errors=("无条目",), fail_key="entity_site")
        _absorb(
            "godiva_source",
            label="无RSS信源",
            soft_errors=("无条目",),
            fail_key="godiva_source",
        )
        _absorb(
            "sina_724",
            label="新浪7x24",
            soft_errors=("无条目",),
            fail_key="sina_724",
        )
        _absorb(
            "tdnet",
            label="TDnet",
            soft_errors=("无监控企业披露",),
            fail_key="tdnet",
        )
        _absorb("portal_notes", label="EDINET")
        return out_batches, source_ok, source_fail, fail_notes, nonlocal_auth[0]

    def _collect_rss(
        self,
        module_code: str,
        funnel: dict[str, Any],
        *,
        db: Optional[Session] = None,
    ) -> dict[str, Any]:
        session = db or self.db
        profile = self._entity_profile() if module_code in ENTITY_MODULES else None
        detailed = self.rss.collect_detailed(
            module_code,
            hours=self.window_hours,
            max_items=self.collect_max_items,
            entity_key=profile.key if profile else None,
            extra_queries=profile.rss_queries() if profile else None,
            calendar_day=self.calendar_day,
        )
        funnel["rss_fetched"] = len(detailed.items)
        funnel["rss_feeds_ok"] = detailed.fetch_ok
        funnel["rss_feeds_fail"] = detailed.fetch_errors
        items = [
            {
                "title": h.title,
                "url": h.url,
                "snippet": h.snippet,
                "published_at": h.published_at,
                "source_domain": h.source_domain,
                "feed": h.feed_label,
                "fingerprint": h.fingerprint,
                "publisher": h.publisher,
                "entity_key": h.entity_key,
                "relation": h.relation,
                "source_type": h.source_type,
                "configured_source_url": h.configured_source_url,
            }
            for h in detailed.items
        ]
        if items and self.settings.news_fetch_body:
            items = enrich_items_with_body(
                items,
                max_items=int(self.settings.news_max_body_items or 8),
            )
        log = SearchLog(
            module_code=module_code,
            query_text=f"[RSS近{self.window_hours}小时]",
            status="completed" if items else "empty",
            result_count=len(items),
            raw_response=json.dumps(
                {"items": items, "feed_health": [h.__dict__ for h in detailed.feed_health]},
                ensure_ascii=False,
            )[:50000],
        )
        session.add(log)
        session.flush()
        return {
            "source": "rss",
            "items": items,
            "search_log_id": log.id,
            "metadata": {"source": "rss"},
            "error": None if detailed.fetch_ok else "全部 feed 失败",
        }

    def _collect_direct_sites(
        self,
        module_code: str,
        funnel: dict[str, Any],
        *,
        db: Optional[Session] = None,
    ) -> dict[str, Any]:
        """无 RSS 直连网站 HTML 列表采集（config/direct_sites.yaml）。"""
        session = db or self.db
        if self._direct_sites_config is not None:
            collector = DirectSiteCollector(config=self._direct_sites_config)
        else:
            path = getattr(self.settings, "direct_sites_config_path", None) or None
            collector = DirectSiteCollector(config_path=path)
        configured = collector.config.sites_for_module(module_code)
        if not configured:
            funnel["direct_site_fetched"] = 0
            return {
                "source": "direct_site",
                "items": [],
                "search_log_id": None,
                "metadata": {"source": "direct_site"},
                "error": "未配置直连站点",
            }

        hits = collector.collect_for_module(
            module_code, hours=self.window_hours, max_items=self.collect_max_items
        )
        funnel["direct_site_fetched"] = len(hits)
        items = [
            {
                "title": h.title,
                "url": h.url,
                "snippet": h.snippet,
                "published_at": h.published_at,
                "source_domain": h.source_domain,
                "feed": h.feed_label,
                "fingerprint": content_fingerprint(
                    module_code=module_code,
                    title=h.title,
                    url=h.url,
                    published_at=h.published_at,
                ),
            }
            for h in hits
        ]
        if items and self.settings.news_fetch_body:
            items = enrich_items_with_body(
                items,
                max_items=min(8, int(self.settings.news_max_body_items or 8)),
            )
        labels = ", ".join(s.label for s in configured[:5])
        log = SearchLog(
            module_code=module_code,
            query_text=f"[直连站点 {labels} 近{self.window_hours}小时]",
            status="completed" if items else "empty",
            result_count=len(items),
            raw_response=json.dumps({"items": items}, ensure_ascii=False)[:50000],
        )
        session.add(log)
        session.flush()
        return {
            "source": "direct_site",
            "items": items,
            "search_log_id": log.id,
            "metadata": {
                "source": "direct_site",
                "sites": [s.label for s in configured],
            },
            "error": None if items else "近窗内无条目",
        }

    def _collect_godiva_sources(
        self,
        module_code: str,
        funnel: dict[str, Any],
        *,
        db: Optional[Session] = None,
    ) -> dict[str, Any]:
        """无 RSS 的行业背景页：仅当当前主体信源配置了对应站点时启用。"""
        session = db or self.db
        profile = self._entity_profile() if module_code in ENTITY_MODULES else None
        collector = GodivaSourceCollector(
            retry_attempts=int(getattr(self.settings, "network_retry_attempts", 3) or 3),
            retry_backoff=float(
                getattr(self.settings, "network_retry_backoff_seconds", 1.5) or 1.5
            ),
        )
        hits = collector.collect_for_module(
            module_code, hours=self.window_hours, max_items=self.collect_max_items
        )
        funnel["godiva_source_fetched"] = len(hits)
        entity_key = profile.key if profile else None
        items = [
            {
                "title": h.title,
                "url": h.url,
                "snippet": h.snippet,
                "published_at": h.published_at,
                "source_domain": h.source_domain,
                "feed": h.feed_label,
                "publisher": h.feed_label,
                "entity_key": entity_key,
                "relation": "contextual",
                "source_type": "industry",
                "fingerprint": content_fingerprint(
                    module_code=module_code,
                    title=h.title,
                    url=h.url,
                    published_at=h.published_at,
                ),
            }
            for h in hits
        ]
        if items and self.settings.news_fetch_body:
            items = enrich_items_with_body(
                items,
                max_items=min(8, int(self.settings.news_max_body_items or 8)),
            )
        log = SearchLog(
            module_code=module_code,
            query_text=f"[无RSS信源 近{self.window_hours}小时]",
            status="completed" if items else "empty",
            result_count=len(items),
            raw_response=json.dumps({"items": items}, ensure_ascii=False)[:50000],
        )
        session.add(log)
        session.flush()
        return {
            "source": "godiva_source",
            "items": items,
            "search_log_id": log.id,
            "metadata": {"source": "godiva_source"},
            "error": None if items else "近窗内无条目",
        }

    def _collect_entity_sites(self, module_code: str, funnel: dict[str, Any], *, db: Optional[Session] = None) -> dict[str, Any]:
        """从当前主体 YAML 中的官网、监管与交易所列表页提取可追溯条目。"""
        session = db or self.db
        profile = self._entity_profile()
        hits = EntitySourceCollector().collect(profile, hours=self.window_hours, max_items=self.collect_max_items)
        funnel["entity_site_fetched"] = len(hits)
        items = [{"title": h.title, "url": h.url, "snippet": h.snippet, "published_at": h.published_at,
                  "source_domain": h.source_domain, "feed": h.feed_label, "publisher": h.feed_label,
                  "entity_key": profile.key if profile else None, "relation": "direct", "source_type": h.source_type,
                  "fingerprint": content_fingerprint(module_code=module_code, title=h.title, url=h.url, published_at=h.published_at)} for h in hits]
        if items and self.settings.news_fetch_body:
            items = enrich_items_with_body(
                items,
                max_items=min(12, int(self.settings.news_max_body_items or 12)),
            )
        log = SearchLog(module_code=module_code, query_text=f"[主体直连信源 近{self.window_hours}小时]", status="completed" if items else "empty", result_count=len(items), raw_response=json.dumps({"items": items}, ensure_ascii=False)[:50000])
        session.add(log)
        session.flush()
        return {"source": "entity_site", "items": items, "search_log_id": log.id, "metadata": {"source": "entity_site"}, "error": None if items else "近窗内无条目"}

    def _collect_sina_724(
        self,
        module_code: str,
        funnel: dict[str, Any],
        *,
        db: Optional[Session] = None,
    ) -> dict[str, Any]:
        """新浪财经 7×24 快讯（zhibo feed API）。"""
        session = db or self.db
        collector = Sina724Collector()
        hits = collector.collect_for_module(
            module_code, hours=self.window_hours, max_items=self.collect_max_items
        )
        funnel["sina_724_fetched"] = len(hits)
        items = [
            {
                "title": h.title,
                "url": h.url,
                "snippet": h.snippet,
                "published_at": h.published_at,
                "source_domain": h.source_domain,
                "feed": h.feed_label,
                "fingerprint": content_fingerprint(
                    module_code=module_code,
                    title=h.title,
                    url=h.url,
                    published_at=h.published_at,
                ),
            }
            for h in hits
        ]
        if items and self.settings.news_fetch_body:
            items = enrich_items_with_body(
                items,
                max_items=min(8, int(self.settings.news_max_body_items or 8)),
            )
        log = SearchLog(
            module_code=module_code,
            query_text=f"[新浪财经7x24 近{self.window_hours}小时]",
            status="completed" if items else "empty",
            result_count=len(items),
            raw_response=json.dumps({"items": items}, ensure_ascii=False)[:50000],
        )
        session.add(log)
        session.flush()
        return {
            "source": "sina_724",
            "items": items,
            "search_log_id": log.id,
            "metadata": {"source": "sina_724", "portal": "finance.sina.com.cn/7x24"},
            "error": None if items else "近窗内无条目",
        }

    def _collect_tdnet(
        self,
        module_code: str,
        funnel: dict[str, Any],
        *,
        db: Optional[Session] = None,
    ) -> dict[str, Any]:
        """采集监控企业 TDnet 适时应披露，作为可分析/可入库资讯源。"""
        session = db or self.db
        profile = self._entity_profile() if module_code in ENTITY_MODULES else None
        company_codes = None
        if profile and profile.stock_code:
            company_codes = {profile.key: profile.stock_code}
        collector = TdnetCollector(company_codes=company_codes)
        hits = collector.collect(hours=self.window_hours, max_items=self.collect_max_items)
        funnel["tdnet_fetched"] = len(hits)
        items = [
            {
                "title": h.title,
                "url": h.url,
                "snippet": h.snippet,
                "published_at": h.published_at,
                "source_domain": "release.tdnet.info",
                "company": h.company_name,
                "company_code": h.company_code,
                "publisher": "TDnet",
                "feed": "TDnet 适时披露",
                "entity_key": profile.key if profile else None,
                "relation": "direct",
                "source_type": "exchange",
                "fingerprint": content_fingerprint(
                    module_code=module_code,
                    title=h.title,
                    url=h.url,
                    published_at=h.published_at,
                ),
            }
            for h in hits
        ]
        if items and self.settings.news_fetch_body:
            items = enrich_items_with_body(
                items,
                max_items=min(8, int(self.settings.news_max_body_items or 8)),
            )
        log = SearchLog(
            module_code=module_code,
            query_text=f"[TDnet适时应披露近{self.window_hours}小时]",
            status="completed" if items else "empty",
            result_count=len(items),
            raw_response=json.dumps({"items": items}, ensure_ascii=False)[:50000],
        )
        session.add(log)
        session.flush()
        return {
            "source": "tdnet",
            "items": items,
            "search_log_id": log.id,
            "metadata": {"source": "tdnet", "category": "適時開示"},
            "error": None if items else "近窗内无监控企业披露",
        }

    def _collect_portals_as_notes(
        self,
        module_code: str,
        report_date: date,
        funnel: dict[str, Any],
        *,
        db: Optional[Session] = None,
    ) -> str:
        """生成 EDINET 核对备注；TDnet 正文已走独立采集通道。"""
        scraper = OfficialPortalScraper()
        from app.config import MODULE_C_TARGETS

        seen: set[str] = set()
        companies: list[str] = []
        for name in MODULE_C_TARGETS:
            if not any("\u4e00" <= ch <= "\u9fff" for ch in name):
                continue
            if name in seen:
                continue
            seen.add(name)
            companies.append(name)
            if len(companies) >= 5:
                break

        hits = []
        for company in companies:
            # 仅保留 EDINET 入口；TDnet 已正文采集
            for h in scraper.collect_reference_hits(company, report_date):
                if h.source == "EDINET":
                    hits.append(h)
        funnel["portal_fetched"] = len(hits)
        funnel["portal_reference_only"] = True

        lines = [
            f"- [{h.source}] {h.title}: {h.url} （{h.snippet}）"
            for h in hits
        ]
        note = "\n".join(lines)
        session = db or self.db
        log = SearchLog(
            module_code=module_code,
            query_text="[EDINET核对入口-不入库]",
            status="completed",
            result_count=0,
            raw_response=note[:20000],
        )
        session.add(log)
        session.flush()
        self._save_artifact(
            report_date,
            module_code,
            "collect",
            "official_portal_notes",
            {"notes": note, "hit_count": len(hits)},
            item_count=0,
            db=session,
        )
        return note

    def _collect_mita(
        self,
        *,
        module_code: str,
        query: str,
        metadata: dict[str, Any],
        whitelist: list[str],
        blacklist: list[str],
        funnel: dict[str, Any],
        max_results: int = 12,
    ) -> dict[str, Any]:
        # 先完成外网请求，再短事务写日志，避免采集期间占着写锁导致页面无法跳转
        response = self._search_mita_then_ddg(
            module_code=module_code,
            query=query,
            metadata=metadata,
            whitelist=whitelist,
            blacklist=blacklist,
            funnel=funnel,
            max_results=max(1, min(12, int(max_results or 12))),
        )
        provider = getattr(response, "provider", None) or "mita"
        # 重点日本企业只采纳一手网页。秘塔客户端在通用场景可放宽
        # 白名单以提高召回；C 板块不得放宽，否则会混入聚合页和转载。
        if module_code == "C":
            response.items = apply_local_domain_filter(
                list(response.items or []), whitelist, blacklist
            )
        if provider != "mita":
            funnel["mita_fallback_provider"] = provider
        recent_items = []
        for i in response.items:
            if not is_within_hours(i.published_at, self.window_hours, allow_unknown=True):
                continue
            # 历史日：无发布时间则丢掉，避免把近日结果误入库
            if not self._item_matches_calendar_day(i.published_at, allow_unknown=False):
                continue
            recent_items.append(i)
        profile = self._entity_profile() if module_code in ENTITY_MODULES else None
        payload = [
            {
                "title": i.title,
                "url": i.url,
                "snippet": i.snippet,
                "published_at": i.published_at,
                "source_domain": i.source_domain,
                "publisher": i.source_domain,
                "entity_key": profile.key if profile else None,
                "relation": "direct" if profile else "unscoped",
                "source_type": "media",
                "fingerprint": content_fingerprint(
                    module_code=module_code,
                    title=i.title,
                    url=i.url,
                    published_at=i.published_at,
                ),
            }
            for i in recent_items
        ]
        funnel["mita_fetched"] = int(funnel.get("mita_fetched") or 0) + len(payload)
        if payload and self.settings.news_fetch_body:
            payload = enrich_items_with_body(
                payload,
                max_items=min(6, int(self.settings.news_max_body_items or 8)),
            )
        log = SearchLog(
            module_code=module_code,
            query_text=query,
            domains_whitelist=json.dumps(whitelist, ensure_ascii=False),
            domains_blacklist=json.dumps(blacklist, ensure_ascii=False),
            status="completed" if payload else "empty",
            result_count=len(payload),
            raw_response=json.dumps(payload, ensure_ascii=False)[:50000],
        )
        self.db.add(log)
        self.db.commit()
        return {
            "source": "mita",
            "items": payload,
            "search_log_id": log.id,
            "metadata": {
                "source": "mita",
                "search_provider": provider,
                "search_query": getattr(response, "query", query),
                **metadata,
            },
            "query": query,
        }

    def _search_mita_then_ddg(
        self,
        *,
        module_code: str,
        query: str,
        metadata: dict[str, Any],
        whitelist: list[str],
        blacklist: list[str],
        funnel: dict[str, Any],
        max_results: int,
    ) -> MitaSearchResponse:
        response: MitaSearchResponse | None = None
        mita_error: Exception | None = None
        try:
            response = self.mita.search(
                query=query,
                whitelist_domains=whitelist or None,
                blacklist_domains=blacklist or None,
                max_results=max_results,
            )
        except Exception as exc:
            mita_error = exc
            if not is_empty_search_error(exc):
                raise

        items = list(getattr(response, "items", None) or [])
        if items:
            return response or MitaSearchResponse(query=query, items=items, provider="mita")

        profile = self._entity_profile() if module_code in ENTITY_MODULES else None
        if not should_fallback_to_ddg(
            module_code,
            enabled=bool(getattr(self.settings, "search_ddg_fallback_enabled", True)),
            calendar_day=self.calendar_day,
            metadata=metadata,
            profile=profile,
        ):
            if mita_error:
                raise mita_error
            return response or MitaSearchResponse(query=query, items=[], provider="mita")

        ddg = self._get_ddg()
        if ddg is None:
            if mita_error:
                raise mita_error
            return response or MitaSearchResponse(query=query, items=[], provider="mita")

        ddg_query = rewrite_ddg_news_query(
            query, module_code, metadata=metadata, profile=profile
        )
        funnel["ddg_fallback"] = int(funnel.get("ddg_fallback") or 0) + 1
        funnel["ddg_query"] = ddg_query
        try:
            if self.mita_pause > 0:
                time.sleep(self.mita_pause)
            ddg_response = ddg.search(
                query=ddg_query,
                whitelist_domains=whitelist or None,
                blacklist_domains=blacklist or None,
                max_results=max_results,
                window_hours=self.window_hours,
            )
        except Exception as exc:
            logger.warning("DuckDuckGo 补缺失败: %s | %s", ddg_query, exc)
            funnel["ddg_error"] = str(exc)[:200]
            if mita_error:
                raise mita_error
            return response or MitaSearchResponse(query=query, items=[], provider="mita")

        funnel["ddg_fetched"] = int(funnel.get("ddg_fetched") or 0) + len(ddg_response.items)
        if ddg_response.items:
            logger.info(
                "DuckDuckGo 补缺成功: module=%s items=%s q=%s",
                module_code,
                len(ddg_response.items),
                ddg_query[:160],
            )
            return ddg_response
        if mita_error:
            raise mita_error
        return ddg_response

    def _get_ddg(self) -> Optional[DuckDuckGoNewsClient]:
        if not bool(getattr(self.settings, "search_ddg_fallback_enabled", True)):
            return None
        if self.ddg is not None:
            return self.ddg
        if not hasattr(self, "_ddg_lazy"):
            self._ddg_lazy = DuckDuckGoNewsClient()
        return self._ddg_lazy

    def _count_valid_items(self, items: list[dict[str, Any]], module_code: str) -> int:
        """近窗内、有实质内容、符合板块范围、按指纹去重后的条数。"""
        seen: set[str] = set()
        n = 0
        for it in items or []:
            if not is_substantive_news_item(it):
                continue
            if not is_within_hours(
                it.get("published_at"), self.window_hours, allow_unknown=True
            ):
                continue
            if not self._item_matches_calendar_day(
                it.get("published_at"), allow_unknown=False
            ):
                continue
            if (
                module_code in ENTITY_MODULES
                and self.entity_id
                and not bool(
                    getattr(self.settings, "intelligence_agent_entity_screening_enabled", True)
                )
            ):
                # 模型不可用的兼容模式下，才以静态直接关联度决定补缺数量。
                entity = self._selected_entity()
                if entity:
                    relevance = classify_entity_relevance(
                        entity,
                        {
                            "标题": it.get("title"),
                            "关联企业": it.get("company"),
                            "核心摘要": it.get("snippet") or it.get("body"),
                            "_source_item": it,
                        },
                        self._entity_profile(),
                    )
                    # 背景材料可以展示，但不能占满“主体直接资讯”的补缺配额。
                    if relevance != "direct":
                        continue
            if module_code in NEWS_MODULES:
                ok, _ = item_in_module_scope(
                    module_code,
                    title=str(it.get("title") or ""),
                    content=str(it.get("snippet") or it.get("body") or ""),
                    source=str(it.get("feed") or it.get("source_domain") or ""),
                    related_company=str(it.get("company") or ""),
                )
                if not ok:
                    continue
            fp = it.get("fingerprint") or content_fingerprint(
                module_code=module_code,
                title=it.get("title"),
                url=it.get("url"),
                published_at=it.get("published_at"),
            )
            if fp in seen:
                continue
            seen.add(fp)
            n += 1
        return n

    def _count_primary_valid(
        self, batches: list[dict[str, Any]], module_code: str
    ) -> int:
        """主源（RSS / 直连站点 / 新浪7x24 / TDnet）有效候选数；不含秘塔与权威上传正文。"""
        primary_items: list[dict[str, Any]] = []
        for batch in batches:
            if batch.get("source") not in (
                "rss",
                "tdnet",
                "direct_site",
                "sina_724",
                "godiva_source",
            ):
                continue
            primary_items.extend(batch.get("items") or [])
        return self._count_valid_items(primary_items, module_code)

    def _count_collect_valid(
        self, batches: list[dict[str, Any]], module_code: str
    ) -> int:
        """已采集候选数：主源 + Google 补缺 + 秘塔，不含权威上传正文。"""
        items: list[dict[str, Any]] = []
        for batch in batches:
            if batch.get("source") == "authority":
                continue
            items.extend(batch.get("items") or [])
        return self._count_valid_items(items, module_code)

    def _mita_target(self, module_code: str, batches: list[dict[str, Any]]) -> int:
        """秘塔补齐目标条数（与展示全量对齐，默认 24，不再跟 LLM 批大小挂钩）。"""
        configured = int(getattr(self.settings, "pipeline_mita_min_items", 0) or 0)
        base = configured if configured > 0 else 24
        if module_code in ENTITY_MODULES:
            # 主体评估：更严，有几条相关资讯即可，避免空窗也硬扛全量 query
            return min(base, 6)
        if module_code == "C":
            tdnet_n = 0
            for batch in batches:
                if batch.get("source") == "tdnet":
                    tdnet_n += self._count_valid_items(batch.get("items") or [], module_code)
            # 已有正式披露时，搜索补缺门槛略降
            if tdnet_n >= 1:
                return min(base, 16)
        return max(1, base)

    def _should_force_mita(
        self,
        module_code: str,
        funnel: dict[str, Any],
        *,
        primary_valid: int = 0,
    ) -> bool:
        # 历史日补采：live RSS 不可靠，强制走秘塔/Google 按日检索
        if self.calendar_day is not None and module_code in NEWS_MODULES:
            return True
        if not bool(getattr(self.settings, "pipeline_mita_force_on_primary_fail", True)):
            return False
        fails = set(funnel.get("source_fail") or [])
        rss_hard = "rss" in fails
        rss_fail_n = int(funnel.get("rss_feeds_fail") or 0)
        rss_ok_n = int(funnel.get("rss_feeds_ok") or 0)
        # 部分源 DNS/超时：失败数不少于成功数，且有效候选明显不足时强制补
        partial_rss_weak = (
            rss_fail_n > 0
            and rss_fail_n >= max(1, rss_ok_n)
            and primary_valid < self._mita_target(module_code, [])
        )
        if module_code == "C":
            return (rss_hard and "tdnet" in fails) or partial_rss_weak
        # B/D/A：RSS 全挂或半残且候选不足
        return rss_hard or partial_rss_weak

    def _item_matches_calendar_day(
        self, published_at: Any, *, allow_unknown: bool = False
    ) -> bool:
        """无 calendar_day 时放行；有约束时要求发布时间落在该东京日。"""
        if self.calendar_day is None:
            return True
        dt = published_at if isinstance(published_at, datetime) else parse_published_at(
            published_at
        )
        if dt is None:
            return bool(allow_unknown)
        return self._published_on_calendar_day(dt)

    def _collect_google_query_supplement(
        self,
        *,
        module_code: str,
        report_date: date,
        funnel: dict[str, Any],
        fill_budget: int,
        entity_targets: list[str] | None = None,
        allow_llm: bool = False,
    ) -> dict[str, Any]:
        """秘塔不足时用 Google News RSS 补缺；按日无结果则放宽日界，必要时检索替代。"""
        from app.services.rss_config import RssQuerySpec

        baseline_queries = module_search_queries(
            module_code,
            report_date.isoformat(),
            entity_targets=entity_targets,
            window_hours=self.window_hours,
            calendar_day=self.calendar_day,
        )
        # Google News 补缺与主搜索使用同一 Agent 计划，避免两套关键词口径漂移。
        queries = self.intelligence_agent.plan_search(
            module_code=module_code,
            report_date=report_date.isoformat(),
            window_hours=self.window_hours,
            fallback_queries=baseline_queries,
        ).queries
        profile = self._entity_profile() if module_code in ENTITY_MODULES else None

        def _build_specs(*, day_scope: bool) -> list[RssQuerySpec]:
            specs: list[RssQuerySpec] = []
            if profile:
                for idx, query in enumerate(profile.rss_queries()[:4]):
                    raw = query.query or ""
                    scoped = (
                        with_google_day_scope(raw, self.calendar_day)
                        if day_scope
                        else strip_google_day_scope(raw)
                    )
                    specs.append(
                        RssQuerySpec(
                            label=query.label or f"entity_google_{idx+1}",
                            query=scoped,
                            enabled=True,
                            priority=60 - idx,
                            max_items=min(12, max(4, fill_budget)),
                            entity_key=profile.key,
                            relation=query.relation,
                            source_type=query.source_type,
                            source_url=query.source_url,
                        )
                    )
            soft_cap = {"A": 4, "B": 3, "C": 4, "D": 4}.get(module_code, 3)
            for idx, qcfg in enumerate(queries[:soft_cap]):
                if len(specs) >= 6:
                    break
                raw = str(qcfg.get("query") or "")
                scoped = (
                    with_google_day_scope(raw, self.calendar_day)
                    if day_scope
                    else strip_google_day_scope(raw)
                )
                specs.append(
                    RssQuerySpec(
                        label=f"google_fill_{idx+1}",
                        query=scoped,
                        enabled=True,
                        priority=50 - idx,
                        max_items=min(12, max(4, fill_budget)),
                    )
                )
            return [spec for spec in specs if (spec.query or "").strip()]

        def _keep(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
            kept: list[dict[str, Any]] = []
            for it in items:
                if self._count_valid_items([it], module_code) <= 0:
                    continue
                kept.append(it)
                if len(kept) >= fill_budget:
                    break
            return kept

        def _run_rss(
            specs: list[RssQuerySpec],
            *,
            calendar_day: date | None,
            hours: int,
        ) -> tuple[list[dict[str, Any]], str | None]:
            if not specs:
                return [], None
            detailed = self.rss.collect_detailed(
                module_code,
                hours=hours,
                max_items=min(self.collect_max_items, max(fill_budget * 2, 12)),
                extra_queries=specs,
                calendar_day=calendar_day,
                skip_direct_feeds=True,
                only_extra_queries=True,
                raise_on_all_fail=False,
            )
            items = [
                {
                    "title": h.title,
                    "url": h.url,
                    "snippet": h.snippet,
                    "published_at": h.published_at,
                    "source_domain": h.source_domain,
                    "feed": h.feed_label,
                    "fingerprint": h.fingerprint,
                    "publisher": h.publisher,
                    "entity_key": h.entity_key,
                    "relation": h.relation,
                    "source_type": h.source_type,
                    "configured_source_url": h.configured_source_url,
                    "_day_scoped": calendar_day is not None,
                }
                for h in detailed.items
            ]
            err = None
            if not items and detailed.fetch_errors and not detailed.fetch_ok:
                err = f"RSS 全部源请求失败（{detailed.fetch_errors} 个）"
            return _keep(items), err

        last_error: str | None = None
        kept: list[dict[str, Any]] = []
        fetched_total = 0

        day_specs = _build_specs(day_scope=True)
        if not day_specs:
            return {"source": "google_supplement", "items": [], "metadata": {}}

        try:
            kept, last_error = _run_rss(
                day_specs,
                calendar_day=self.calendar_day,
                hours=self.window_hours,
            )
            fetched_total += len(kept)
        except Exception as exc:
            last_error = str(exc)
            logger.warning("Google 补缺按日失败: %s", exc)

        if not kept:
            open_specs = _build_specs(day_scope=False)
            lookback_hours = max(self.window_hours, GOOGLE_LOOKBACK_DAYS * 24)
            logger.info("Google 补缺：按日无结果，放宽日界重试 %s 路", len(open_specs))
            try:
                kept, open_error = _run_rss(
                    open_specs,
                    calendar_day=None,
                    hours=lookback_hours,
                )
                fetched_total += len(kept)
                if open_error:
                    last_error = open_error
            except Exception as exc:
                last_error = str(exc)
                logger.warning("Google 补缺放宽日界失败: %s", exc)

        provider = "google_news"
        if not kept and allow_llm:
            query = strip_google_day_scope(day_specs[0].query or "")
            try:
                from app.services.llm_web_search import search_web_fallback

                rows, provider = search_web_fallback(
                    query, max_results=min(12, max(4, fill_budget))
                )
                llm_items: list[dict[str, Any]] = []
                for row in rows:
                    title = str(row.get("title") or "").strip()
                    url = str(row.get("url") or "").strip()
                    if not title:
                        continue
                    llm_items.append(
                        {
                            "title": title,
                            "url": url,
                            "snippet": str(row.get("snippet") or ""),
                            "published_at": row.get("published_at"),
                            "source_domain": row.get("source_domain"),
                            "feed": f"检索补缺·{provider}",
                            "publisher": row.get("source_domain"),
                            "fingerprint": content_fingerprint(
                                module_code=module_code,
                                title=title,
                                url=url,
                                published_at=row.get("published_at"),
                            ),
                        }
                    )
                kept = _keep(llm_items)
                fetched_total += len(llm_items)
                if kept:
                    last_error = None
                    logger.info(
                        "Google 补缺检索替代成功: provider=%s items=%s",
                        provider,
                        len(kept),
                    )
            except Exception as exc:
                last_error = str(exc)
                logger.warning("Google 补缺检索替代失败: %s", exc)

        funnel["google_supplement_fetched"] = int(
            funnel.get("google_supplement_fetched") or 0
        ) + fetched_total
        if last_error and not kept:
            funnel["google_supplement_error"] = last_error
        metadata: dict[str, Any] = {
            "source": "google_supplement",
            "calendar_day": funnel.get("calendar_day"),
            "provider": provider,
        }
        if last_error and not kept:
            metadata["error"] = last_error
        return {
            "source": "google_supplement",
            "items": kept,
            "metadata": metadata,
        }

    def _mita_query_budget(
        self, module_code: str, total_queries: int, gap: int, force: bool
    ) -> int:
        """按缺口限流秘塔查询条数，避免模块 C/D 全量串行。"""
        if total_queries <= 0:
            return 0
        if force:
            # 硬故障时多跑一些，但仍设上限
            cap = {"A": 4, "B": 2, "C": 4, "D": 4, "E": 2}.get(module_code, 4)
            return min(total_queries, max(2, cap))
        # 正常补缺：约每查询贡献 2～3 条有效结果估算
        needed = max(1, int(math.ceil(gap / 2.0)))
        soft_cap = {"A": 4, "B": 2, "C": 4, "D": 4, "E": 2}.get(module_code, 4)
        return min(total_queries, max(1, min(needed, soft_cap)))

    # ------------------------------------------------------------------
    # analyze helpers
    # ------------------------------------------------------------------

    def _analyze_merged(
        self,
        batches: list[dict[str, Any]],
        *,
        module_code: str,
        report_date: date,
        authority_text: str,
        funnel: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """多源合并：指纹+标题相似去重后全量分析入库（LLM 仅分批增强，不截断展示）。"""
        rows: list[dict[str, Any]] = []
        auth_batches = [b for b in batches if b.get("source") == "authority"]
        other_batches = [b for b in batches if b.get("source") != "authority"]

        for batch in auth_batches:
            rows.extend(
                self._analyze_batch(
                    batch,
                    module_code=module_code,
                    report_date=report_date,
                    authority_text=authority_text,
                    funnel=funnel,
                )
            )

        merged_items: list[dict[str, Any]] = []
        for batch in other_batches:
            for it in batch.get("items") or []:
                if not is_substantive_news_item(it):
                    continue
                item = dict(it)
                item["_batch_source"] = batch.get("source")
                item["_batch_meta"] = batch.get("metadata") or {}
                merged_items.append(item)

        if not merged_items and not auth_batches:
            return []
        if not merged_items:
            return rows

        unique = self._unique_source_items(merged_items, module_code)
        if module_code in NEWS_MODULES and not bool(
            getattr(self.settings, "intelligence_agent_ai_screening_enabled", True)
        ):
            before = len(unique)
            unique, dropped = filter_candidate_items(unique, module_code)
            funnel["scope_prefilter_dropped"] = int(
                funnel.get("scope_prefilter_dropped") or 0
            ) + dropped
            if dropped:
                logger.info(
                    "模块 %s LLM 前范围预过滤剔除 %s/%s 条",
                    module_code,
                    dropped,
                    before,
                )
        funnel["after_event_dedup"] = len(unique)
        metadata = {"source": "merged"}
        authority_first = bool(funnel.get("authority"))
        rows.extend(
            self._analyze_item_chunks(
                unique,
                module_code=module_code,
                report_date=report_date,
                authority_text=authority_text,
                metadata=metadata,
                authority_first=authority_first,
                funnel=funnel,
                source_label="merged",
            )
        )
        return rows

    def _analyze_batch(
        self,
        batch: dict[str, Any],
        *,
        module_code: str,
        report_date: date,
        authority_text: str,
        funnel: dict[str, Any],
    ) -> list[dict[str, Any]]:
        source = batch.get("source") or "unknown"
        metadata = dict(batch.get("metadata") or {})
        items = list(batch.get("items") or [])

        if source == "authority":
            text = batch.get("authority_text") or authority_text
            if not text:
                return []
            try:
                structured = self._llm_analyze(
                    text,
                    module_code=module_code,
                    report_date=report_date,
                    source=source,
                    authority_first=True,
                    funnel=funnel,
                    context={"report_date": report_date.isoformat(), "source": source},
                )
                return self._tag_rows(
                    structured, metadata, items, module_code=module_code, degraded=False
                )
            except Exception as exc:
                logger.warning("权威源 LLM 失败，跳过: %s", exc)
                funnel["source_fail"].append("authority_llm")
                return []

        if not items:
            return []

        real_items = [it for it in items if is_substantive_news_item(it)]
        if not real_items:
            logger.info("来源 %s 无实质资讯条目，跳过分析", source)
            return []

        unique = self._unique_source_items(real_items, module_code)
        return self._analyze_item_chunks(
            unique,
            module_code=module_code,
            report_date=report_date,
            authority_text=authority_text,
            metadata=metadata,
            authority_first=bool(authority_text),
            funnel=funnel,
            source_label=source,
        )

    def _unique_source_items(
        self, items: list[dict[str, Any]], module_code: str
    ) -> list[dict[str, Any]]:
        """URL 指纹去重 + 标题相似事件去重。"""
        seen: set[str] = set()
        unique: list[dict[str, Any]] = []
        for it in items:
            fp = it.get("fingerprint") or content_fingerprint(
                module_code=module_code,
                title=it.get("title"),
                url=it.get("url"),
                published_at=it.get("published_at"),
            )
            if fp in seen:
                continue
            seen.add(fp)
            it = dict(it)
            it["fingerprint"] = fp
            unique.append(it)
        return dedupe_by_title_similarity(unique)

    def _analyze_item_chunks(
        self,
        items: list[dict[str, Any]],
        *,
        module_code: str,
        report_date: date,
        authority_text: str,
        metadata: dict[str, Any],
        authority_first: bool,
        funnel: dict[str, Any],
        source_label: str,
    ) -> list[dict[str, Any]]:
        """按批调用 LLM；漏条再强制补分析一次，尽量避免「结构化分析暂不可用」。"""
        if not items:
            return []
        batch_size = max(1, self.llm_top_k)
        chunk_specs: list[tuple[int, list[dict[str, Any]]]] = [
            (start, items[start : start + batch_size])
            for start in range(0, len(items), batch_size)
        ]
        workers = min(self.llm_concurrency, len(chunk_specs))
        if workers <= 1:
            out: list[dict[str, Any]] = []
            for start, chunk in chunk_specs:
                out.extend(
                    self._analyze_one_chunk(
                        chunk,
                        chunk_offset=start,
                        module_code=module_code,
                        report_date=report_date,
                        authority_text=authority_text,
                        metadata=metadata,
                        authority_first=authority_first,
                        funnel=funnel,
                        source_label=source_label,
                    )
                )
            return out

        results: list[Optional[list[dict[str, Any]]]] = [None] * len(chunk_specs)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(
                    self._analyze_one_chunk,
                    chunk,
                    chunk_offset=start,
                    module_code=module_code,
                    report_date=report_date,
                    authority_text=authority_text,
                    metadata=metadata,
                    authority_first=authority_first,
                    funnel=funnel,
                    source_label=source_label,
                ): idx
                for idx, (start, chunk) in enumerate(chunk_specs)
            }
            for fut in as_completed(futures):
                idx = futures[fut]
                try:
                    results[idx] = fut.result()
                except Exception as exc:
                    start, chunk = chunk_specs[idx]
                    provider = "DeepSeek"
                    reason = _llm_failure_reason(exc, provider=provider)
                    logger.warning(
                        "LLM 并发批次异常，改用原文兜底 (%s#%s): %s [%s]",
                        source_label,
                        start,
                        exc,
                        reason,
                    )
                    fallback = self._fallback_structure_items(chunk, metadata)
                    with self._llm_db_lock:
                        funnel["degraded"] = int(funnel.get("degraded") or 0) + len(fallback)
                        funnel["structure_fallback"] = int(
                            funnel.get("structure_fallback") or 0
                        ) + len(fallback)
                        funnel["source_fail"].append(f"{source_label}_llm")
                    results[idx] = fallback

        out = []
        for part in results:
            if part:
                out.extend(part)
        return out

    def _analyze_one_chunk(
        self,
        chunk: list[dict[str, Any]],
        *,
        chunk_offset: int,
        module_code: str,
        report_date: date,
        authority_text: str,
        metadata: dict[str, Any],
        authority_first: bool,
        funnel: dict[str, Any],
        source_label: str,
    ) -> list[dict[str, Any]]:
        with self._llm_db_lock:
            funnel["sent_llm"] = int(funnel.get("sent_llm") or 0) + len(chunk)
        try:
            tagged = self._llm_tag_chunk(
                chunk,
                module_code=module_code,
                report_date=report_date,
                authority_text=authority_text,
                metadata=metadata,
                authority_first=authority_first,
                funnel=funnel,
                source_label=source_label,
                chunk_offset=chunk_offset,
                force_cover=False,
            )
            missing = self._unmatched_chunk_items(chunk, tagged)
            if missing:
                with self._llm_db_lock:
                    funnel["llm_cover_retry"] = int(funnel.get("llm_cover_retry") or 0) + len(
                        missing
                    )
                logger.info(
                    "LLM 未覆盖 %s 条，强制补分析 (%s#%s)",
                    len(missing),
                    source_label,
                    chunk_offset,
                )
                retry_tagged = self._llm_tag_chunk(
                    missing,
                    module_code=module_code,
                    report_date=report_date,
                    authority_text="",
                    metadata=metadata,
                    authority_first=False,
                    funnel=funnel,
                    source_label=f"{source_label}_cover",
                    chunk_offset=chunk_offset,
                    force_cover=True,
                )
                tagged = list(tagged) + list(retry_tagged)
            return self._reconcile_chunk_rows(
                chunk, tagged, metadata, module_code=module_code, funnel=funnel
            )
        except Exception as exc:
            provider = "DeepSeek"
            reason = _llm_failure_reason(exc, provider=provider)
            logger.warning(
                "LLM 分析失败，改用原文结构化兜底 (%s#%s): %s [%s]",
                source_label,
                chunk_offset,
                exc,
                reason,
            )
            fallback = self._fallback_structure_items(chunk, metadata)
            with self._llm_db_lock:
                funnel["degraded"] = int(funnel.get("degraded") or 0) + len(fallback)
                funnel["structure_fallback"] = int(funnel.get("structure_fallback") or 0) + len(
                    fallback
                )
                funnel["source_fail"].append(f"{source_label}_llm")
            return fallback

    def _llm_tag_chunk(
        self,
        chunk: list[dict[str, Any]],
        *,
        module_code: str,
        report_date: date,
        authority_text: str,
        metadata: dict[str, Any],
        authority_first: bool,
        funnel: dict[str, Any],
        source_label: str,
        chunk_offset: int,
        force_cover: bool,
    ) -> list[dict[str, Any]]:
        if not chunk:
            return []
        web_text = json.dumps(chunk, ensure_ascii=False, indent=2)
        combined = web_text
        if authority_text:
            combined = (
                f"=== 权威数据源（优先参考）===\n{authority_text[:20000]}\n\n"
                f"=== {source_label} 近{self.window_hours}小时候选 ===\n{web_text}"
            )
        structured = None
        last_exc: Optional[BaseException] = None
        for attempt in range(2):
            try:
                structured = self._llm_analyze(
                    combined,
                    module_code=module_code,
                    report_date=report_date,
                    source=f"{source_label}:{chunk_offset}:c{int(force_cover)}",
                    authority_first=authority_first,
                    funnel=funnel,
                    context={
                        "report_date": report_date.isoformat(),
                        "source": source_label,
                        "window_hours": self.window_hours,
                        "chunk_offset": chunk_offset,
                        "chunk_size": len(chunk),
                        "attempt": attempt + 1,
                        "force_cover": force_cover,
                        "target_entity": (
                            (self._selected_entity().display_name or self._selected_entity().name)
                            if module_code in ENTITY_MODULES and self._selected_entity()
                            else None
                        ),
                    },
                )
                last_exc = None
                break
            except Exception as exc:
                last_exc = exc
                if attempt == 0:
                    logger.warning(
                        "LLM 分析失败将重试一次 (%s#%s): %s",
                        source_label,
                        chunk_offset,
                        exc,
                    )
                    time.sleep(0.8)
                    continue
        if last_exc is not None:
            raise last_exc
        if not structured:
            return []
        return filter_publishable_rows(
            self._tag_rows(
                structured,
                metadata,
                chunk,
                module_code=module_code,
                degraded=False,
            )
        )

    def _unmatched_chunk_items(
        self,
        chunk: list[dict[str, Any]],
        tagged: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        used: set[int] = set()
        for row in tagged:
            src_url = (row.get("来源链接") or "").strip()
            title = row.get("标题") or ""
            for i, it in enumerate(chunk):
                if i in used:
                    continue
                if src_url and (it.get("url") or "").strip() == src_url:
                    used.add(i)
                    break
                if titles_similar(title, it.get("title")):
                    used.add(i)
                    break
        return [it for i, it in enumerate(chunk) if i not in used]

    def _reconcile_chunk_rows(
        self,
        chunk: list[dict[str, Any]],
        tagged: list[dict[str, Any]],
        metadata: dict[str, Any],
        *,
        module_code: str,
        funnel: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """将 LLM 结果与候选对齐；仍缺的用原文生成可展示结构化行（不再展示系统失败提示）。"""
        used: set[int] = set()
        rows: list[dict[str, Any]] = []
        for row in tagged:
            rows.append(row)
            src_url = (row.get("来源链接") or "").strip()
            title = row.get("标题") or ""
            for i, it in enumerate(chunk):
                if i in used:
                    continue
                if src_url and (it.get("url") or "").strip() == src_url:
                    used.add(i)
                    break
                if titles_similar(title, it.get("title")):
                    used.add(i)
                    break
        missing = [it for i, it in enumerate(chunk) if i not in used]
        if missing:
            # 末级兜底：用原文组装正常字段，避免页面出现「结构化分析暂不可用」
            extra = self._fallback_structure_items(missing, metadata)
            with self._llm_db_lock:
                funnel["degraded"] = int(funnel.get("degraded") or 0) + len(extra)
                funnel["structure_fallback"] = int(funnel.get("structure_fallback") or 0) + len(
                    extra
                )
            rows.extend(extra)
        return dedupe_by_title_similarity(
            rows, title_getter=lambda r: r.get("标题") or r.get("title")
        )

    def _fallback_structure_items(
        self,
        items: list[dict[str, Any]],
        metadata: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """LLM 未覆盖时保留原始摘要，并显式标为降级数据。"""
        rows: list[dict[str, Any]] = []
        for it in items:
            if not is_substantive_news_item(it):
                continue
            title = (it.get("title") or "未命名条目").strip()
            snippet = (it.get("snippet") or it.get("body") or "").strip()
            if len(snippet) < 12:
                continue
            # 降级时也保留抓取到的完整正文，供页面“内容详情”直接呈现。
            summary = snippet
            is_entity = bool(self.entity_id)
            rows.append(
                {
                    "标题": title,
                    "关联企业": it.get("company")
                    or metadata.get("company")
                    or metadata.get("target")
                    or it.get("entity_key")
                    or "",
                    "风险类别": metadata.get("category") or metadata.get("topic") or "资讯快讯",
                    "风险等级": "低" if is_entity else "中",
                    "核心摘要": summary,
                    "影响分析": (
                        _degraded_impact_text("模型未完成主体信用信号判定")
                        if is_entity
                        else summary[:280]
                    ),
                    "来源链接": it.get("url") or "",
                    "来源名称": it.get("publisher") or it.get("feed") or "",
                    "发布时间": it.get("published_at") or "",
                    "资讯重要度": "中" if is_entity else "",
                    "影响方向": "unknown" if is_entity else "",
                    "信用风险信号": "none" if is_entity else "",
                    "主体相关性": it.get("relation") or "unknown",
                    "置信度": "",
                    "_degraded": True,
                    "_structure_fallback": True,
                    "_fingerprint": it.get("fingerprint"),
                    "_metadata": metadata,
                    "_source_item": it,
                }
            )
        return filter_publishable_rows(rows)

    def _llm_analyze(
        self,
        text: str,
        *,
        module_code: str,
        report_date: date,
        source: str,
        authority_first: bool,
        funnel: dict[str, Any],
        context: dict[str, Any],
    ) -> list[dict[str, Any]]:
        target_cache_key = str(context.get("target_entity") or "")
        # 摘要提示词升级后变更版本，避免复用旧的短摘要缓存。
        cache_source = f"{source}:w{self.window_hours}:target={target_cache_key}:body-summary-impact-v5"
        key = material_hash(
            text,
            module_code=module_code,
            source=cache_source,
        )
        with self._llm_db_lock:
            cached = get_cached_items(self.db, material_key=key, max_age_hours=self.cache_hours)
            if cached is not None:
                funnel["llm_cached"] = int(funnel.get("llm_cached") or 0) + 1
                funnel["structured"] = int(funnel.get("structured") or 0) + len(cached)
                return cached
        # 统一使用 DeepSeek 生成新闻摘要、风险标签与主题评估分析。
        analyzer = self.deepseek
        structured = analyzer.analyze_raw(
            text,
            module_code=module_code,
            context=context,
            authority_first=authority_first,
        )
        with self._llm_db_lock:
            set_cached_items(
                self.db,
                material_key=key,
                module_code=module_code,
                source=cache_source,
                items=structured,
            )
            funnel["structured"] = int(funnel.get("structured") or 0) + len(structured)
        return structured

    def _prefilter_items(self, items: list[dict[str, Any]], top_k: int) -> list[dict[str, Any]]:
        def score(it: dict[str, Any]) -> tuple:
            body = (it.get("body") or it.get("snippet") or "")
            has_body = 1 if len(body) > 80 else 0
            has_time = 1 if it.get("published_at") else 0
            return (has_body, has_time, len(body), len(it.get("title") or ""))

        ranked = sorted(items, key=score, reverse=True)
        return ranked[: max(1, top_k)]

    def _degrade_items(
        self,
        items: list[dict[str, Any]],
        metadata: dict[str, Any],
        *,
        reason: str = "模型分析失败",
    ) -> list[dict[str, Any]]:
        rows = []
        impact = _degraded_impact_text(reason)
        for it in items:
            if not is_substantive_news_item(it):
                continue
            title = (it.get("title") or "未命名条目").strip()
            snippet = (it.get("snippet") or it.get("body") or "").strip()
            if len(snippet) < 12:
                continue
            is_entity = bool(self.entity_id)
            rows.append(
                {
                    "标题": title,
                    "关联企业": it.get("company")
                    or metadata.get("company")
                    or metadata.get("target")
                    or it.get("entity_key")
                    or "",
                    "风险类别": metadata.get("category") or metadata.get("topic") or "资讯快讯",
                    "风险等级": "低" if is_entity else "中",
                    "核心摘要": snippet,
                    "影响分析": impact,
                    "来源链接": it.get("url") or "",
                    "来源名称": it.get("publisher") or it.get("feed") or "",
                    "发布时间": it.get("published_at") or "",
                    "资讯重要度": "中" if is_entity else "",
                    "影响方向": "unknown" if is_entity else "",
                    "信用风险信号": "none" if is_entity else "",
                    "主体相关性": it.get("relation") or "unknown",
                    "置信度": "",
                    "_degraded": True,
                    "_degrade_reason": reason,
                    "_fingerprint": it.get("fingerprint"),
                    "_metadata": metadata,
                    "_source_item": it,
                }
            )
        return filter_publishable_rows(rows)

    def _purge_out_of_scope_entries(self, report_date: date, module_code: str) -> int:
        """删除已入库但不符合当前板块范围的条目（如中东栏里的科普/彩票）。"""
        if module_code not in NEWS_MODULES:
            return 0
        removed = 0
        rows = (
            self.db.query(NewsArticle)
            .filter(
                NewsArticle.report_date == report_date,
                NewsArticle.module_code == module_code,
                NewsArticle.window_hours == self.persist_hours,
            )
            .all()
        )
        for row in rows:
            # Modified by DingJiaye: 2026-08-27 — AI 筛选模式下，已入库事件的
            # 相关性以 AI 正文审核为准，不再被静态别名/信源名单的二次清理删除。
            if bool(getattr(self.settings, "intelligence_agent_ai_screening_enabled", True)):
                continue
            ok, _reason = item_in_module_scope(
                module_code,
                title=row.title or "",
                content=f"{row.summary or ''} {row.impact_analysis or ''}",
                source=str(row.source_title or row.source_url or ""),
                related_company=str(row.related_company or ""),
            )
            if ok:
                continue
            legacy_id = row.legacy_entry_id
            row.legacy_entry_id = None
            self.db.flush()
            self.db.delete(row)
            self.db.flush()
            if legacy_id:
                self.db.query(DailyRiskEntry).filter(
                    DailyRiskEntry.id == legacy_id
                ).delete(synchronize_session=False)
            removed += 1
        legacy = (
            self.db.query(DailyRiskEntry)
            .filter(
                DailyRiskEntry.report_date == report_date,
                DailyRiskEntry.module_code == module_code,
                DailyRiskEntry.window_hours == self.persist_hours,
            )
            .all()
        )
        for row in legacy:
            if bool(getattr(self.settings, "intelligence_agent_ai_screening_enabled", True)):
                continue
            still = (
                self.db.query(NewsArticle)
                .filter(NewsArticle.legacy_entry_id == row.id)
                .first()
            )
            if still:
                continue
            ok, _reason = item_in_module_scope(
                module_code,
                title=row.title or "",
                content=f"{row.summary or ''} {row.impact_analysis or ''}",
                source=str(row.source_title or row.source_url or ""),
                related_company=str(row.related_company or ""),
            )
            if ok:
                continue
            self.db.delete(row)
            removed += 1
        if removed:
            self.db.commit()
            logger.info("模块 %s 已清理越界条目 %d 条", module_code, removed)
        return removed

    def _purge_non_news_placeholders(self, report_date: date, module_code: str) -> int:
        """删除已入库的披露检索等非新闻占位。"""
        removed = 0
        if module_code in NEWS_MODULES:
            rows = (
                self.db.query(NewsArticle)
                .filter(
                    NewsArticle.report_date == report_date,
                    NewsArticle.module_code == module_code,
                    NewsArticle.window_hours == self.persist_hours,
                )
                .all()
            )
            for row in rows:
                probe = {
                    "title": row.title,
                    "snippet": row.summary,
                    "url": row.source_url,
                    "影响分析": row.impact_analysis,
                    "核心摘要": row.summary,
                }
                impact = row.impact_analysis or ""
                if is_reference_only_item(probe) or (
                    "DeepSeek 暂不可用" in impact and is_reference_only_item(probe)
                ) or (
                    "披露检索" in (row.title or "")
                    or "法定披露" in (row.title or "")
                ):
                    # 同步删 legacy
                    if row.legacy_entry_id:
                        self.db.query(DailyRiskEntry).filter(
                            DailyRiskEntry.id == row.legacy_entry_id
                        ).delete(synchronize_session=False)
                    self.db.delete(row)
                    removed += 1
        legacy = (
            self.db.query(DailyRiskEntry)
            .filter(
                DailyRiskEntry.report_date == report_date,
                DailyRiskEntry.module_code == module_code,
                DailyRiskEntry.window_hours == self.persist_hours,
            )
            .all()
        )
        for row in legacy:
            if (
                "披露检索" in (row.title or "")
                or "法定披露" in (row.title or "")
                or "DeepSeek 暂不可用" in (row.impact_analysis or "")
            ):
                # 若 news 已删，这里补漏；若仍有 news 指向则上面已处理
                still = (
                    self.db.query(NewsArticle)
                    .filter(NewsArticle.legacy_entry_id == row.id)
                    .first()
                )
                if not still:
                    self.db.delete(row)
                    removed += 1
        if removed:
            self.db.commit()
            logger.info("模块 %s 已清理非新闻占位 %d 条", module_code, removed)
        return removed

    def _tag_rows(
        self,
        structured: list[dict[str, Any]],
        metadata: dict[str, Any],
        source_items: list[dict[str, Any]],
        *,
        module_code: str,
        degraded: bool,
    ) -> list[dict[str, Any]]:
        out = []
        for row in structured:
            enriched = dict(row)
            enriched["_metadata"] = metadata
            enriched["_source_items"] = source_items
            enriched["_degraded"] = degraded
            src_url = (row.get("来源链接") or "").strip()
            matched_source = None
            if src_url and source_items:
                for it in source_items:
                    if (it.get("url") or "").strip() == src_url:
                        matched_source = it
                        break
            if matched_source is None and source_items:
                title = row.get("标题") or ""
                for it in source_items:
                    if titles_similar(title, it.get("title")):
                        matched_source = it
                        break
            if matched_source is None and len(source_items) == 1:
                matched_source = source_items[0]
            if matched_source is not None:
                enriched["_fingerprint"] = matched_source.get("fingerprint")
                enriched["_source_item"] = matched_source
            if not enriched.get("_fingerprint"):
                enriched["_fingerprint"] = content_fingerprint(
                    module_code=module_code,
                    title=row.get("标题"),
                    url=src_url or None,
                    published_at=row.get("发布时间"),
                )
            out.append(enriched)
        return out

    def _dedupe_structured(
        self, rows: list[dict[str, Any]], module_code: str, funnel: dict[str, Any]
    ) -> list[dict[str, Any]]:
        seen_fp: set[str] = set()
        unique: list[dict[str, Any]] = []
        for row in rows:
            fp = row.get("_fingerprint") or content_fingerprint(
                module_code=module_code,
                title=row.get("标题"),
                url=row.get("来源链接"),
                published_at=row.get("发布时间"),
            )
            if fp in seen_fp:
                continue
            seen_fp.add(fp)
            row["_fingerprint"] = fp
            unique.append(row)
        # 跨媒体同一事件：标题相似合并
        unique = dedupe_by_title_similarity(
            unique, title_getter=lambda r: r.get("标题") or r.get("title")
        )
        funnel["after_dedup"] = len(unique)
        return unique

    # ------------------------------------------------------------------
    # publish / storage
    # ------------------------------------------------------------------

    def _ensure_run(self, report_date: date, module_code: str) -> ReportRun:
        run = (
            self.db.query(ReportRun)
            .filter(
                ReportRun.report_date == report_date,
                ReportRun.module_code == module_code,
                ReportRun.window_hours == self.persist_hours,
            )
            .first()
        )
        if not run:
            run = ReportRun(
                report_date=report_date,
                module_code=module_code,
                window_hours=self.persist_hours,
                status="running",
            )
            self.db.add(run)
        else:
            run.status = "running"
        run.started_at = tokyo_now()
        run.finished_at = None
        run.entry_count = 0
        run.kept_previous = False
        run.phase = "collect"
        run.job_id = self.job_id
        run.notes = None
        self.db.commit()
        return run

    def _selected_entity(self):
        if not self.entity_id:
            return None
        if self._entity_cache is None:
            from app.database.models import TargetEntity

            self._entity_cache = (
                self.db.query(TargetEntity).filter(TargetEntity.id == self.entity_id).first()
            )
        return self._entity_cache

    def _entity_profile(self) -> EntityProfile | None:
        if self._entity_profile_loaded:
            return self._entity_profile_cache
        self._entity_profile_loaded = True
        entity = self._selected_entity()
        if entity:
            self._entity_profile_cache = configured_entity_catalog().find(
                (entity.name, entity.display_name, entity.aliases)
            )
        return self._entity_profile_cache

    def _filter_entity_rows(
        self, rows: list[dict[str, Any]], funnel: dict[str, Any]
    ) -> list[dict[str, Any]]:
        entity = self._selected_entity()
        if not entity:
            return rows
        profile = self._entity_profile()
        accepted: list[dict[str, Any]] = []
        for row in rows:
            # Modified by DingJiaye: 2026-08-27 — 主题评估由 AI 根据正文判断主体、
            # 股东及关联方相关性。静态名单只在模型降级时用于安全兜底。
            ai_screening = bool(
                getattr(self.settings, "intelligence_agent_entity_screening_enabled", True)
            ) and not bool(row.get("_degraded"))
            accepted_row = (
                prepare_ai_entity_row(row)
                if ai_screening
                else prepare_entity_row(entity, row, profile)
            )
            if accepted_row:
                accepted.append(row)
        funnel["entity_relevance_dropped"] = len(rows) - len(accepted)
        funnel["entity_direct"] = sum(
            1 for row in accepted if row.get("_entity_relevance") == "direct"
        )
        funnel["entity_contextual"] = sum(
            1 for row in accepted if row.get("_entity_relevance") == "contextual"
        )
        return accepted

    def _entity_search_targets(self) -> list[str] | None:
        """主体评估限定检索目标；未指定 entity_id 则搜全部默认主体。"""
        if not self.entity_id:
            return None
        ent = self._selected_entity()
        if not ent:
            return None
        profile = self._entity_profile()
        names = list(profile.all_names[:3]) if profile else [ent.display_name or ent.name, ent.name]
        return [n for n in dict.fromkeys(names) if n]

    def _count_existing(self, report_date: date, module_code: str) -> int:
        if module_code in NEWS_MODULES:
            return (
                self.db.query(NewsArticle)
                .filter(
                    NewsArticle.report_date == report_date,
                    NewsArticle.module_code == module_code,
                    NewsArticle.window_hours == self.persist_hours,
                )
                .count()
            )
        if module_code in ENTITY_MODULES:
            q = self.db.query(EntityRisk).filter(EntityRisk.report_date == report_date)
            if self.entity_id:
                q = q.filter(EntityRisk.entity_id == self.entity_id)
            return q.count()
        return (
            self.db.query(DailyRiskEntry)
            .filter(
                DailyRiskEntry.report_date == report_date,
                DailyRiskEntry.module_code == module_code,
                DailyRiskEntry.window_hours == self.persist_hours,
            )
            .count()
        )

    def _replace_module_entries(self, report_date: date, module_code: str) -> None:
        """仅在确定有新结果要发布时调用。"""
        if module_code in NEWS_MODULES:
            self.db.query(NewsArticle).filter(
                NewsArticle.report_date == report_date,
                NewsArticle.module_code == module_code,
                NewsArticle.window_hours == self.persist_hours,
            ).delete(synchronize_session=False)

        if module_code in ENTITY_MODULES:
            from app.database.models import CreditUpdate

            q = self.db.query(EntityRisk.id).filter(EntityRisk.report_date == report_date)
            if self.entity_id:
                q = q.filter(EntityRisk.entity_id == self.entity_id)
            risk_ids = [r[0] for r in q.all()]
            if risk_ids:
                self.db.query(CreditUpdate).filter(
                    CreditUpdate.trigger_risk_id.in_(risk_ids)
                ).update({CreditUpdate.trigger_risk_id: None}, synchronize_session=False)
                self.db.query(EntityRisk).filter(EntityRisk.id.in_(risk_ids)).delete(
                    synchronize_session=False
                )

        # 主体限定采集时不整模块清空 DailyRiskEntry，避免误删其他主体对应旧条目
        if not (module_code in ENTITY_MODULES and self.entity_id):
            self.db.query(DailyRiskEntry).filter(
                DailyRiskEntry.report_date == report_date,
                DailyRiskEntry.module_code == module_code,
                DailyRiskEntry.window_hours == self.persist_hours,
            ).delete(synchronize_session=False)
        self.db.commit()

    def _save_artifact(
        self,
        report_date: date,
        module_code: str,
        phase: str,
        source: str,
        payload: dict[str, Any],
        *,
        item_count: int,
        db: Optional[Session] = None,
    ) -> None:
        session = db or self.db
        try:
            session.add(
                PipelineArtifact(
                    job_id=self.job_id,
                    report_date=report_date,
                    module_code=module_code,
                    phase=phase,
                    source=source,
                    payload_json=json.dumps(payload, ensure_ascii=False)[:500000],
                    item_count=item_count,
                )
            )
            session.commit()
        except Exception as exc:
            session.rollback()
            logger.warning("中间落盘失败（不影响主流程）: %s", exc)

    def _set_funnel(self, run: ReportRun, funnel: dict[str, Any]) -> None:
        run.funnel_json = json.dumps(funnel, ensure_ascii=False)

    def _resolve_published_at(
        self,
        row: dict[str, Any],
        source_items: Optional[list[dict[str, Any]]],
    ) -> Optional[datetime]:
        candidates = [
            row.get("发布时间"),
            row.get("published_at"),
        ]
        src_item = row.get("_source_item")
        if isinstance(src_item, dict):
            candidates.append(src_item.get("published_at"))
        src_url = (row.get("来源链接") or "").strip()
        items = source_items or row.get("_source_items")
        if items and src_url:
            for item in items:
                if (item.get("url") or "").strip() == src_url:
                    candidates.append(item.get("published_at"))
                    break
        for cand in candidates:
            dt = parse_published_at(cand)
            if dt:
                return dt.replace(tzinfo=None)
        return None

    def sync_dual_route_mirrors(self, report_date: date) -> int:
        """全量任务结束后：将双投 [B,D] 条目补齐到另一板块（不整表清空）。"""
        if not NEWS_MODULES:
            return 0
        sources = (
            self.db.query(DailyRiskEntry)
            .filter(
                DailyRiskEntry.report_date == report_date,
                DailyRiskEntry.module_code.in_(tuple(NEWS_MODULES)),
                DailyRiskEntry.window_hours == self.persist_hours,
            )
            .all()
        )
        if not sources:
            return 0

        existing_keys: set[tuple[str, str]] = set()
        for e in sources:
            key = (e.module_code or "", (e.title or "").strip().lower())
            existing_keys.add(key)
            if e.source_url:
                existing_keys.add((e.module_code or "", f"url:{(e.source_url or '').strip().lower()}"))

        mirrored = 0
        for entry in sources:
            route = route_news_sections(
                title=entry.title or "",
                content=f"{entry.summary or ''} {entry.impact_analysis or ''}",
                source=entry.source_url or "",
                related_company=entry.related_company or "",
            )
            if len(route.sections) < 2:
                continue
            for target in route.sections:
                if target == entry.module_code:
                    continue
                title_key = (target, (entry.title or "").strip().lower())
                url_key = (
                    (target, f"url:{(entry.source_url or '').strip().lower()}")
                    if entry.source_url
                    else None
                )
                if title_key in existing_keys or (url_key and url_key in existing_keys):
                    continue
                clone = DailyRiskEntry(
                    report_date=report_date,
                    module_code=target,
                    country_or_region=entry.country_or_region,
                    target_entity=entry.target_entity,
                    title=entry.title,
                    related_company=entry.related_company,
                    risk_category=entry.risk_category,
                    risk_level=entry.risk_level,
                    summary=entry.summary,
                    impact_analysis=entry.impact_analysis,
                    source_url=entry.source_url,
                    source_title=entry.source_title,
                    pillar_or_topic=entry.pillar_or_topic,
                    structured_json=entry.structured_json,
                    search_log_id=entry.search_log_id,
                    published_at=entry.published_at,
                    window_hours=self.persist_hours,
                )
                self.db.add(clone)
                self.db.flush()
                # 同步 NewsArticle（若源侧有）
                src_news = (
                    self.db.query(NewsArticle)
                    .filter(NewsArticle.legacy_entry_id == entry.id)
                    .first()
                )
                if src_news:
                    self.db.add(
                        NewsArticle(
                            report_date=report_date,
                            module_code=target,
                            window_hours=self.persist_hours,
                            category_tag=src_news.category_tag,
                            country_or_region=src_news.country_or_region,
                            target_entity=src_news.target_entity,
                            title=src_news.title,
                            related_company=src_news.related_company,
                            risk_category=src_news.risk_category,
                            risk_level=src_news.risk_level,
                            summary=src_news.summary,
                            impact_analysis=src_news.impact_analysis,
                            source_url=src_news.source_url,
                            source_title=src_news.source_title,
                            structured_json=src_news.structured_json,
                            published_at=src_news.published_at,
                            legacy_entry_id=clone.id,
                        )
                    )
                else:
                    self.db.add(
                        NewsArticle(
                            report_date=report_date,
                            module_code=target,
                            window_hours=self.persist_hours,
                            category_tag=entry.pillar_or_topic or entry.risk_category,
                            country_or_region=entry.country_or_region,
                            target_entity=entry.target_entity,
                            title=entry.title,
                            related_company=entry.related_company,
                            risk_category=entry.risk_category,
                            risk_level=entry.risk_level,
                            summary=entry.summary or "",
                            impact_analysis=entry.impact_analysis,
                            source_url=entry.source_url,
                            source_title=entry.source_title,
                            structured_json=entry.structured_json,
                            published_at=entry.published_at,
                            legacy_entry_id=clone.id,
                        )
                    )
                existing_keys.add(title_key)
                if url_key:
                    existing_keys.add(url_key)
                mirrored += 1
                logger.info(
                    "双投镜像 %s → %s: %s",
                    entry.module_code,
                    target,
                    (entry.title or "")[:80],
                )

        if mirrored:
            self.db.commit()
        return mirrored

    def _save_structured_entries(
        self,
        structured: list[dict[str, Any]],
        *,
        module_code: str,
        report_date: date,
        metadata: dict[str, Any],
        search_log_id: Optional[int],
        raw_context: str,
        source_items: Optional[list[dict[str, Any]]] = None,
        register_fingerprints: bool = False,
    ) -> int:
        chart_specs, _ = extract_and_build_charts(raw_context)
        chart_json = json.dumps(chart_specs, ensure_ascii=False) if chart_specs else None

        count = 0
        touched_entities: dict[int, Any] = {}
        # Modified by DingJiaye: 2026-08-27 — 仅收集本次新入库主体事件，
        # 提交成功后再分发高/极高预警，避免事务未落库即外发。
        new_entity_risks: list[EntityRisk] = []
        seen_titles: set[str] = set()

        for row in structured:
            title = (row.get("标题") or "未命名条目").strip()
            title_key = title.lower()
            if title_key in seen_titles:
                continue

            published_at = self._resolve_published_at(row, source_items)
            if published_at and not is_within_hours(
                published_at, self.window_hours, allow_unknown=False
            ):
                # 已知发布时间超窗一律丢弃；降级状态不能绕过时间边界。
                continue
            if not self._published_on_calendar_day(published_at):
                continue

            seen_titles.add(title_key)
            meta = dict(row.get("_metadata") or metadata or {})
            enriched = {
                k: v
                for k, v in row.items()
                if not str(k).startswith("_")
            }
            if chart_json and count == 0:
                enriched["_chart_specs"] = chart_json
            if published_at:
                enriched["发布时间"] = published_at.isoformat()
            if row.get("_degraded"):
                enriched["_degraded"] = True
            target_hint = (
                meta.get("target")
                or meta.get("company")
                or meta.get("topic")
            )
            source_name = (row.get("来源名称") or "").strip() or None
            provenance = "degraded" if row.get("_degraded") else "real"
            entry = DailyRiskEntry(
                report_date=report_date,
                module_code=module_code,
                country_or_region=meta.get("region") or meta.get("country"),
                target_entity=target_hint,
                title=title,
                related_company=row.get("关联企业"),
                risk_category=row.get("风险类别"),
                risk_level=row.get("风险等级") or "中",
                summary=row.get("核心摘要") or "",
                impact_analysis=row.get("影响分析"),
                source_url=row.get("来源链接"),
                source_title=source_name,
                pillar_or_topic=meta.get("category") or meta.get("topic"),
                structured_json=json.dumps(enriched, ensure_ascii=False),
                search_log_id=search_log_id or row.get("_search_log_id"),
                published_at=published_at,
                window_hours=self.persist_hours,
            )
            self.db.add(entry)
            self.db.flush()

            if register_fingerprints:
                fp = row.get("_fingerprint") or content_fingerprint(
                    module_code=module_code,
                    title=title,
                    url=row.get("来源链接"),
                    published_at=row.get("发布时间"),
                )
                exists = (
                    self.db.query(ContentFingerprint)
                    .filter(
                        ContentFingerprint.module_code == module_code,
                        ContentFingerprint.fingerprint == fp,
                    )
                    .first()
                )
                if not exists:
                    self.db.add(
                        ContentFingerprint(
                            module_code=module_code,
                            fingerprint=fp,
                            title=title[:512],
                            source_url=(row.get("来源链接") or "")[:1024] or None,
                            report_date=report_date,
                        )
                    )

            if module_code in NEWS_MODULES:
                self.db.add(
                    NewsArticle(
                        report_date=entry.report_date,
                        module_code=entry.module_code,
                        window_hours=self.persist_hours,
                        category_tag=entry.pillar_or_topic or entry.risk_category,
                        country_or_region=entry.country_or_region,
                        target_entity=entry.target_entity,
                        title=entry.title,
                        related_company=entry.related_company,
                        risk_category=entry.risk_category,
                        risk_level=entry.risk_level,
                        summary=entry.summary,
                        impact_analysis=entry.impact_analysis,
                        source_url=entry.source_url,
                        source_title=entry.source_title,
                        structured_json=entry.structured_json,
                        published_at=published_at,
                        legacy_entry_id=entry.id,
                    )
                )
            elif module_code in ENTITY_MODULES:
                ent = None
                if self.entity_id:
                    from app.database.models import TargetEntity

                    ent = (
                        self.db.query(TargetEntity)
                        .filter(TargetEntity.id == self.entity_id)
                        .first()
                    )
                if not ent:
                    ent = resolve_entity(
                        self.db,
                        name_hint=target_hint,
                        related_company=row.get("关联企业"),
                        create_if_missing=True,
                    )
                if ent:
                    risk = EntityRisk(
                        entity_id=ent.id,
                        report_date=report_date,
                        title=entry.title,
                        risk_category=entry.risk_category,
                        risk_level=entry.risk_level,
                        summary=entry.summary,
                        impact_analysis=entry.impact_analysis,
                        source_url=entry.source_url,
                        source_name=source_name,
                        published_at=published_at,
                        related_company=entry.related_company,
                        provenance=provenance,
                        relevance=str(
                            row.get("_entity_relevance")
                            or row.get("主体相关性")
                            or "unknown"
                        ).lower(),
                        news_importance=row.get("资讯重要度") or None,
                        sentiment_direction=str(
                            row.get("影响方向") or "unknown"
                        ).lower(),
                        credit_impact=str(
                            row.get("信用风险信号") or "none"
                        ).lower(),
                        confidence=row.get("_confidence"),
                        review_status="pending",
                        rule_version="entity-signal-v1",
                        structured_json=entry.structured_json,
                        legacy_entry_id=entry.id,
                    )
                    self.db.add(risk)
                    self.db.flush()
                    touched_entities[ent.id] = ent
                    new_entity_risks.append(risk)

            count += 1

        for ent in touched_entities.values():
            refresh_entity_credit(
                self.db,
                ent,
                reason="公开信息信用风险信号更新（规则 entity-signal-v1）",
                force=True,
            )

        self.db.commit()
        if new_entity_risks:
            from app.services.alert_notifications import dispatch_entity_risk_alerts_async

            dispatch_entity_risk_alerts_async([risk.id for risk in new_entity_risks])
        return count

    def ingest_manual_entries(
        self,
        module_code: str,
        report_date: date,
        entries: list[dict[str, str]],
    ) -> int:
        """手工录入或爬虫结果直接结构化入库。"""
        code = module_code.upper()
        count = 0
        touched = []
        for row in entries:
            published_at = parse_published_at(row.get("发布时间") or row.get("published_at"))
            if published_at:
                published_at = published_at.replace(tzinfo=None)
            entry = DailyRiskEntry(
                report_date=report_date,
                module_code=code,
                title=row.get("标题", ""),
                related_company=row.get("关联企业"),
                risk_category=row.get("风险类别"),
                risk_level=row.get("风险等级") or "中",
                summary=row.get("核心摘要") or "",
                impact_analysis=row.get("影响分析"),
                source_url=row.get("来源链接"),
                source_title=row.get("来源名称"),
                structured_json=json.dumps(row, ensure_ascii=False),
                published_at=published_at,
            )
            self.db.add(entry)
            self.db.flush()

            if code in NEWS_MODULES:
                self.db.add(
                    NewsArticle(
                        report_date=report_date,
                        module_code=code,
                        category_tag=row.get("风险类别"),
                        title=entry.title,
                        related_company=entry.related_company,
                        risk_category=entry.risk_category,
                        risk_level=entry.risk_level,
                        summary=entry.summary,
                        impact_analysis=entry.impact_analysis,
                        source_url=entry.source_url,
                        structured_json=entry.structured_json,
                        published_at=published_at,
                        legacy_entry_id=entry.id,
                    )
                )
            elif code in ENTITY_MODULES:
                ent = resolve_entity(
                    self.db,
                    name_hint=row.get("关联企业"),
                    related_company=row.get("关联企业"),
                    create_if_missing=True,
                )
                if ent:
                    risk = EntityRisk(
                        entity_id=ent.id,
                        report_date=report_date,
                        title=entry.title,
                        risk_category=entry.risk_category,
                        risk_level=entry.risk_level,
                        summary=entry.summary,
                        impact_analysis=entry.impact_analysis,
                        source_url=entry.source_url,
                        source_name=entry.source_title,
                        published_at=published_at,
                        related_company=entry.related_company,
                        provenance="manual",
                        relevance=str(row.get("主体相关性") or "direct").lower(),
                        news_importance=row.get("资讯重要度") or row.get("风险等级"),
                        sentiment_direction=str(row.get("影响方向") or "unknown").lower(),
                        credit_impact=str(row.get("信用风险信号") or "none").lower(),
                        confidence=(
                            float(row["置信度"])
                            if row.get("置信度") not in (None, "")
                            else None
                        ),
                        review_status="accepted",
                        rule_version="entity-signal-v1",
                        structured_json=entry.structured_json,
                        legacy_entry_id=entry.id,
                    )
                    self.db.add(risk)
                    self.db.flush()
                    touched.append((ent, risk))
            count += 1

        for ent, risk in touched:
            refresh_entity_credit(
                self.db,
                ent,
                trigger_risk=risk,
                reason="人工录入的公开信息信用风险信号更新",
                force=True,
            )
        self.db.commit()
        if touched:
            from app.services.alert_notifications import dispatch_entity_risk_alerts_async

            dispatch_entity_risk_alerts_async([risk.id for _, risk in touched])
        return count


def _batch_item_total(batches: list[dict[str, Any]]) -> int:
    total = 0
    for b in batches:
        if b.get("source") == "authority":
            total += 1
        else:
            total += len(b.get("items") or [])
    return total
