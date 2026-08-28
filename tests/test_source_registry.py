from __future__ import annotations

import hashlib
import unittest

from app.services.data_source_parser import parse_text_with_chunks
from app.services.source_registry import build_registry_metadata


class SourceRegistryTests(unittest.TestCase):
    def test_raw_and_extracted_hashes_are_independent(self) -> None:
        raw = b"raw bytes that are not the extracted text"
        parsed = parse_text_with_chunks("解析后的正文")
        metadata = build_registry_metadata(
            raw_content=raw,
            parsed=parsed,
            source_origin="customer_file",
            mime_type="text/plain",
        )
        self.assertEqual(metadata.raw_content_hash, hashlib.sha256(raw).hexdigest())
        self.assertEqual(
            metadata.extracted_text_hash,
            hashlib.sha256(parsed.extracted_text.encode("utf-8")).hexdigest(),
        )
        self.assertNotEqual(metadata.raw_content_hash, metadata.extracted_text_hash)
        self.assertEqual(metadata.evidence_grade, "full_text")

    def test_network_snippet_is_always_lead_only(self) -> None:
        parsed = parse_text_with_chunks("只有标题和搜索摘要", format_name="network_search")
        metadata = build_registry_metadata(
            raw_content=parsed.extracted_text.encode("utf-8"),
            parsed=parsed,
            source_origin="network_search",
            is_full_text=True,
        )
        self.assertFalse(metadata.is_full_text)
        self.assertEqual(metadata.evidence_grade, "lead_only")


if __name__ == "__main__":
    unittest.main()
