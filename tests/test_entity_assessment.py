from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi import Request
from sqlalchemy import create_engine, inspect

from app.api.routes import export_entity_assessment_docx
from app.database.models import EntityRisk, ReportRun, TargetEntity
from app.database.session import _migrate_sqlite_columns
from app.main import _entity_assessment_context
from app.services.deepseek_analyzer import build_system_prompt
from app.services.entity_catalog import configured_entity_catalog
from app.services.entity_credit import (
    refresh_entity_credit,
    seed_default_entities,
    suggested_credit_for_entity,
)
from app.services.entity_mock import seed_entity_demo_data
from app.services.entity_relevance import is_monitored_public_event, prepare_entity_row
from app.services.pipeline import RiskPipeline
from tests.helpers import isolated_session


class EntityCatalogTests(unittest.TestCase):
    def test_catalog_expands_targets_and_sources(self) -> None:
        catalog = configured_entity_catalog()
        self.assertGreaterEqual(len(catalog.profiles), 10)
        self.assertIn("Godiva", {profile.key for profile in catalog.profiles})
        godiva = next(profile for profile in catalog.profiles if profile.key == "Godiva")
        party_by_role = {party.name: party.role for party in godiva.resolved_related_parties()}
        self.assertEqual(party_by_role.get("Yıldız Holding"), "parent")
        self.assertEqual(party_by_role.get("pladis"), "parent")
        self.assertEqual(party_by_role.get("Ülker Bisküvi"), "shareholder")
        self.assertEqual(party_by_role.get("MBK Partners"), "counterparty")
        self.assertEqual(party_by_role.get("Önem Gıda"), "supplier")
        self.assertEqual(party_by_role.get("Barry Callebaut"), "supplier")
        self.assertEqual(party_by_role.get("Fildişi Cocoa Industry"), "supplier")
        self.assertEqual(party_by_role.get("誉焙食品贸易（上海）有限公司"), "counterparty")
        party_names = {name for party in godiva.resolved_related_parties() for name in party.all_names}
        self.assertTrue(any("yildiz" in name.casefold() or "yıldız" in name.casefold() for name in party_names))
        self.assertIn("星辰控股", party_names)
        self.assertTrue(any("lesnard" in name.casefold() for name in godiva.executives))
        self.assertIn("歌帝梵（上海）食品商贸有限公司", godiva.aliases)
        self.assertNotIn("歌帝梵（上海）食品商贸有限公司", party_names)
        forbidden = ("ICCO", "COCOBOD", "Lindt", "Ferrero", "Amazon", "Walmart", "沃尔玛")
        for name in forbidden:
            self.assertFalse(
                any(name.casefold() in item.casefold() for item in party_names),
                name,
            )
        self.assertIn("大和証券", {profile.key for profile in catalog.profiles})
        for profile in catalog.profiles:
            minimum = 2 if profile.key in {"Mercuria", "Vitol", "Trafigura"} else 4
            self.assertGreaterEqual(len(profile.sources), minimum, profile.key)
            self.assertTrue(any(source.source_type == "official" for source in profile.sources))

    def test_seed_is_idempotent_and_uses_catalog(self) -> None:
        with isolated_session() as db:
            first = seed_default_entities(db)
            second = seed_default_entities(db)
            active = db.query(TargetEntity).filter(TargetEntity.monitor_status == "active").all()
            self.assertGreaterEqual(first, 10)
            self.assertEqual(second, 0)
            self.assertGreaterEqual(len(active), 10)

    def test_seed_renames_legacy_keys_without_unique_conflict(self) -> None:
        with isolated_session() as db:
            legacy = TargetEntity(
                name="ＳＭＢＣ日興証券",
                display_name="旧日兴",
                monitor_status="active",
                credit_level="关注",
            )
            db.add(legacy)
            db.commit()
            created = seed_default_entities(db)
            current = db.query(TargetEntity).filter(TargetEntity.name == "SMBC日興").one()
            leftover = db.query(TargetEntity).filter(TargetEntity.name == "ＳＭＢＣ日興証券").all()
            self.assertEqual(created, len(configured_entity_catalog().profiles) - 1)
            self.assertEqual(current.id, legacy.id)
            self.assertEqual(current.credit_level, "关注")
            self.assertEqual(current.monitor_status, "active")
            self.assertEqual(current.display_name, "日兴证券 SMBC Nikko")
            self.assertEqual(leftover, [])

    def test_seed_merges_legacy_key_when_current_already_exists(self) -> None:
        with isolated_session() as db:
            current = TargetEntity(
                name="SMBC日興",
                display_name="SMBC日兴 SMBC Nikko",
                monitor_status="active",
                credit_level="正常",
            )
            old = TargetEntity(
                name="ＳＭＢＣ日興証券",
                display_name="旧日兴",
                monitor_status="active",
                credit_level="预警",
            )
            db.add_all([current, old])
            db.flush()
            db.add(
                EntityRisk(
                    entity_id=old.id,
                    report_date=date.today(),
                    title="历史事件",
                    summary="归并到当前主体",
                    risk_level="medium",
                    source_name="test",
                )
            )
            db.commit()
            seed_default_entities(db)
            merged = db.query(TargetEntity).filter(TargetEntity.name == "SMBC日興").one()
            retired = db.query(TargetEntity).filter(TargetEntity.name == "ＳＭＢＣ日興証券").one()
            risks = db.query(EntityRisk).filter(EntityRisk.entity_id == merged.id).all()
            self.assertEqual(merged.id, current.id)
            self.assertEqual(retired.monitor_status, "inactive")
            self.assertEqual(len(risks), 1)
            self.assertEqual(risks[0].title, "历史事件")

    def test_api_rejects_writing_entity_sources(self) -> None:
        from fastapi import HTTPException

        from app.api.routes import add_module_url_source
        from app.schemas import DataSourceUrlIn

        with isolated_session() as db:
            seed_default_entities(db)
            entity = db.query(TargetEntity).first()
            self.assertIsNotNone(entity)
            with self.assertRaises(HTTPException) as ctx:
                add_module_url_source(
                    DataSourceUrlIn(
                        name="不应写入",
                        url="https://example.com/entity-source",
                        entity_id=entity.id,
                    ),
                    db,
                )
            self.assertEqual(ctx.exception.status_code, 400)
            self.assertIn("后端配置", str(ctx.exception.detail))

    def test_briefing_and_financial_sources_are_optional_and_parsed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "targets.yaml"
            path.write_text(
                "\n".join(
                    [
                        "entities:",
                        "  - key: SampleCo",
                        "    display_name: 示例公司",
                        "    sources:",
                        "      - label: 官网",
                        "        url: https://example.com/",
                        "        source_type: official",
                        "    briefing_sources:",
                        "      - label: 新闻中心",
                        "        url: https://example.com/news",
                        "        source_type: official",
                        "    financial_sources:",
                        "      - statement: 损益",
                        "        label: 最新损益表",
                        "        url: https://example.com/income",
                        "      - statement: 负债",
                        "        label: 最新负债表",
                        "        url: https://example.com/balance",
                        "      - statement: 流量",
                        "        label: 最新流量表",
                        "        url: https://example.com/cashflow",
                    ]
                ),
                encoding="utf-8",
            )
            from app.services.entity_catalog import load_entity_catalog

            load_entity_catalog.cache_clear()
            catalog = load_entity_catalog(str(path))
            profile = catalog.find(("SampleCo",))
            self.assertIsNotNone(profile)
            assert profile is not None
            self.assertEqual(len(profile.briefing_sources), 1)
            self.assertEqual(profile.briefing_sources[0].url, "https://example.com/news")
            self.assertEqual(
                [item.statement for item in profile.financial_sources],
                ["income", "balance", "cashflow"],
            )
            self.assertEqual(profile.category, "其他")

    def test_catalog_assigns_sidebar_categories(self) -> None:
        from types import SimpleNamespace

        from app.services.entity_catalog import group_monitored_entities

        catalog = configured_entity_catalog()
        self.assertEqual(
            [item.key for item in catalog.categories],
            ["重点关注", "五大商社", "三大银行", "三大券商", "其他"],
        )
        for name, category in (
            ("Godiva", "重点关注"),
            ("普洛斯", "重点关注"),
            ("三菱商事", "五大商社"),
            ("三菱UFJ", "三大银行"),
            ("三井住友FG", "三大银行"),
            ("みずほ", "三大银行"),
            ("野村", "三大券商"),
            ("大和証券", "三大券商"),
            ("SMBC日興", "三大券商"),
            ("デンソー", "其他"),
        ):
            profile = catalog.find((name,))
            self.assertIsNotNone(profile, name)
            assert profile is not None
            self.assertEqual(profile.category, category, name)

        entities = [
            SimpleNamespace(name=profile.key, display_name=profile.display_name, aliases=",".join(profile.aliases))
            for profile in catalog.profiles
        ]
        entities.append(SimpleNamespace(name="UnknownCo", display_name="未知公司", aliases=""))
        grouped = group_monitored_entities(entities, catalog)
        groups = {item["key"]: [ent.name for ent in item["entities"]] for item in grouped}
        self.assertEqual(groups["重点关注"], ["Godiva", "普洛斯"])
        godiva = next(ent for ent in grouped[0]["entities"] if ent.name == "Godiva")
        self.assertTrue(getattr(godiva, "name_zh", ""))
        self.assertTrue(getattr(godiva, "name_en", ""))
        self.assertEqual(
            groups["五大商社"],
            ["三菱商事", "三井物産", "伊藤忠商事", "住友商事", "丸紅"],
        )
        self.assertEqual(groups["三大银行"], ["三菱UFJ", "三井住友FG", "みずほ"])
        self.assertEqual(groups["三大券商"], ["野村", "大和証券", "SMBC日興"])
        self.assertIn("デンソー", groups["其他"])
        self.assertIn("UnknownCo", groups["其他"])

    def test_display_names_split_into_chinese_and_english(self) -> None:
        from app.services.entity_catalog import split_bilingual_display_name

        self.assertEqual(
            split_bilingual_display_name("三井物产 Mitsui & Co."),
            {"zh": "三井物产", "en": "Mitsui & Co."},
        )
        self.assertEqual(
            split_bilingual_display_name("日兴证券 SMBC Nikko"),
            {"zh": "日兴证券", "en": "SMBC Nikko"},
        )
        self.assertEqual(
            split_bilingual_display_name("加纳可可局 (COCOBOD)"),
            {"zh": "加纳可可局", "en": "COCOBOD"},
        )
        from app.services.entity_catalog import apply_bilingual_display_name, canonical_bilingual_display_name
        from types import SimpleNamespace

        self.assertEqual(
            canonical_bilingual_display_name("BF International", "BF International"),
            "BF国际 BF International",
        )
        self.assertEqual(
            canonical_bilingual_display_name(
                "International Cocoa Initiative (ICI)",
                "International Cocoa Initiative (ICI)",
            ),
            "国际可可倡议 ICI",
        )
        self.assertEqual(
            canonical_bilingual_display_name("欧盟委员会", "欧盟委员会"),
            "欧盟委员会 European Commission",
        )
        extra = apply_bilingual_display_name(
            SimpleNamespace(name="欧盟委员会", display_name="欧盟委员会")
        )
        self.assertEqual(extra.name_zh, "欧盟委员会")
        self.assertEqual(extra.name_en, "European Commission")
        with isolated_session() as db:
            db.add(
                TargetEntity(
                    name="BF International",
                    display_name="BF International",
                    monitor_status="active",
                    credit_level="正常",
                )
            )
            db.add(
                TargetEntity(
                    name="欧盟委员会",
                    display_name="欧盟委员会",
                    monitor_status="active",
                    credit_level="正常",
                )
            )
            db.commit()
            seed_default_entities(db)
            bf = db.query(TargetEntity).filter(TargetEntity.name == "BF International").one()
            eu = db.query(TargetEntity).filter(TargetEntity.name == "欧盟委员会").one()
            self.assertEqual(bf.display_name, "BF国际 BF International")
            self.assertEqual(eu.display_name, "欧盟委员会 European Commission")
        catalog = configured_entity_catalog()
        for profile in catalog.profiles:
            parts = split_bilingual_display_name(profile.display_name)
            self.assertTrue(parts["zh"], profile.key)
            self.assertTrue(parts["en"], profile.display_name)

    def test_glp_and_trafigura_financial_pages_are_linked(self) -> None:
        catalog = configured_entity_catalog()
        glp = catalog.find(["普洛斯"])
        self.assertIsNotNone(glp)
        assert glp is not None
        self.assertEqual(glp.financial_source_page, "https://www.glpjreit.com/ja/ir/library.html")
        self.assertEqual(glp.stock_code, "3281")
        self.assertTrue(
            any(
                "ir_library_term-0d9b43551bcd04bd112efdc0b4d169a0e569542f.pdf" in src.url
                for src in glp.financial_sources
            )
        )
        self.assertTrue(any("en/ir/library.html" in src.url for src in glp.sources))
        self.assertTrue(any("en-ir_library_term" in src.url for src in glp.sources))

        trafigura = catalog.find(["Trafigura"])
        self.assertIsNotNone(trafigura)
        assert trafigura is not None
        self.assertIn("2025-trafigura-annual-report", trafigura.financial_source_page or "")
        self.assertTrue(
            any("2025-trafigura-annual-report" in src.url for src in trafigura.financial_sources)
        )

    def test_market_quotes_omit_copper_until_url_is_distinct(self) -> None:
        from app.services.rss_config import load_rss_config

        quotes = load_rss_config().market_quotes
        labels = [item.label for item in quotes]
        self.assertIn("纳斯达克", labels)
        self.assertIn("日经", labels)
        self.assertIn("原油", labels)
        self.assertIn("美元兑日元", labels)
        self.assertNotIn("铜", labels)
        yen = next(item for item in quotes if item.label == "美元兑日元")
        self.assertIn("JPY", yen.url)


