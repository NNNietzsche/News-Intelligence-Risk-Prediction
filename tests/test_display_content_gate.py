from types import SimpleNamespace

from app.services.display_zh import build_display_cards, has_publishable_content_detail


def test_hides_news_without_real_content_detail() -> None:
    assert not has_publishable_content_detail("WTI油价维持高位", "Google News")
    assert not has_publishable_content_detail("市场新闻", "市场新闻")
    assert not has_publishable_content_detail("市场新闻", "短摘要")
    assert not has_publishable_content_detail(
        "随着BTC价格保持稳定，国债收益率飙升，FOMC利率会议纪要成为焦点：Crypto Daily – CoinDesk",
        "随着BTC价格保持稳定，国债收益率飙升，FOMC利率会议纪要成为焦点：Crypto Daily &nbsp;&nbsp; CoinDesk。",
    )
    assert not has_publishable_content_detail(
        "中东航司利润预期骤降115亿美元，从盈利72亿到亏损43亿，地缘政治冲击全球中转模式 – 虎嗅",
        "中东航司利润预期骤降115亿美元，从盈利72亿到亏损43亿，地缘政治冲击全球中转模式 &nbsp;&nbsp; 虎嗅。",
    )


def test_keeps_news_with_substantive_detail() -> None:
    detail = (
        "报道披露了事件发生的时间、涉及主体和主要过程，并说明相关市场价格变化、"
        "机构回应及后续安排。目前部分数据仍有待官方进一步确认，原始来源提供了完整说明。"
    )
    assert has_publishable_content_detail("市场新闻", detail)


def test_keeps_title_card_when_content_detail_is_unavailable() -> None:
    entry = SimpleNamespace(
        id=1,
        module_code="D",
        title="市场新闻标题 – 示例媒体",
        summary="市场新闻标题 &nbsp;&nbsp; 示例媒体",
        impact_analysis="",
        related_company=None,
        risk_category="市场风险",
        category_tag=None,
        risk_level="低",
        source_url="https://example.com/news",
        source_title="示例媒体",
        published_at=None,
    )

    card = build_display_cards(object(), [entry], social_resolver=lambda _: {})[0]

    assert card.title == entry.title
    assert card.overview == ""
    assert card.source_url == entry.source_url
