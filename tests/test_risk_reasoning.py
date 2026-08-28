import unittest

from app.services.risk_reasoning import build_risk_reasoning


class RiskReasoningTests(unittest.TestCase):
    def test_uses_existing_evidence_without_inventing_new_facts(self) -> None:
        chain = build_risk_reasoning(
            title="监管发布产品召回公告",
            summary="监管公告涉及部分批次产品。",
            impact="若召回范围扩大，可能增加处置和沟通成本。",
            risk_level="中",
            category="司法/行政监管",
        )
        self.assertEqual(chain["fact"], "监管公告涉及部分批次产品")
        self.assertIn("若召回范围扩大", chain["transmission"])
        self.assertIn("不宜直接推定", chain["judgement"])
        self.assertIn("监管文件", chain["focus"])
        self.assertTrue(chain["show"])

    def test_missing_analysis_falls_back_to_cautious_category_path(self) -> None:
        chain = build_risk_reasoning(
            title="供应商经营动态",
            risk_level="低",
            category="供应链/关联方监控",
        )
        self.assertIn("公开来源披露", chain["fact"])
        self.assertIn("受影响供应商、原料或物流节点", chain["transmission"])
        self.assertIn("暂不构成重大负面判断", chain["judgement"])

    def test_duplicate_summary_and_impact_use_category_path(self) -> None:
        chain = build_risk_reasoning(
            title="经营动态",
            summary="公司发布经营动态。",
            impact="公司发布经营动态。",
            risk_level="低",
            category="金融与经营数据",
        )
        self.assertIn("收入确认、利润率、融资成本", chain["transmission"])

    def test_title_repeated_as_impact_uses_actual_currency_transmission(self) -> None:
        title = "中东局势动荡引发美元避险需求，墨西哥比索下跌"
        chain = build_risk_reasoning(
            title=title,
            summary="受中东局势动荡影响，美元避险需求升温，导致墨西哥比索汇率下滑。",
            impact=title,
            risk_level="中",
            category="市场行情",
        )
        self.assertNotEqual(chain["transmission"], title)
        self.assertIn("外币结算", chain["transmission"])

    def test_unknown_event_never_uses_generic_materiality_template(self) -> None:
        chain = build_risk_reasoning(
            title="企业发布业务动态",
            summary="公司披露一项新的业务安排。",
            risk_level="低",
            category="其他",
        )
        self.assertNotIn("该信息是否形成实质影响", chain["transmission"])
        self.assertFalse(chain["show"])
        self.assertIn("业务、资金、合同与合规责任", chain["transmission"])