class EntityBriefingTests(unittest.TestCase):
    def setUp(self) -> None:
        from app.services.entity_financial_pdf import PdfFinance

        self._pdf_patch = patch(
            "app.services.entity_briefing.load_pdf_statements",
            return_value=PdfFinance(),
        )
        self._pdf_patch.start()

    def tearDown(self) -> None:
        self._pdf_patch.stop()

    def test_latest_news_keeps_core_risk_in_lookback(self) -> None:
        from app.services.entity_briefing import build_latest_news

        target_day = date(2026, 8, 13)
        low = EntityRisk(
            id=1,
            entity_id=1,
            report_date=target_day,
            title="日常经营动态",
            risk_level="低",
            summary="一般动态",
            provenance="real",
            relevance="direct",
            credit_impact="none",
        )
        high = EntityRisk(
            id=2,
            entity_id=1,
            report_date=date(2026, 6, 20),
            title="监管处罚公告",
            risk_level="高",
            summary="核心风险",
            provenance="real",
            relevance="direct",
            credit_impact="medium",
        )
        too_old = EntityRisk(
            id=4,
            entity_id=1,
            report_date=date(2026, 4, 1),
            title="过期处罚",
            risk_level="高",
            summary="超出观察期",
            provenance="real",
            relevance="direct",
            credit_impact="high",
        )
        contextual = EntityRisk(
            id=3,
            entity_id=1,
            report_date=target_day,
            title="行业背景波动",
            risk_level="极高",
            summary="背景",
            provenance="real",
            relevance="contextual",
            credit_impact="high",
        )
        payload = build_latest_news(
            risks=[low, high, contextual, too_old],
            report_date=target_day,
        )
        self.assertEqual(payload["status"], "ready")
        self.assertEqual(payload["lookback_days"], 90)
        self.assertEqual(payload["lookback_start"], "2026-05-15")
        self.assertEqual(
            [item["title"] for item in payload["highlights"]],
            ["监管处罚公告"],
        )
        self.assertTrue(payload["uses_llm_search"])
        self.assertTrue(payload["direct_pending"])
        self.assertEqual(payload["summary_source"], "template")
        self.assertIn("近三个月", payload["summary"])

    def test_latest_news_writes_overview_when_no_core_events(self) -> None:
        from app.services.entity_briefing import build_latest_news
        from app.services.entity_briefing_feed import BriefingHeadline
        from app.services.entity_catalog import EntityProfile, EntitySourceSpec

        target_day = date(2026, 8, 13)
        low = EntityRisk(
            id=1,
            entity_id=1,
            report_date=target_day,
            title="日常经营动态",
            risk_level="低",
            summary="一般动态",
            provenance="real",
            relevance="direct",
            credit_impact="none",
        )
        headline = BriefingHeadline(
            title="Godiva opens seasonal shop",
            url="https://example.com/g",
            feed_label="Godiva Media",
        )
        profile = EntityProfile(
            key="Godiva",
            display_name="歌帝梵 Godiva",
            briefing_sources=(
                EntitySourceSpec(
                    label="Godiva Media",
                    url="https://example.com/rss.xml",
                    source_type="official",
                ),
            ),
        )
        with patch("app.services.entity_briefing.fetch_briefing_headlines", return_value=[headline]):
            with patch("app.services.entity_briefing.is_placeholder_key", return_value=True):
                payload = build_latest_news(
                    risks=[low],
                    report_date=target_day,
                    profile=profile,
                )
        self.assertEqual(payload["status"], "ready")
        self.assertEqual(payload["mode"], "overview")
        self.assertEqual(payload["highlights"], [])
        self.assertIn("未见重大", payload["summary"])
        self.assertEqual(payload["headlines"][0]["title"], "Godiva opens seasonal shop")

    def test_latest_news_uses_search_fallback_when_feeds_empty(self) -> None:
        from app.services.entity_briefing import build_latest_news
        from app.services.entity_briefing_feed import BriefingHeadline
        from app.services.entity_catalog import EntityProfile, EntitySourceSpec

        target_day = date(2026, 8, 13)
        profile = EntityProfile(
            key="Godiva",
            display_name="歌帝梵 Godiva",
            aliases=("Godiva",),
            briefing_sources=(
                EntitySourceSpec(
                    label="Godiva Media",
                    url="https://example.com/rss.xml",
                    source_type="official",
                    query="Godiva",
                ),
            ),
        )
        fallback = BriefingHeadline(
            title="Godiva supply update",
            url="https://example.com/s",
            feed_label="检索补缺·mita",
        )
        with patch("app.services.entity_briefing.fetch_briefing_headlines", return_value=[]):
            with patch(
                "app.services.entity_briefing._search_fallback_headlines",
                return_value=[fallback],
            ):
                with patch("app.services.entity_briefing.is_placeholder_key", return_value=True):
                    payload = build_latest_news(
                        risks=[],
                        report_date=target_day,
                        profile=profile,
                        live=True,
                    )
        self.assertEqual(payload["status"], "ready")
        self.assertTrue(payload["search_fallback_used"])
        self.assertEqual(payload["feed_item_count"], 1)
        self.assertIn("未见重大", payload["summary"])

    def test_latest_news_uses_db_risks_when_external_empty(self) -> None:
        from app.services.entity_briefing import build_latest_news
        from app.services.entity_catalog import EntityProfile

        target_day = date(2026, 8, 13)
        older = EntityRisk(
            id=9,
            entity_id=1,
            report_date=date(2026, 8, 10),
            title="Godiva 召回部分产品",
            risk_level="中",
            summary="库内事件",
            provenance="real",
            relevance="direct",
            source_url="https://example.com/old",
        )
        profile = EntityProfile(key="Godiva", display_name="歌帝梵 Godiva")
        with patch("app.services.entity_briefing.fetch_briefing_headlines", return_value=[]):
            with patch("app.services.entity_briefing._search_fallback_headlines", return_value=[]):
                with patch("app.services.entity_briefing.is_placeholder_key", return_value=True):
                    payload = build_latest_news(
                        risks=[],
                        report_date=target_day,
                        profile=profile,
                        live=False,
                        recent_risks=[older],
                    )
        self.assertEqual(payload["status"], "ready")
        self.assertTrue(payload["db_fallback_used"])
        self.assertEqual(payload["headlines"][0]["title"], "Godiva 召回部分产品")
        self.assertIn("近三个月", payload["summary"])
        self.assertIn("库内事件", payload["headlines"][0]["feed_label"])

    def test_briefing_channels_use_native_rss_or_existing_queries(self) -> None:
        from app.services.entity_briefing_feed import resolve_briefing_channels

        catalog = configured_entity_catalog()
        mizuho = catalog.find(["みずほ"])
        self.assertIsNotNone(mizuho)
        mizuho_channels = resolve_briefing_channels(mizuho)
        self.assertTrue(mizuho_channels)
        self.assertEqual(mizuho_channels[0].kind, "native_rss")
        self.assertIn("rss2.www.mizuho-fg.co.jp", mizuho_channels[0].native_feed_url or "")

        godiva = catalog.find(["Godiva"])
        self.assertIsNotNone(godiva)
        godiva_channels = resolve_briefing_channels(godiva)
        self.assertGreaterEqual(len(godiva_channels), 1)
        self.assertTrue(all(ch.kind == "google_news" for ch in godiva_channels))
        self.assertTrue(all(ch.query for ch in godiva_channels))
        self.assertFalse(any("有価証券" in ch.label or "有价证券" in ch.label for ch in godiva_channels))

        mitsubishi = catalog.find(["三菱商事"])
        self.assertIsNotNone(mitsubishi)
        mc_channels = resolve_briefing_channels(mitsubishi)
        self.assertGreaterEqual(len(mc_channels), 1)
        self.assertTrue(all(ch.kind == "google_news" for ch in mc_channels))
        self.assertFalse(any("有価証券" in (ch.label + ch.page_url + (ch.query or "")) for ch in mc_channels))

    def test_every_entity_uses_the_same_briefing_and_finance_path(self) -> None:
        from app.services.entity_briefing import build_financials_panel
        from app.services.entity_briefing_feed import resolve_briefing_channels
        from app.services.pipeline import _profile_needs_html_industry_sources

        catalog = configured_entity_catalog()
        html_keys: list[str] = []
        for profile in catalog.profiles:
            channels = resolve_briefing_channels(profile)
            self.assertTrue(channels, profile.key)
            payload = build_financials_panel(profile, live=False)
            self.assertEqual(len(payload["statements"]), 3, profile.key)
            self.assertEqual(
                [stmt["title"] for stmt in payload["statements"]],
                ["损益表要点 (通期)", "资产负债表要点 (通期)", "现金流量表要点 (通期)"],
            )
            if profile.stock_code:
                self.assertEqual(
                    payload["source_page_url"],
                    f"https://kabutan.jp/stock/finance?code={profile.stock_code}",
                    profile.key,
                )
            if _profile_needs_html_industry_sources(profile):
                html_keys.append(profile.key)
        self.assertEqual(html_keys, ["Godiva"])

    def test_latest_news_uses_gemini_summary_when_profile_has_core_events(self) -> None:
        from app.services.entity_briefing import build_latest_news
        from app.services.entity_briefing_feed import BriefingHeadline
        from app.services.entity_catalog import EntityProfile, EntitySourceSpec

        target_day = date(2026, 8, 13)
        high = EntityRisk(
            id=2,
            entity_id=1,
            report_date=target_day,
            title="监管处罚公告",
            risk_level="高",
            summary="核心风险",
            provenance="real",
            relevance="direct",
            credit_impact="medium",
        )
        profile = EntityProfile(
            key="みずほ",
            display_name="瑞穗 Mizuho",
            briefing_sources=(
                EntitySourceSpec(
                    label="瑞穗新闻 RSS",
                    url="https://rss2.www.mizuho-fg.co.jp/rss?site=AQ83RXG5&item=1",
                    source_type="official",
                ),
            ),
        )
        headline = BriefingHeadline(title="みずほ 業務改善命令", url="https://example.com/n", feed_label="RSS")
        with patch("app.services.entity_briefing.fetch_briefing_headlines", return_value=[headline]):
            with patch(
                "app.services.entity_briefing.summarize_latest_news",
                return_value=("瑞穗当日因监管处罚进入核心风险关注。", "2026-08-13T12:00:00+09:00", "gemini"),
            ) as summarizer:
                payload = build_latest_news(
                    risks=[high],
                    report_date=target_day,
                    profile=profile,
                )
        self.assertFalse(payload["direct_pending"])
        self.assertEqual(payload["direct_source_count"], 1)
        self.assertEqual(payload["summary"], "瑞穗当日因监管处罚进入核心风险关注。")
        self.assertEqual(payload["summary_source"], "gemini")
        self.assertEqual(payload["headlines"][0]["title"], "みずほ 業務改善命令")
        self.assertEqual(payload["headlines"][0]["url"], "https://example.com/n")
        summarizer.assert_called_once()

    def test_summarize_falls_back_without_gemini_key(self) -> None:
        from app.services.entity_briefing import summarize_latest_news

        with patch("app.services.entity_briefing.is_placeholder_key", return_value=True):
            text, generated_at, source = summarize_latest_news(
                entity_name="瑞穗",
                report_date=date(2026, 8, 13),
                highlights=[{"title": "监管处罚公告"}],
                headlines=[],
                fallback="模板摘要",
            )
        self.assertEqual(text, "模板摘要")
        self.assertIsNone(generated_at)
        self.assertEqual(source, "template")

    def test_summarize_uses_gemini_plain_text(self) -> None:
        from app.services.entity_briefing import summarize_latest_news

        fake = Mock()
        fake.generate_text.return_value = "  瑞穗当日核心风险为监管处罚。  "
        with patch("app.services.entity_briefing.is_placeholder_key", return_value=False):
            with patch("app.services.entity_briefing.gemini_for", return_value=fake):
                text, generated_at, source = summarize_latest_news(
                    entity_name="瑞穗",
                    report_date=date(2026, 8, 13),
                    highlights=[{"title": "监管处罚公告"}],
                    headlines=[],
                    fallback="模板摘要",
                )
        self.assertEqual(text, "瑞穗当日核心风险为监管处罚。")
        self.assertEqual(source, "gemini")
        self.assertIsNotNone(generated_at)
        fake.generate_text.assert_called_once()

    def test_financial_panel_uses_kabutan_page_for_listed_entities(self) -> None:
        from app.services.entity_briefing import build_financials_panel
        from app.services.entity_catalog import EntityProfile
        from app.services.entity_kabutan import _cache

        _cache.clear()
        profile = EntityProfile(key="三菱商事", display_name="三菱商事", stock_code="8058")
        with patch("app.services.entity_kabutan._fetch_html", return_value=""):
            payload = build_financials_panel(profile)
        self.assertEqual(payload["status"], "linked")
        self.assertEqual(payload["source_page_url"], "https://kabutan.jp/stock/finance?code=8058")
        self.assertEqual(payload["source_label"], "株探同期表 (整理自公开数据)")
        self.assertEqual(payload["statements"][2]["columns"][2]["label"], "自由现金流")

    def test_financial_panel_keeps_ir_pdf_but_links_kabutan_for_listed_entities(self) -> None:
        from app.services.entity_briefing import build_financials_panel
        from app.services.entity_catalog import EntityProfile
        from app.services.entity_kabutan import _cache

        _cache.clear()
        profile = EntityProfile(
            key="SampleCo",
            display_name="示例",
            stock_code="8058",
            financial_source_page="https://example.com/finance",
            financial_source_label="有价证券报告书（整理自公开披露）",
            financial_unit="单位: 百万日元 (有价证券报告书)",
        )
        html = """
        <a href="/old/yuho2024.pdf">第101期 有価証券報告書</a>
        <a href="/q/half.pdf">半期報告書</a>
        <a href="/lib/yuho2026.pdf">第103期 有価証券報告書</a>
        """
        with patch("app.services.entity_financial_pdf._fetch_html", return_value=html):
            with patch("app.services.entity_kabutan._fetch_html", return_value=""):
                payload = build_financials_panel(profile)
        self.assertEqual(payload["source_page_url"], "https://kabutan.jp/stock/finance?code=8058")
        self.assertEqual(payload["source_label"], "株探同期表 (整理自公开数据)")
        self.assertEqual(payload["unit"], "单位: 百万日元 (株探口径)")
        self.assertEqual(payload["latest_pdf_source"], "discovered")
        self.assertTrue(payload["latest_pdf_url"].endswith("/lib/yuho2026.pdf"))

    def test_financial_panel_uses_explicit_page_when_unlisted(self) -> None:
        from app.services.entity_briefing import build_financials_panel
        from app.services.entity_catalog import EntityProfile

        profile = EntityProfile(
            key="Trafigura",
            display_name="托克",
            financial_source_page="https://www.trafigura.com/news-and-insights/publications/financials/2025/2025-trafigura-annual-report/",
            financial_source_label="年度报告（整理自公开披露）",
        )
        payload = build_financials_panel(profile, live=False)
        self.assertEqual(
            payload["source_page_url"],
            "https://www.trafigura.com/news-and-insights/publications/financials/2025/2025-trafigura-annual-report/",
        )
        self.assertEqual(payload["source_label"], "年度报告（整理自公开披露）")
        self.assertFalse(payload["uses_kabutan_page"])

    def test_unlisted_brand_uses_group_context_without_faking_statements(self) -> None:
        from app.services.entity_briefing import build_financials_panel

        profile = configured_entity_catalog().find(["Godiva"])
        self.assertIsNotNone(profile)
        payload = build_financials_panel(profile, live=False)
        self.assertTrue(payload["is_alternative_context"])
        self.assertFalse(payload["uses_kabutan_page"])
        self.assertIn("非 Godiva 独立财报", payload["source_label"])
        self.assertEqual(payload["context_metrics"][0]["value"], "TRY 657.8 billion")
        self.assertIn("yildizholding.com.tr", payload["context_metrics"][0]["url"])

    def test_financial_panel_can_prefer_official_pdf_for_listed_entity(self) -> None:
        from app.services.entity_briefing import build_financials_panel
        from app.services.entity_catalog import EntityProfile

        profile = EntityProfile(
            key="GLP",
            display_name="普洛斯 GLP",
            stock_code="3281",
            prefer_financial_pdf=True,
            financial_source_page="https://www.glpjreit.com/ja/ir/library.html",
            financial_source_label="GLP J-REIT 決算资料（整理自公开披露）",
        )
        html = '<a href="/file/2026-results.pdf">2026年2月期 決算短信</a>'
        with patch("app.services.entity_financial_pdf._fetch_html", return_value=html):
            with patch("app.services.entity_briefing.load_pdf_statements") as mocked:
                from app.services.entity_financial_pdf import PdfFinance
                mocked.return_value = PdfFinance()
                payload = build_financials_panel(profile)
        self.assertFalse(payload["uses_kabutan_page"])
        self.assertEqual(payload["source_label"], "GLP J-REIT 決算资料（整理自公开披露）")
        self.assertTrue(payload["latest_pdf_url"].endswith("/file/2026-results.pdf"))

    def test_catalog_listed_entities_open_kabutan_source_page(self) -> None:
        from app.services.entity_briefing import build_financials_panel
        from app.services.entity_kabutan import _cache

        catalog = configured_entity_catalog()
        profile = catalog.find(["三菱商事"])
        self.assertIsNotNone(profile)
        assert profile is not None
        _cache.clear()
        with patch("app.services.entity_financial_pdf._fetch_html", return_value=""):
            with patch("app.services.entity_kabutan._fetch_html", return_value=""):
                payload = build_financials_panel(profile)
        self.assertEqual(payload["source_page_url"], "https://kabutan.jp/stock/finance?code=8058")
        self.assertEqual(payload["source_label"], "株探同期表 (整理自公开数据)")

    def test_kabutan_parser_keeps_full_year_rows_and_skips_half_year(self) -> None:
        from app.services.entity_kabutan import parse_kabutan_finance

        html = (Path(__file__).parent / "fixtures" / "kabutan_finance_sample.html").read_text(
            encoding="utf-8"
        )
        parsed = parse_kabutan_finance(html)
        self.assertEqual(
            [row["period"] for row in parsed["income"]],
            ["2025.03", "2026.03", "2027.03"],
        )
        self.assertEqual(parsed["income"][1]["revenue"], "18,915,995")
        self.assertIsNone(parsed["income"][1]["operating_profit"])
        self.assertEqual(parsed["income"][1]["released_at"], "26/05/01")
        self.assertEqual(parsed["cashflow"][1]["free_cash_flow"], "1,041,457")
        self.assertEqual(parsed["balance"][1]["total_assets"], "24,151,695")
        self.assertEqual([row["period"] for row in parsed["balance"]], ["2025.03", "2026.03"])

    def test_pdf_rows_keep_only_numbers_found_in_source_text(self) -> None:
        from app.services.entity_financial_pdf import verify_statement_rows

        source = "通期 売上高 18,915,995 最終益 800,460"
        parsed = {
            "income": [
                {
                    "period": "2026.03",
                    "revenue": "18,915,995",
                    "net_profit": "800,460",
                    "operating_profit": "999999",
                }
            ],
            "balance": [],
            "cashflow": [],
        }
        verified = verify_statement_rows(parsed, source)
        self.assertEqual(verified["income"][0]["revenue"], "18,915,995")
        self.assertEqual(verified["income"][0]["net_profit"], "800,460")
        self.assertNotIn("operating_profit", verified["income"][0])

    def test_risk_tab_maps_industry_background_to_public(self) -> None:
        from app.services.entity_relevance import canonical_risk_category, classify_risk_tab

        self.assertEqual(classify_risk_tab("行业背景"), "public")
        self.assertEqual(canonical_risk_category("行业动态"), "公开舆情")
        self.assertEqual(classify_risk_tab("司法处罚"), "judicial")
        self.assertEqual(classify_risk_tab("供应链稳定性"), "supply")

    def test_financial_panel_fills_rows_from_kabutan(self) -> None:
        from app.services.entity_briefing import build_financials_panel
        from app.services.entity_catalog import EntityProfile
        from app.services.entity_kabutan import _cache

        _cache.clear()
        html = (Path(__file__).parent / "fixtures" / "kabutan_finance_sample.html").read_text(
            encoding="utf-8"
        )
        profile = EntityProfile(key="三菱商事", display_name="三菱商事", stock_code="8058")
        with patch("app.services.entity_kabutan._fetch_html", return_value=html):
            payload = build_financials_panel(profile)
        self.assertTrue(payload["kabutan_ok"])
        self.assertEqual(
            [row["period"] for row in payload["statements"][0]["rows"]],
            ["2027.03", "2026.03", "2025.03"],
        )
        self.assertEqual(payload["statements"][0]["rows"][1]["net_profit"], "800,460")
        self.assertEqual(
            [row["period"] for row in payload["statements"][2]["rows"]],
            ["2026.03", "2025.03"],
        )
        self.assertEqual(payload["statements"][2]["rows"][0]["operating_cash_flow"], "1,490,041")

    def test_unlisted_entity_puts_latest_pdf_in_source_link(self) -> None:
        from app.services.entity_briefing import build_financials_panel
        from app.services.entity_catalog import EntityProfile, FinancialSourceSpec

        profile = EntityProfile(
            key="Trafigura",
            display_name="托克",
            financial_source_page="https://example.com/ir/",
            financial_source_label="年度报告（整理自公开披露）",
            financial_sources=(
                FinancialSourceSpec(
                    statement="income",
                    label="2025 年度报告",
                    url="https://example.com/ir/2025-annual.pdf",
                ),
            ),
        )
        html = """
        <p><a href="/old/2024-annual.pdf">2024 Annual Report</a></p>
        <p><a href="/ir/2026-annual.pdf">2026 Annual Report</a></p>
        """
        with patch("app.services.entity_financial_pdf._fetch_html", return_value=html):
            with patch("app.services.entity_briefing.load_pdf_statements") as mocked:
                from app.services.entity_financial_pdf import PdfFinance

                mocked.return_value = PdfFinance(
                    statements={
                        "income": [{"period": "2025.09", "revenue": "1", "net_profit": "2"}],
                        "balance": [],
                        "cashflow": [],
                    },
                    ok=True,
                )
                payload = build_financials_panel(profile)
        self.assertFalse(payload["uses_kabutan_page"])
        self.assertTrue(payload["source_page_url"].endswith("/ir/2026-annual.pdf"))
        self.assertEqual(payload["source_page_url"], payload["latest_pdf_url"])
        self.assertEqual(payload["statements"][0]["rows"][0]["period"], "2025.09")

    def test_latest_pdf_skips_quarterly_and_uses_hint(self) -> None:
        from app.services.entity_financial_pdf import extract_pdf_candidates, pick_latest_pdf

        html = """
        <a href="https://example.com/fg_fy.pdf">みずほフィナンシャルグループ 有価証券報告書</a>
        <a href="https://example.com/bk_fy.pdf">みずほ銀行 有価証券報告書</a>
        <a href="https://example.com/half.pdf">半期報告書</a>
        """
        candidates = extract_pdf_candidates(html, "https://example.com/list/")
        picked = pick_latest_pdf(candidates, hint="fg_fy")
        self.assertIsNotNone(picked)
        assert picked is not None
        self.assertIn("fg_fy.pdf", picked.url)


