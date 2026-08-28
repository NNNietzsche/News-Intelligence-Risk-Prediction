from __future__ import annotations

import unittest

from app.services.citation_rendering import build_citation_context, render_report_html
from app.services.grounded_report import GroundedReportService
from tests.formal_grounded_helpers import FormalFakeAnalyzer, grounded_candidate, make_ready_report
from tests.helpers import isolated_session


class GroundedReportWebTests(unittest.TestCase):
    def test_html_uses_clickable_display_number_and_escapes_report_text(self):
        with isolated_session() as db:
            report, _, _, _, card = make_ready_report(db)
            candidate = grounded_candidate(card)
            candidate["title"] = '<script>alert("x")</script>'
            run = GroundedReportService(db, FormalFakeAnalyzer(generated=candidate)).generate(report.id)
            GroundedReportService(db).promote(
                report.id, run.id, promotion_type="manual", promotion_note="approved",
            )
            rendered = render_report_html(build_citation_context(db, report))
            self.assertIn('data-evidence-code="E000001"', rendered)
            self.assertIn(">[1]</button>", rendered)
            self.assertNotIn("<script>", rendered)
            self.assertIn("&lt;script&gt;", rendered)


if __name__ == "__main__":
    unittest.main()
