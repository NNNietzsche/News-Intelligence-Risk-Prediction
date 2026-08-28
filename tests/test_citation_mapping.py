from __future__ import annotations

import unittest

from app.services.citation_rendering import citation_number_map


class CitationMappingTests(unittest.TestCase):
    def test_first_occurrence_order_is_stable_across_report_fields(self):
        payload = {
            "summary": "摘要[E000002]及重复[E000002]。",
            "sections": [{"content": "正文[E000001]。"}],
            "risk_outlook": "展望[E000003]。",
            "key_metrics": [{"name": "收入", "value": "100", "evidence_code": "E000004"}],
        }
        self.assertEqual(citation_number_map(payload), {
            "E000002": 1, "E000001": 2, "E000003": 3, "E000004": 4,
        })


if __name__ == "__main__":
    unittest.main()