class EntitySignalTests(unittest.TestCase):
    def _entity(self, db, name: str = "Godiva") -> TargetEntity:
        seed_default_entities(db)
        return db.query(TargetEntity).filter(TargetEntity.name == name).one()

    def test_public_event_filter_keeps_company_supply_exec_finance_shareholder(self) -> None:
        company = EntityRisk(
            title="Godiva 召回部分产品",
            summary="监管机构公布召回。",
            relevance="direct",
            risk_category="公开舆情",
            provenance="real",
        )
        background = EntityRisk(
            title="全球可可期货上涨",
            relevance="contextual",
            risk_category="公开舆情",
            provenance="real",
        )
        unnamed_supply = EntityRisk(
            title="上游可可供应商延迟交货",
            summary="可可减产导致供应紧张。",
            relevance="contextual",
            risk_category="供应链/关联方监控",
            provenance="real",
        )
        named_supply = EntityRisk(
            title="Barry Callebaut 作为可可供应商交货中断",
            summary="材料写明 Barry Callebaut 是供应商，交货已中断。",
            relevance="contextual",
            risk_category="供应链/关联方监控",
            provenance="real",
        )
        exec_change = EntityRisk(
            title="董事会任命新CEO",
            relevance="direct",
            risk_category="公开舆情",
            provenance="real",
        )
        finance = EntityRisk(
            title="发布年度财报利润下滑",
            relevance="direct",
            risk_category="金融与经营数据",
            provenance="real",
        )
        shareholder = EntityRisk(
            title="大股东增持本公司股份",
            relevance="direct",
            risk_category="公开舆情",
            provenance="real",
        )
        gift = EntityRisk(
            title="情人节巧克力礼盒推荐：Godiva 与 Lindt",
            summary="节日礼盒评测。",
            relevance="direct",
            risk_category="公开舆情",
            provenance="real",
        )
        self.assertTrue(is_monitored_public_event(company))
        self.assertFalse(is_monitored_public_event(background))
        self.assertFalse(is_monitored_public_event(unnamed_supply))
        self.assertTrue(is_monitored_public_event(named_supply))
        self.assertTrue(is_monitored_public_event(exec_change))
        self.assertTrue(is_monitored_public_event(finance))
        self.assertTrue(is_monitored_public_event(shareholder))
        self.assertFalse(is_monitored_public_event(gift))

    def test_godiva_acceptance_keeps_related_drops_noise(self) -> None:
        with isolated_session() as db:
            entity = self._entity(db)
            profile = configured_entity_catalog().find((entity.name,))
            keep = [
                EntityRisk(
                    title="Godiva 在美国召回部分巧克力产品",
                    summary="FDA 公布 Godiva 召回。",
                    relevance="direct",
                    provenance="real",
                    source_name="美国 FDA",
                    source_url="https://www.fda.gov/safety/recalls-godiva",
                ),
                EntityRisk(
                    title="Godiva 面临消费者诉讼",
                    summary="集体诉讼已立案。",
                    relevance="direct",
                    provenance="real",
                ),
                EntityRisk(
                    title="Godiva 发布年度财报",
                    summary="利润与债务情况已披露。",
                    relevance="direct",
                    risk_category="金融与经营数据",
                    provenance="real",
                ),
                EntityRisk(
                    title="Yıldız Holding 宣布债务重组",
                    summary="Godiva 母公司 Yıldız Holding 再融资。",
                    relevance="contextual",
                    provenance="real",
                ),
                EntityRisk(
                    title="Yıldız Holding 控股结构发生变动",
                    summary="母公司持股与控制权变化。",
                    relevance="contextual",
                    provenance="real",
                ),
                EntityRisk(
                    title="Godiva 任命新任 CEO",
                    summary="董事会完成高管任免。",
                    relevance="direct",
                    provenance="real",
                ),
                EntityRisk(
                    title="Yıldız Holding 任命新任 CEO",
                    summary="母公司董事会完成高管更换。",
                    relevance="contextual",
                    provenance="real",
                ),
                EntityRisk(
                    title="Steve Lesnard resigns as CEO",
                    summary="Steve Lesnard 辞去 CEO 职务。",
                    relevance="direct",
                    provenance="real",
                ),
                EntityRisk(
                    title="Barry Callebaut 对 Godiva 交货中断",
                    summary="可可供应商 Barry Callebaut 宣布停供。",
                    relevance="contextual",
                    risk_category="供应链/关联方监控",
                    provenance="real",
                ),
                EntityRisk(
                    title="Önem Gıda 停产影响供应",
                    summary="已点名供应商 Önem Gıda 出现经营中断。",
                    relevance="contextual",
                    risk_category="供应链/关联方监控",
                    provenance="real",
                ),
                EntityRisk(
                    title="MBK Partners 出售日韩澳新许可资产",
                    summary="MBK Partners 与 Godiva 日韩澳新许可相关的重大资产变动。",
                    relevance="contextual",
                    provenance="real",
                ),
                EntityRisk(
                    title="歌帝梵（上海）食品商贸有限公司收到监管处罚",
                    summary="中国实体因合规问题被处罚。",
                    relevance="direct",
                    risk_category="司法/行政监管",
                    provenance="real",
                ),
                EntityRisk(
                    title="欧盟通过可可尽职调查新规",
                    summary="新规明确约束欧盟巧克力进口商与制造商。",
                    relevance="contextual",
                    provenance="real",
                ),
            ]
            drop = [
                EntityRisk(
                    title="ICCO 公布可可价格月报",
                    summary="国际可可组织统计全球价格。",
                    relevance="contextual",
                    provenance="real",
                ),
                EntityRisk(
                    title="COCOBOD 发布产季产量更新",
                    summary="加纳可可局作物展望。",
                    relevance="contextual",
                    provenance="real",
                ),
                EntityRisk(
                    title="巧克力礼盒推荐包含 Godiva",
                    summary="节日礼盒评测榜单。",
                    relevance="direct",
                    provenance="real",
                ),
                EntityRisk(
                    title="Godiva 快闪店评测",
                    summary="旅游攻略介绍快闪店与口味。",
                    relevance="direct",
                    provenance="real",
                ),
                EntityRisk(
                    title="巧克力行业 ESG 综述",
                    summary="未点名 Godiva 的行业报告。",
                    relevance="contextual",
                    provenance="real",
                ),
                EntityRisk(
                    title="上游供应紧张影响巧克力行业",
                    summary="可可减产导致供应紧张，未点名具体公司。",
                    relevance="contextual",
                    risk_category="供应链/关联方监控",
                    provenance="real",
                ),
                EntityRisk(
                    title="Lindt 面临消费者诉讼",
                    summary="仅关于同行 Lindt 的集体诉讼。",
                    relevance="direct",
                    provenance="real",
                ),
                EntityRisk(
                    title="Ferrero 发布年度财报",
                    summary="仅关于 Ferrero 的经营披露。",
                    relevance="direct",
                    risk_category="金融与经营数据",
                    provenance="real",
                ),
                EntityRisk(
                    title="沃尔玛调整巧克力货架",
                    summary="沃尔玛作为零售渠道调整货架，并非官方点名关联方。",
                    relevance="contextual",
                    risk_category="供应链/关联方监控",
                    provenance="real",
                ),
            ]
            for risk in keep:
                self.assertTrue(
                    is_monitored_public_event(risk, entity=entity, profile=profile),
                    risk.title,
                )
            for risk in drop:
                self.assertFalse(
                    is_monitored_public_event(risk, entity=entity, profile=profile),
                    risk.title,
                )

    def test_parent_news_is_contextual_and_does_not_raise_warning(self) -> None:
        with isolated_session() as db:
            entity = self._entity(db)
            profile = configured_entity_catalog().find((entity.name,))
            row = {
                "标题": "Yıldız Holding 宣布大规模再融资",
                "关联企业": "Yıldız Holding",
                "核心摘要": "母公司 Yıldız Holding 将重组到期债务。",
                "影响方向": "negative",
                "信用风险信号": "high",
                "资讯重要度": "极高",
                "_source_item": {
                    "title": "Yildiz Holding refinances debt",
                    "snippet": "The parent company announced a debt restructuring.",
                    "entity_key": "Godiva",
                    "relation": "direct",
                    "source_type": "official",
                    "feed": "Yıldız Holding Newsroom",
                    "url": "https://www.yildizholding.com.tr/en/press-room/debt",
                },
            }
            self.assertTrue(prepare_entity_row(entity, row, profile))
            self.assertEqual(row["主体相关性"], "contextual")
            self.assertEqual(row["信用风险信号"], "none")
            self.assertEqual(row["风险等级"], "低")

    def test_gift_box_mention_is_not_entity_subject(self) -> None:
        with isolated_session() as db:
            entity = self._entity(db)
            profile = configured_entity_catalog().find((entity.name,))
            row = {
                "标题": "情人节巧克力礼盒推荐：Godiva、Lindt 与 Ferrero",
                "核心摘要": "节日礼盒评测，Godiva 出现在榜单中。",
                "影响方向": "positive",
                "信用风险信号": "none",
                "_source_item": {
                    "title": "Best chocolate gift boxes: Godiva, Lindt and Ferrero",
                    "snippet": "A holiday gift guide ranking popular chocolate brands.",
                    "entity_key": "Godiva",
                    "relation": "direct",
                    "source_type": "media",
                },
            }
            self.assertFalse(prepare_entity_row(entity, row, profile))

    def test_shanghai_entity_is_treated_as_godiva_itself(self) -> None:
        with isolated_session() as db:
            entity = self._entity(db)
            profile = configured_entity_catalog().find((entity.name,))
            row = {
                "标题": "歌帝梵（上海）食品商贸有限公司收到监管处罚",
                "核心摘要": "中国实体因合规问题被处罚。",
                "影响方向": "negative",
                "信用风险信号": "medium",
                "_source_item": {
                    "title": "Shanghai Godiva entity fined by regulator",
                    "snippet": "The Chinese operating company received an administrative penalty.",
                    "entity_key": "Godiva",
                    "relation": "direct",
                    "source_type": "regulatory",
                },
            }
            self.assertTrue(prepare_entity_row(entity, row, profile))
            self.assertEqual(row["主体相关性"], "direct")

    def test_mbk_license_news_is_contextual_and_does_not_raise_warning(self) -> None:
        with isolated_session() as db:
            entity = self._entity(db)
            profile = configured_entity_catalog().find((entity.name,))
            row = {
                "标题": "MBK Partners 出售日韩澳新许可资产",
                "关联企业": "MBK Partners",
                "核心摘要": "MBK Partners 调整 Godiva 日韩澳新许可相关资产。",
                "影响方向": "negative",
                "信用风险信号": "high",
                "资讯重要度": "高",
                "_source_item": {
                    "title": "MBK Partners sells ANZ and Korea licenses",
                    "snippet": "A material change to the regional license assets.",
                    "entity_key": "Godiva",
                    "relation": "contextual",
                    "source_type": "media",
                },
            }
            self.assertTrue(prepare_entity_row(entity, row, profile))
            self.assertEqual(row["主体相关性"], "contextual")
            self.assertEqual(row["信用风险信号"], "none")
            self.assertEqual(row["风险等级"], "低")

    def test_peer_and_generic_retail_news_are_rejected(self) -> None:
        with isolated_session() as db:
            entity = self._entity(db)
            profile = configured_entity_catalog().find((entity.name,))
            lindt = {
                "标题": "Lindt 面临消费者诉讼",
                "核心摘要": "仅关于同行 Lindt 的集体诉讼。",
                "影响方向": "negative",
                "信用风险信号": "high",
                "_source_item": {
                    "title": "Lindt faces class action",
                    "snippet": "A lawsuit against a peer chocolate maker.",
                    "entity_key": "Godiva",
                    "relation": "direct",
                    "source_type": "media",
                },
            }
            walmart = {
                "标题": "沃尔玛作为经销商调整货架",
                "核心摘要": "沃尔玛门店调整巧克力陈列，并非官方点名关联方。",
                "影响方向": "negative",
                "信用风险信号": "low",
                "_source_item": {
                    "title": "Walmart resets chocolate shelves",
                    "snippet": "A retail channel change at Walmart.",
                    "entity_key": "Godiva",
                    "relation": "contextual",
                    "source_type": "media",
                },
            }
            self.assertFalse(prepare_entity_row(entity, lindt, profile))
            self.assertFalse(prepare_entity_row(entity, walmart, profile))

    def test_positive_high_importance_news_cannot_raise_warning(self) -> None:
        with isolated_session() as db:
            entity = self._entity(db)
            profile = configured_entity_catalog().find((entity.name,))
            row = {
                "标题": "Godiva 成功偿还到期债务",
                "关联企业": "Godiva",
                "风险等级": "极高",
                "资讯重要度": "极高",
                "影响方向": "positive",
                "信用风险信号": "high",
                "主体相关性": "direct",
                "核心摘要": "公司公告称已按期完成偿还。",
                "来源链接": "https://www.godiva.com/blogs/media/example",
                "来源名称": "Godiva Media Center",
                "_source_item": {
                    "title": "Godiva repays debt",
                    "entity_key": "Godiva",
                    "relation": "direct",
                    "source_type": "official",
                },
            }
            self.assertTrue(prepare_entity_row(entity, row, profile))
            self.assertEqual(row["信用风险信号"], "none")
            self.assertEqual(row["风险等级"], "低")

    def test_only_valid_direct_negative_signal_affects_warning(self) -> None:
        with isolated_session() as db:
            entity = self._entity(db)
            today = date.today()
            rows = [
                EntityRisk(
                    entity_id=entity.id,
                    report_date=today,
                    title="演示负面事件",
                    risk_level="极高",
                    summary="演示",
                    provenance="demo",
                    relevance="direct",
                    sentiment_direction="negative",
                    credit_impact="critical",
                    confidence=0.99,
                    review_status="pending",
                ),
                EntityRisk(
                    entity_id=entity.id,
                    report_date=today,
                    title="可可行业背景",
                    risk_level="极高",
                    summary="行业背景",
                    provenance="real",
                    relevance="contextual",
                    sentiment_direction="negative",
                    credit_impact="critical",
                    confidence=0.99,
                    review_status="pending",
                ),
                EntityRisk(
                    entity_id=entity.id,
                    report_date=today,
                    title="主体出现流动性压力",
                    risk_level="高",
                    summary="主体直接负面信号",
                    provenance="real",
                    relevance="direct",
                    sentiment_direction="negative",
                    credit_impact="medium",
                    confidence=0.8,
                    review_status="pending",
                ),
            ]
            db.add_all(rows)
            db.flush()
            self.assertEqual(suggested_credit_for_entity(db, entity.id), "预警")
            refresh_entity_credit(db, entity, force=True)
            self.assertEqual(entity.credit_level, "预警")

    def test_cross_entity_and_contextual_sources_are_separated(self) -> None:
        with isolated_session() as db:
            godiva = self._entity(db)
            glp = self._entity(db, "普洛斯")
            profile = configured_entity_catalog().find((godiva.name,))
            source_item = {
                "title": "COCOBOD publishes cocoa crop update",
                "snippet": "The cocoa board published a crop update for the industry.",
                "entity_key": "Godiva",
                "relation": "contextual",
                "source_type": "industry",
            }
            godiva_row = {
                "标题": "加纳可可局发布产量更新",
                "核心摘要": "行业供应背景信息。",
                "影响方向": "negative",
                "信用风险信号": "high",
                "_source_item": source_item,
            }
            self.assertFalse(prepare_entity_row(godiva, godiva_row, profile))

            glp_profile = configured_entity_catalog().find((glp.name,))
            glp_row = dict(godiva_row)
            glp_row["_source_item"] = source_item
            self.assertFalse(prepare_entity_row(glp, glp_row, glp_profile))

            named_supply = {
                "标题": "Barry Callebaut 对 Godiva 交货中断",
                "核心摘要": "可可供应商 Barry Callebaut 宣布停供。",
                "影响方向": "negative",
                "信用风险信号": "high",
                "_source_item": {
                    "title": "Barry Callebaut halts deliveries to Godiva",
                    "snippet": "The cocoa supplier Barry Callebaut halted shipments.",
                    "entity_key": "Godiva",
                    "relation": "contextual",
                    "source_type": "media",
                },
            }
            self.assertTrue(prepare_entity_row(godiva, named_supply, profile))
            self.assertEqual(named_supply["主体相关性"], "contextual")
            self.assertEqual(named_supply["信用风险信号"], "none")
            self.assertFalse(prepare_entity_row(glp, dict(named_supply), glp_profile))

    def test_untraceable_model_row_is_rejected(self) -> None:
        with isolated_session() as db:
            entity = self._entity(db)
            profile = configured_entity_catalog().find((entity.name,))
            row = {
                "标题": "Godiva 据称出现重大流动性问题",
                "关联企业": "Godiva",
                "核心摘要": "模型输出没有对应采集候选。",
                "影响方向": "negative",
                "信用风险信号": "critical",
                "置信度": 0.99,
            }
            self.assertFalse(prepare_entity_row(entity, row, profile))

    def test_real_signal_without_confidence_does_not_raise_warning(self) -> None:
        with isolated_session() as db:
            entity = self._entity(db)
            db.add(
                EntityRisk(
                    entity_id=entity.id,
                    report_date=date.today(),
                    title="置信度缺失的负面事件",
                    risk_level="极高",
                    summary="没有可信度字段。",
                    provenance="real",
                    relevance="direct",
                    sentiment_direction="negative",
                    credit_impact="critical",
                    confidence=None,
                    review_status="pending",
                )
            )
            db.flush()
            self.assertEqual(suggested_credit_for_entity(db, entity.id), "正常")


