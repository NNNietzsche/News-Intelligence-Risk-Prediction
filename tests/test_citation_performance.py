from __future__ import annotations

import unittest

from sqlalchemy import event

from app.services.citation_rendering import build_citation_context
from app.services.grounded_report import GroundedReportService
from tests.formal_grounded_helpers import FormalFakeAnalyzer, grounded_candidate, make_ready_report
from tests.helpers import isolated_session


class CitationPerformanceTests(unittest.TestCase):
    def test_citation_context_uses_bounded_batch_queries(self):
        with isolated_session() as db:
            report, _, _, _, card = make_ready_report(db)
            run = GroundedReportService(
                db, FormalFakeAnalyzer(generated=grounded_candidate(card)),
            ).generate(report.id)
            GroundedReportService(db).promote(
                report.id, run.id, promotion_type="manual", promotion_note="approved",
            )
            count = 0

            def before_cursor_execute(*_args):
                nonlocal count
                count += 1

            event.listen(db.bind, "before_cursor_execute", before_cursor_execute)
            try:
                build_citation_context(db, report)
            finally:
                event.remove(db.bind, "before_cursor_execute", before_cursor_execute)
            self.assertLessEqual(count, 6)


if __name__ == "__main__":
    unittest.main()
