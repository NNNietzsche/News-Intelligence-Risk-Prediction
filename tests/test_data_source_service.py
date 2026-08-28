from __future__ import annotations

import hashlib
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

from app.database.models import IndustryDataSource, IndustryReport, IndustrySourceChunk
from app.services import data_source_service
from app.services.data_source_service import (
    append_industry_network_search_sources,
    clone_industry_sources,
    save_industry_file_source,
    save_industry_url_source,
    delete_industry_source,
)
from app.services.data_source_parser import FetchedSource, parse_html_with_chunks
from tests.helpers import isolated_session


@dataclass
class _SearchItem:
    title: str = "某企业发布经营数据"
    url: str = "https://example.com/result"
    snippet: str = "搜索摘要称收入增长12.5%。"
    published_at: str = "2026-08-01"
    source_domain: str = "example.com"


class IndustryDataSourceServiceTests(unittest.TestCase):
    def test_file_upload_saves_registry_and_chunks_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, isolated_session() as db:
            report = IndustryReport(industry_name="测试行业", status="draft", version=1)
            db.add(report)
            db.commit()
            with patch.object(data_source_service, "INDUSTRY_UPLOAD_ROOT", Path(tmp)):
                raw = "第一段\n\n收入为100亿元。".encode("utf-8")
                row = save_industry_file_source(db, report.id, "测试文本", "source.txt", raw)
                chunks = db.query(IndustrySourceChunk).filter_by(source_id=row.id).all()
            self.assertEqual(row.source_origin, "customer_file")
            self.assertEqual(row.raw_content_hash, hashlib.sha256(raw).hexdigest())
            self.assertEqual(
                row.extracted_text_hash,
                hashlib.sha256(row.extracted_text.encode("utf-8")).hexdigest(),
            )
            self.assertEqual(row.content_hash, row.extracted_text_hash)
            self.assertEqual(row.evidence_grade, "full_text")
            self.assertEqual(len(chunks), 2)
            self.assertTrue(all(chunk.report_id == report.id for chunk in chunks))

    def test_reupload_same_file_creates_predictable_independent_sources(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, isolated_session() as db:
            report = IndustryReport(industry_name="测试行业", status="draft", version=1)
            db.add(report)
            db.commit()
            raw = b"same content"
            with patch.object(data_source_service, "INDUSTRY_UPLOAD_ROOT", Path(tmp)):
                first = save_industry_file_source(db, report.id, "A", "same.txt", raw)
                second = save_industry_file_source(db, report.id, "B", "same.txt", raw)
            self.assertNotEqual(first.id, second.id)
            self.assertEqual(first.raw_content_hash, second.raw_content_hash)
            self.assertNotEqual(first.file_path, second.file_path)

    def test_network_search_is_lead_only_and_has_chunks(self) -> None:
        with isolated_session() as db:
            report = IndustryReport(industry_name="测试行业", status="running", version=1)
            db.add(report)
            db.commit()
            rows = append_industry_network_search_sources(db, report.id, [_SearchItem()])
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(row.source_origin, "network_search")
            self.assertFalse(row.is_full_text)
            self.assertEqual(row.evidence_grade, "lead_only")
            self.assertIsNone(row.source_publisher)
            self.assertEqual(row.published_at, "2026-08-01")
            self.assertGreater(len(row.chunks), 0)

    def test_network_search_translation_is_used_and_original_is_traceable(self) -> None:
        with isolated_session() as db:
            report = IndustryReport(industry_name="能源", status="running", version=1)
            db.add(report)
            db.commit()
            rows = append_industry_network_search_sources(
                db,
                report.id,
                [_SearchItem(title="Tesla Enterprise Risk Assessment", snippet="Downside probability 20%.")],
                translator=lambda title, snippet: ("特斯拉企业风险评估", "下行情景概率为20%。"),
                require_translation=True,
            )
            self.assertEqual(rows[0].name, "特斯拉企业风险评估")
            self.assertIn("标题摘要：特斯拉企业风险评估", rows[0].extracted_text)
            self.assertIn("搜索摘要：下行情景概率为20%", rows[0].extracted_text)
            self.assertIn("原始摘要（追溯）：Downside probability 20%.", rows[0].extracted_text)

    def test_network_search_keeps_original_when_translation_fails(self) -> None:
        with isolated_session() as db:
            report = IndustryReport(industry_name="能源", status="running", version=1)
            db.add(report)
            db.commit()

            def _boom(title: str, snippet: str) -> tuple[str, str]:
                raise ValueError("network_source_translation_not_chinese")

            rows = append_industry_network_search_sources(
                db,
                report.id,
                [_SearchItem(title="Tesla Enterprise Risk Assessment", snippet="Downside probability 20%.")],
                translator=_boom,
                require_translation=False,
            )
            self.assertEqual(len(rows), 1)
            self.assertIn("Tesla Enterprise Risk Assessment", rows[0].name)
            self.assertIn("Downside probability 20%.", rows[0].extracted_text)
            self.assertNotIn("原始摘要（追溯）：", rows[0].extracted_text)

    def test_completed_report_can_delete_network_source_only(self) -> None:
        with isolated_session() as db:
            report = IndustryReport(industry_name="能源", status="running", version=1)
            db.add(report)
            db.commit()
            row = append_industry_network_search_sources(db, report.id, [_SearchItem()])[0]
            source_id = row.id
            report.status = "completed"
            db.commit()
            self.assertTrue(delete_industry_source(db, report.id, source_id))
            self.assertIsNone(db.get(IndustryDataSource, source_id))
            self.assertEqual(db.query(IndustrySourceChunk).filter_by(source_id=source_id).count(), 0)

    def test_completed_report_can_upload_source_without_changing_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, isolated_session() as db:
            report = IndustryReport(industry_name="能源", status="completed", version=1)
            db.add(report)
            db.commit()
            with patch.object(data_source_service, "INDUSTRY_UPLOAD_ROOT", Path(tmp)):
                row = save_industry_file_source(
                    db, report.id, "补充材料", "extra.txt", "新增正文。".encode("utf-8")
                )
            db.refresh(report)
            self.assertEqual(report.status, "completed")
            self.assertEqual(row.report_id, report.id)
            self.assertTrue(row.is_selected)

    def test_customer_url_uses_download_bytes_for_raw_hash(self) -> None:
        raw = b"<html><h1>Title</h1><p>Revenue 100 USD</p></html>"
        fetched = FetchedSource(
            raw_content=raw,
            parsed=parse_html_with_chunks(raw.decode("utf-8")),
            mime_type="text/html",
            final_url="https://example.com/final",
        )
        with isolated_session() as db:
            report = IndustryReport(industry_name="测试行业", status="draft", version=1)
            db.add(report)
            db.commit()
            with patch.object(data_source_service, "fetch_url_source", return_value=fetched):
                row = save_industry_url_source(
                    db, report.id, "客户网页", "https://example.com/source"
                )
            self.assertEqual(row.source_origin, "customer_url")
            self.assertEqual(row.raw_content_hash, hashlib.sha256(raw).hexdigest())
            self.assertTrue(row.is_full_text)
            self.assertEqual(row.evidence_grade, "full_text")
            self.assertEqual(row.url, "https://example.com/source")
            self.assertEqual(len(row.chunks), 2)

    def test_clone_creates_new_sources_and_new_chunks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, isolated_session() as db:
            parent = IndustryReport(industry_name="测试行业", status="completed", version=1)
            child = IndustryReport(industry_name="测试行业", status="draft", version=2)
            db.add_all([parent, child])
            db.commit()
            with patch.object(data_source_service, "INDUSTRY_UPLOAD_ROOT", Path(tmp)):
                original = IndustryDataSource(
                    report_id=parent.id,
                    name="来源",
                    source_type="url",
                    url="https://example.com",
                    extracted_text="原文",
                    content_hash=hashlib.sha256("原文".encode()).hexdigest(),
                    char_count=2,
                    source_origin="customer_url",
                    parse_status="parsed",
                )
                db.add(original)
                db.flush()
                db.add(
                    IndustrySourceChunk(
                        report_id=parent.id,
                        source_id=original.id,
                        chunk_index=0,
                        text="原文",
                        locator="网页段落1",
                        paragraph_index=1,
                        char_start=0,
                        char_end=2,
                        content_hash=hashlib.sha256("原文".encode()).hexdigest(),
                    )
                )
                db.commit()
                clone_industry_sources(db, parent.id, child.id)
            cloned = db.query(IndustryDataSource).filter_by(report_id=child.id).one()
            cloned_chunk = db.query(IndustrySourceChunk).filter_by(source_id=cloned.id).one()
            self.assertNotEqual(cloned.id, original.id)
            self.assertNotEqual(cloned_chunk.source_id, original.id)
            self.assertEqual(cloned_chunk.report_id, child.id)

    def test_source_delete_cascades_to_chunks(self) -> None:
        with isolated_session() as db:
            report = IndustryReport(industry_name="测试行业", status="draft", version=1)
            db.add(report)
            db.commit()
            source = IndustryDataSource(
                report_id=report.id,
                name="来源",
                source_type="url",
                extracted_text="正文",
                char_count=2,
            )
            db.add(source)
            db.flush()
            db.add(
                IndustrySourceChunk(
                    report_id=report.id,
                    source_id=source.id,
                    chunk_index=0,
                    text="正文",
                    locator="网页段落1",
                    content_hash=hashlib.sha256("正文".encode()).hexdigest(),
                )
            )
            db.commit()
            source_id = source.id
            db.delete(source)
            db.commit()
            self.assertEqual(
                db.query(IndustrySourceChunk).filter_by(source_id=source_id).count(), 0
            )


class IndustrySourceLibraryTests(unittest.TestCase):
    def test_library_upload_is_reused_by_new_draft(self) -> None:
        from app.services.industry_analysis import IndustryAnalysisService

        with tempfile.TemporaryDirectory() as tmp, isolated_session() as db:
            service = IndustryAnalysisService(db)
            library = service.get_or_create_source_library("新能源")
            with patch.object(data_source_service, "INDUSTRY_UPLOAD_ROOT", Path(tmp)):
                save_industry_file_source(
                    db, library.id, "库文件", "lib.txt", "库正文。".encode("utf-8")
                )
            listed_ids = [row.id for row in service.list_reports()]
            self.assertNotIn(library.id, listed_ids)
            draft = service.create_draft("新能源")
            self.assertEqual(
                db.query(IndustryDataSource).filter(IndustryDataSource.report_id == draft.id).count(),
                0,
            )
            kept = (
                db.query(IndustryDataSource)
                .filter(IndustryDataSource.report_id == library.id)
                .all()
            )
            self.assertTrue(any((src.extracted_text or "").find("库正文") >= 0 for src in kept))


if __name__ == "__main__":
    unittest.main()