class EntityPipelineTests(unittest.TestCase):
    def test_contextual_items_do_not_satisfy_direct_source_target(self) -> None:
        with isolated_session() as db:
            seed_default_entities(db)
            entity = db.query(TargetEntity).filter(TargetEntity.name == "Godiva").one()
            pipeline = RiskPipeline(db, entity_id=entity.id, window_hours=24)
            items = [
                {
                    "title": "COCOBOD releases cocoa crop update",
                    "snippet": "The cocoa industry crop forecast has been updated today.",
                    "url": "https://cocobod.gh/news/crop-update",
                    "entity_key": "Godiva",
                    "relation": "contextual",
                    "source_type": "industry",
                },
                {
                    "title": "Godiva publishes a company update",
                    "snippet": "Godiva published a new company update for stakeholders today.",
                    "url": "https://www.godiva.com/blogs/media/company-update",
                    "entity_key": "Godiva",
                    "relation": "direct",
                    "source_type": "official",
                },
            ]
            self.assertEqual(pipeline._count_valid_items(items, "A"), 1)

    def test_stale_degraded_item_is_not_saved(self) -> None:
        with isolated_session() as db:
            seed_default_entities(db)
            entity = db.query(TargetEntity).filter(TargetEntity.name == "Godiva").one()
            pipeline = RiskPipeline(db, entity_id=entity.id, window_hours=24)
            old = datetime.now() - timedelta(days=30)
            count = pipeline._save_structured_entries(
                [
                    {
                        "标题": "Godiva 过期材料",
                        "关联企业": "Godiva",
                        "风险类别": "企业经营",
                        "风险等级": "低",
                        "核心摘要": "这是一条带有明确旧发布时间的原始摘要。",
                        "影响分析": "待复核",
                        "来源链接": "https://example.org/old",
                        "来源名称": "旧材料",
                        "发布时间": old.isoformat(),
                        "主体相关性": "direct",
                        "影响方向": "unknown",
                        "信用风险信号": "none",
                        "_degraded": True,
                    }
                ],
                module_code="A",
                report_date=date.today(),
                metadata={},
                search_log_id=None,
                raw_context="{}",
            )
            self.assertEqual(count, 0)
            self.assertEqual(db.query(EntityRisk).count(), 0)

    def test_empty_collection_does_not_create_mock_by_default(self) -> None:
        class EmptyPipeline(RiskPipeline):
            def _collect_rss(self, module_code, funnel):
                return {"source": "rss", "items": [], "metadata": {}, "error": None}

            def _collect_direct_sites(self, module_code, funnel):
                return {"source": "direct_site", "items": [], "metadata": {}, "error": "未配置直连站点"}

            def _analyze_merged(self, *args, **kwargs):
                return []

        with isolated_session() as db:
            seed_default_entities(db)
            entity = db.query(TargetEntity).filter(TargetEntity.name == "普洛斯").one()
            pipeline = EmptyPipeline(db, entity_id=entity.id, window_hours=24)
            original = (
                pipeline.settings.entity_demo_mode,
                pipeline.settings.mita_api_key,
                pipeline.settings.gemini_api_key,
            )
            try:
                pipeline.settings.entity_demo_mode = False
                pipeline.settings.mita_api_key = ""
                pipeline.settings.gemini_api_key = ""
                with patch("app.services.api_keys.is_placeholder_key", return_value=True):
                    count = pipeline.run_module("A", date.today())
            finally:
                (
                    pipeline.settings.entity_demo_mode,
                    pipeline.settings.mita_api_key,
                    pipeline.settings.gemini_api_key,
                ) = original
            self.assertEqual(count, 0)
            self.assertEqual(db.query(EntityRisk).count(), 0)
            run = db.query(ReportRun).filter(ReportRun.module_code == "A").one()
            funnel = json.loads(run.funnel_json)
            self.assertNotIn("demo_fallback", funnel)


