from __future__ import annotations

import unittest

from app.services.conflict_detection import ConflictDetectionService
from app.services.evidence_packet import build_evidence_packet
from tests.helpers import isolated_session
from tests.test_conflict_detection import add_card, setup_report


class EvidencePacketTests(unittest.TestCase):
    def test_packet_only_contains_current_eligible_evidence(self):
        with isolated_session() as db:
            report, run = setup_report(db)
            add_card(db, report, run, "E000001", "100")
            add_card(db, report, run, "E000002", "110", status="rejected")
            add_card(db, report, run, "E000003", "120", status="stale")
            add_card(db, report, run, "E000004", "130", status="lead_only", grade="lead_only", manual=True)
            add_card(db, report, run, "E000005", "140", claim_type="inference")
            add_card(db, report, run, "E000006", "150", manual=True)
            packet = build_evidence_packet(db, report.id)
            self.assertEqual([item["evidence_code"] for item in packet["evidence"]], ["E000001"])
            self.assertGreaterEqual(packet["coverage"]["excluded_evidence_count"], 5)

    def test_partial_text_is_included_with_explicit_limitation(self):
        with isolated_session() as db:
            report, run = setup_report(db)
            add_card(db, report, run, "E000001", "100", grade="partial_text")
            packet = build_evidence_packet(db, report.id)
            self.assertEqual(packet["coverage"]["partial_text_count"], 1)
            self.assertTrue(any("部分正文" in item for item in packet["limitations"]))

    def test_changed_chunk_hash_removes_evidence_from_packet(self):
        with isolated_session() as db:
            report, run = setup_report(db)
            _, chunk, _ = add_card(db, report, run, "E000001", "100")
            chunk.text += "已变化"
            db.commit()
            packet = build_evidence_packet(db, report.id)
            self.assertEqual(packet["evidence"], [])
            self.assertEqual(packet["coverage"]["excluded_by_reason"]["invalid_chunk_hash"], 1)

    def test_unresolved_material_conflict_is_preserved(self):
        with isolated_session() as db:
            report, run = setup_report(db)
            add_card(db, report, run, "E000001", "100", risk_tags=["financing_debt"], importance=5)
            add_card(db, report, run, "E000002", "200", risk_tags=["financing_debt"], importance=5)
            ConflictDetectionService(db).detect(report.id)
            packet = build_evidence_packet(db, report.id)
            self.assertEqual(len(packet["unresolved_conflicts"]), 1)
            self.assertTrue(any("未解决冲突" in item for item in packet["limitations"]))
            self.assertTrue(all(item["usage_policy"] == "conflicted_do_not_select" for item in packet["evidence"]))

    def test_resolved_selected_only_exposes_selected_evidence(self):
        with isolated_session() as db:
            report, run = setup_report(db)
            add_card(db, report, run, "E000001", "100")
            add_card(db, report, run, "E000002", "200")
            service = ConflictDetectionService(db)
            service.detect(report.id)
            conflict = service.list_conflicts(report.id)[0]
            service.resolve(report.id, conflict.conflict_code, "resolved_selected", "采用审计值", "E000001")
            packet = build_evidence_packet(db, report.id)
            self.assertEqual([item["evidence_code"] for item in packet["evidence"]], ["E000001"])
            self.assertEqual(packet["evidence"][0]["usage_policy"], "selected_value")
            self.assertEqual(packet["resolved_conflicts"][0]["selected_evidence_code"], "E000001")

    def test_missing_topics_and_network_only_limitations_are_explicit(self):
        with isolated_session() as db:
            report, run = setup_report(db)
            add_card(
                db, report, run, "E000001", "100", status="lead_only", grade="lead_only",
                manual=True, source_origin="network_search",
            )
            packet = build_evidence_packet(db, report.id)
            self.assertFalse(packet["evidence"])
            self.assertTrue(packet["missing_information"])
            self.assertTrue(any("网络线索" in item for item in packet["limitations"]))

    def test_current_source_registry_can_restrict_an_old_card(self):
        with isolated_session() as db:
            report, run = setup_report(db)
            source, _, _ = add_card(db, report, run, "E000001", "100")
            source.source_origin = "network_search"
            source.evidence_grade = "lead_only"
            source.is_full_text = False
            db.commit()
            packet = build_evidence_packet(db, report.id)
            self.assertEqual(packet["evidence"], [])

    def test_stale_conflict_run_is_not_treated_as_current(self):
        with isolated_session() as db:
            report, run = setup_report(db)
            add_card(db, report, run, "E000001", "100")
            ConflictDetectionService(db).detect(report.id)
            add_card(db, report, run, "E000002", "200", metric="利润")
            packet = build_evidence_packet(db, report.id)
            self.assertTrue(packet["coverage"]["conflict_snapshot_stale"])
            self.assertTrue(any("过期" in item for item in packet["limitations"]))


if __name__ == "__main__":
    unittest.main()
