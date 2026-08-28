"""风险情报智能体的检索边界测试。"""

from app.services.risk_intelligence_agent import RiskIntelligenceAgent


def test_agent_plan_keeps_controlled_metadata_and_removes_duplicates():
    baseline = [
        {"module": "B", "query": "中东 石油 官方声明", "metadata": {"region": "中东"}},
        {"module": "B", "query": "中东 航运 港口 制裁", "metadata": {"region": "中东"}},
    ]
    planned = [
        {"query": "中东 石油 官方声明 OPEC 政策"},
        {"query": "中东 石油 官方声明 OPEC 政策"},
    ]

    merged = RiskIntelligenceAgent._merge_with_baseline(planned, baseline)

    assert merged[0]["metadata"] == {"region": "中东"}
    assert merged[0]["agent_planned"] is True
    # 第二条模型重复时，应回退到该任务原始查询，不能少搜一个受控任务。
    assert merged[1]["query"] == "中东 航运 港口 制裁"