class EntityPresentationAndMigrationTests(unittest.TestCase):
    def test_page_date_is_strict_and_demo_is_hidden(self) -> None:
        with isolated_session() as db:
            seed_default_entities(db)
            entity = db.query(TargetEntity).filter(TargetEntity.name == "Godiva").one()
            target_day = date(2026, 8, 7)
            db.add_all(
                [
                    EntityRisk(
                        entity_id=entity.id,
                        report_date=target_day,
                        title="Godiva 召回部分产品",
                        risk_level="低",
                        summary="监管机构公布召回。",
                        provenance="real",
                        relevance="direct",
                    ),
                    EntityRisk(
                        entity_id=entity.id,
                        report_date=target_day - timedelta(days=1),
                        title="Godiva 发布年度财报",
                        risk_level="低",
                        summary="利润下滑已披露。",
                        provenance="real",
                        relevance="direct",
                        risk_category="金融与经营数据",
                    ),
                    EntityRisk(
                        entity_id=entity.id,
                        report_date=target_day,
                        title="目标日演示事件",
                        risk_level="高",
                        summary="演示",
                        provenance="demo",
                        relevance="direct",
                    ),
                    EntityRisk(
                        entity_id=entity.id,
                        report_date=target_day,
                        title="行业价格背景波动",
                        risk_level="低",
                        summary="宏观背景",
                        provenance="real",
                        relevance="contextual",
                        risk_category="公开舆情",
                    ),
                    EntityRisk(
                        entity_id=entity.id,
                        report_date=target_day,
                        title="Barry Callebaut 作为供应商交货中断",
                        risk_level="中",
                        summary="上下游企业 Barry Callebaut 停供。",
                        provenance="real",
                        relevance="contextual",
                        risk_category="供应链/关联方监控",
                    ),
                ]
            )
            db.commit()
            request = Request({"type": "http", "method": "GET", "path": "/entity-assessment", "headers": []})
            with patch("app.services.entity_briefing.fetch_briefing_headlines", return_value=[]):
                with patch("app.services.entity_briefing.is_placeholder_key", return_value=True):
                    context = _entity_assessment_context(
                        request=request,
                        report_date=target_day.isoformat(),
                        entity_id=entity.id,
                        db=db,
                    )
            self.assertEqual(
                [risk.title for risk in context["risks"]],
                [
                    "Barry Callebaut 作为供应商交货中断",
                    "Godiva 召回部分产品",
                    "Godiva 发布年度财报",
                ],
            )
            self.assertEqual(context["news_lookback_days"], 90)
            self.assertEqual(context["news_lookback_start"], "2026-05-09")
            self.assertGreaterEqual(len(context["entity_sources"]), 4)
            self.assertEqual(context["latest_news"]["status"], "ready")
            self.assertEqual(context["latest_news"]["mode"], "overview")
            self.assertEqual(context["latest_news"]["highlights"], [])
            self.assertIn("未见重大", context["latest_news"]["summary"])
            self.assertTrue(context["latest_news"]["uses_llm_search"])
            self.assertFalse(context["latest_news"]["direct_pending"])
            self.assertGreaterEqual(context["latest_news"]["direct_source_count"], 1)
            self.assertEqual(
                [stmt["key"] for stmt in context["financials"]["statements"]],
                ["income", "balance", "cashflow"],
            )
            self.assertEqual(
                [stmt["title"] for stmt in context["financials"]["statements"]],
                ["损益表要点 (通期)", "资产负债表要点 (通期)", "现金流量表要点 (通期)"],
            )
            self.assertEqual(
                [col["label"] for col in context["financials"]["statements"][0]["columns"]],
                ["报告期", "营业收入", "营业利润", "经常利润", "净利润", "每股收益", "每股分红", "发布日"],
            )
            self.assertIn(
                "yildizholding.com.tr",
                context["financials"]["source_page_url"],
            )
            self.assertTrue(context["financials"]["is_alternative_context"])
            self.assertEqual(
                [group["key"] for group in context["entity_groups"]],
                ["重点关注", "五大商社", "三大银行", "三大券商", "其他"],
            )
            focus_names = [ent.name for ent in context["entity_groups"][0]["entities"]]
            self.assertEqual(focus_names[:2], ["Godiva", "普洛斯"])
            self.assertNotIn("credit_logs", context)

    def test_switching_entity_does_not_block_on_live_fetch(self) -> None:
        with isolated_session() as db:
            seed_default_entities(db)
            first = db.query(TargetEntity).filter(TargetEntity.name == "Godiva").one()
            second = db.query(TargetEntity).filter(TargetEntity.name == "三菱商事").one()
            request = Request({"type": "http", "method": "GET", "path": "/entity-assessment", "headers": []})
            blocked = AssertionError("page render must not wait on live fetch")
            with patch("app.services.entity_kabutan.get_http_client", side_effect=blocked):
                with patch("app.services.entity_financial_pdf.get_http_client", side_effect=blocked):
                    with patch("app.services.entity_briefing_feed.RssNewsCollector", side_effect=blocked):
                        with patch("app.services.entity_briefing.gemini_for", side_effect=blocked):
                            first_ctx = _entity_assessment_context(
                                request=request,
                                report_date=date.today().isoformat(),
                                entity_id=first.id,
                                db=db,
                            )
                            second_ctx = _entity_assessment_context(
                                request=request,
                                report_date=date.today().isoformat(),
                                entity_id=second.id,
                                db=db,
                            )
            self.assertEqual(first_ctx["selected_entity"].id, first.id)
            self.assertEqual(second_ctx["selected_entity"].id, second.id)
            self.assertNotEqual(first_ctx["selected_entity"].id, second_ctx["selected_entity"].id)
            self.assertEqual(second_ctx["selected_entity"].name, "三菱商事")

    def test_force_demo_seed_never_deletes_real_events(self) -> None:
        with isolated_session() as db:
            seed_default_entities(db)
            entity = db.query(TargetEntity).filter(TargetEntity.name == "Godiva").one()
            db.add(
                EntityRisk(
                    entity_id=entity.id,
                    report_date=date.today(),
                    title="必须保留的真实事件",
                    risk_level="低",
                    summary="真实来源",
                    provenance="real",
                )
            )
            db.commit()
            seed_entity_demo_data(db, force=True)
            real_titles = [
                row.title
                for row in db.query(EntityRisk)
                .filter(
                    EntityRisk.entity_id == entity.id,
                    EntityRisk.provenance == "real",
                )
                .all()
            ]
            self.assertEqual(real_titles, ["必须保留的真实事件"])

    def test_old_entity_table_gets_provenance_columns_and_demo_tag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            engine = create_engine(f"sqlite:///{Path(tmp) / 'old-entity.db'}")
            try:
                with engine.begin() as conn:
                    conn.exec_driver_sql(
                        "CREATE TABLE entity_risks ("
                        "id INTEGER PRIMARY KEY, entity_id INTEGER NOT NULL, report_date DATE NOT NULL, "
                        "title VARCHAR(512) NOT NULL, risk_level VARCHAR(16) NOT NULL, summary TEXT NOT NULL, "
                        "source_url VARCHAR(1024), structured_json TEXT)"
                    )
                    conn.exec_driver_sql(
                        "INSERT INTO entity_risks "
                        "(id, entity_id, report_date, title, risk_level, summary, source_url) "
                        "VALUES (1, 1, '2026-08-06', '演示', '高', '演示', "
                        "'https://example.com/mock/item')"
                    )
                _migrate_sqlite_columns(engine)
                columns = {col["name"] for col in inspect(engine).get_columns("entity_risks")}
                with engine.connect() as conn:
                    row = conn.exec_driver_sql(
                        "SELECT provenance, review_status, credit_impact FROM entity_risks WHERE id=1"
                    ).one()
                self.assertTrue({"published_at", "provenance", "credit_impact"}.issubset(columns))
                self.assertEqual(row, ("demo", "rejected", "none"))
            finally:
                engine.dispose()

    def test_entity_prompt_separates_importance_from_credit_signal(self) -> None:
        prompt = build_system_prompt(
            module_code="A", target_entity="Godiva", window_hours=24
        )
        self.assertIn("资讯重要度", prompt)
        self.assertIn("信用风险信号", prompt)
        self.assertIn("不得因新闻重要就判为负面", prompt)

    def test_export_hides_demo_risks(self) -> None:
        with isolated_session() as db:
            seed_default_entities(db)
            entity = db.query(TargetEntity).filter(TargetEntity.name == "Godiva").one()
            demo_risk = EntityRisk(
                entity_id=entity.id,
                report_date=date.today(),
                title="演示风险",
                risk_level="高",
                summary="演示",
                provenance="demo",
            )
            real_risk = EntityRisk(
                entity_id=entity.id,
                report_date=date.today(),
                title="Godiva 召回部分产品",
                risk_level="低",
                summary="监管机构公布召回。",
                provenance="real",
            )
            db.add_all([demo_risk, real_risk])
            db.commit()

            with patch("app.api.routes.export_entity_assessment_to_path") as exporter:
                response = export_entity_assessment_docx(
                    entity.id, report_date=date.today(), db=db
                )
            self.assertIn("企业公开信息风险监测简报", response.filename)
            self.assertNotIn("credit_logs", exporter.call_args.kwargs)
            exported_risks = exporter.call_args.kwargs["risks"]
            self.assertEqual([risk.title for risk in exported_risks], ["Godiva 召回部分产品"])


if __name__ == "__main__":
    unittest.main()
