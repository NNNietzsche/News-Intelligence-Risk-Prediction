"""应用配置与业务模块 taxonomy。"""

from datetime import date
from functools import lru_cache
from typing import Any, Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Modified by DingJiaye: 2026-08-26 — 用于浏览器标题与 API 服务标识。
    app_name: str = "信息系统与风险预测"
    database_url: str = "sqlite:///./data/risk_intel.db"
    secret_key: str = "dev-secret-key"

    mita_api_base_url: str = "https://metaso.cn/api/v1"
    mita_api_key: str = ""

    deepseek_api_base_url: str = "https://api.deepseek.com"
    deepseek_api_key: str = ""
    deepseek_model: str = "deepseek-chat"

    # 界面二（主体评估）结构化分析
    gemini_api_base_url: str = "https://generativelanguage.googleapis.com"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.0-flash"
    # 同一 GEMINI_API_KEY 可换模型。空值回退到 GEMINI_MODEL。
    gemini_fast_model: str = ""
    gemini_flash_model: str = ""
    gemini_strong_model: str = ""

    # Existing deployments keep the original report path until explicitly switched.
    industry_report_generation_mode: Literal["legacy", "grounded"] = "legacy"
    # Modified by DingJiaye: 2026-08-31 — 取消固定演示稿；行业报告统一基于用户勾选
    # 的材料和常规 AI 分析流程生成。
    industry_demo_fixed_reports: bool = False
    grounded_report_require_approval: bool = True
    # Automatic grounded -> legacy fallback is intentionally unsupported.
    grounded_report_allow_legacy_fallback: Literal[False] = False

    @field_validator("grounded_report_allow_legacy_fallback", mode="before")
    @classmethod
    def _coerce_legacy_fallback_off(cls, value: Any) -> Any:
        # .env 写入的是字符串 "false"；Literal[False] 不会自动转换。
        if isinstance(value, str) and value.strip().lower() in {"0", "false", "no", "off"}:
            return False
        return value

    daily_pipeline_cron: str = "0 6 * * *"
    # 国际评级与市场信号：默认东京时间每日 06:30 刷新。
    intl_ratings_refresh_cron: str = "30 6 * * *"
    # Modified by DingJiaye: 2026-08-27 — 交互式情报检索优先快速返回；慢站点不应长期占用任务。
    request_timeout_seconds: int = 45
    news_window_hours: int = 24
    # 内容摘要必须基于新闻详情页正文，不以标题/RSS 片段扩写。
    news_fetch_body: bool = True
    news_max_body_items: int = 8
    # 页面只读取已存资讯；为空时由用户手动刷新，避免每次重新打开页面都触发采集。
    news_auto_backfill_on_empty: bool = False
    # Modified by DingJiaye: 2026-08-27 — 单次快速重试，避免外网异常造成数分钟等待。
    network_retry_attempts: int = 1
    network_retry_backoff_seconds: float = 0.35
    # 流水线韧性：先粗筛再 LLM、缓存、降级入库、异步默认开启
    pipeline_async_default: bool = True
    # LLM 单批处理条数（不是最终展示上限；展示不设上限）
    pipeline_llm_top_k: int = 12
    pipeline_llm_cache_hours: int = 168
    # 同一模块内 LLM 分批并发数；1 = 串行。建议 2～3，避免限流
    pipeline_llm_concurrency: int = 3
    pipeline_mita_query_pause_seconds: float = 0.3
    # 秘塔「不足才补」目标条数。0 = 默认 24（与展示全量策略对齐，不再跟 LLM 批大小挂钩）
    pipeline_mita_min_items: int = 0
    # 主源整类失败（RSS/TDnet 硬故障）时强制跑秘塔补缺
    pipeline_mita_force_on_primary_fail: bool = True
    # 新闻检索保持以秘塔 API 与配置的可核验 RSS/官网来源为主，不切换到第三方搜索补缺。
    search_ddg_fallback_enabled: bool = False
    search_ddg_region: str = "jp-jp"
    # 新闻检索只使用秘塔 API；关闭后秘塔异常不会改由 Gemini/DeepSeek 搜索。
    search_llm_fallback_enabled: bool = False
    # Modified by DingJiaye: 2026-08-27 — 风险情报智能体先规划检索，再由可信来源抓取正文。
    intelligence_agent_enabled: bool = True
    intelligence_agent_plan_cache_hours: int = 12
    # Modified by DingJiaye: 2026-08-27 — 新闻板块的相关性、主体归属与展示取舍
    # 由情报智能体完成；静态规则仅在模型不可用时兜底，避免关键词误筛掉整块资讯。
    intelligence_agent_ai_screening_enabled: bool = True
    # Modified by DingJiaye: 2026-08-27 — 主题评估沿用同一 AI 相关性审核策略。
    intelligence_agent_entity_screening_enabled: bool = True
    # 已开 WAL；界面一 B/C/D 可并行。侧栏明显变卡时改回 false
    pipeline_module_parallel: bool = True
    pipeline_merge_llm: bool = True
    # 单模块内 RSS/Google 并发拉取数；1 = 串行
    pipeline_rss_fetch_workers: int = 6
    # 同模块主源通道（RSS/直连/新浪/TDnet 等）并行；1=串行
    pipeline_channel_parallel: bool = True
    # 单模块主源采集上限（RSS/直连/新浪/TDnet）；0 或负数按 80
    pipeline_collect_max_items: int = 80
    rss_config_path: str = "config/rss_feeds.yaml"
    direct_sites_config_path: str = "config/direct_sites.yaml"
    entity_targets_config_path: str = "config/entity_targets.yaml"
    # 演示数据必须显式开启；正常运行不再因空结果写入 Mock。
    entity_demo_mode: bool = False
    # 舆情预警、最新消息与公开信息事件的观察期（天）。
    entity_warning_lookback_days: int = 90

    # 外部新闻通知。未填写任何渠道时只保留站内页面，不阻断采集。
    # Modified by DingJiaye: 2026-08-27 — 推送范围由页面“预警设置”保存。
    alert_email_to: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_use_starttls: bool = True
    wecom_webhook_url: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()


def _nonempty_model(value: str | None, fallback: str) -> str:
    text = (value or "").strip()
    return text or fallback


def resolve_gemini_model(task: str = "fast", settings: Settings | None = None) -> str:
    """按任务选择 Gemini 模型名；未配置时回退 GEMINI_MODEL。"""
    s = settings or get_settings()
    default = _nonempty_model(s.gemini_model, "gemini-3.1-flash-lite")
    name = (task or "fast").strip().lower()
    if name in {"fast", "briefing"}:
        return _nonempty_model(s.gemini_fast_model, default)
    if name in {"flash", "finance", "evidence", "search"}:
        return _nonempty_model(s.gemini_flash_model, default)
    if name in {"strong", "entity", "industry", "grounded"}:
        return _nonempty_model(s.gemini_strong_model, default)
    return default


# ---------------------------------------------------------------------------
# 业务模块与监控 taxonomy（中文）
# ---------------------------------------------------------------------------

MODULE_CODES = {
    "A": "企业与品牌风险",
    "B": "中东日报",
    "C": "大型企业",
    "D": "宏观市场",
    "F": "交通银行监测",
    "E": "授信报告与行业分析",
}

# 新闻日报时间窗（小时）
NEWS_WINDOW_HOURS_24 = 24
NEWS_WINDOW_HOURS_7X24 = 168  # 7×24


def news_window_label(hours: int) -> str:
    """界面/导出用时间窗文案。"""
    h = int(hours or NEWS_WINDOW_HOURS_24)
    if h >= NEWS_WINDOW_HOURS_7X24:
        return "7×24小时"
    return f"{h}小时"


# 三级页面与模块映射（深度研报走 IndustryAnalysis*，不占用 DailyRiskEntry）
PAGE_MODULES = {
    "daily_news": ("F", "B", "C", "D"),
    "news_7x24": ("F", "B", "C", "D"),
    "entity_assessment": ("A",),
}

PAGE_META = {
    "daily_news": {
        "path": "/daily-news",
        "title": "新闻汇总",
        "subtitle": "近24小时重要资讯 · 区域 · 机构 · 宏观",
        "window_hours": NEWS_WINDOW_HOURS_24,
        "collect_label": "采集近24小时资讯",
        "empty_hint": "当前筛选条件下暂无条目。可通过侧边栏运行流水线采集近 24 小时资讯。",
    },
    "news_7x24": {
        "path": "/daily-news-7x24",
        "title": "新闻汇总 · 7×24",
        "subtitle": "近七日按日快照 · 区域 · 机构 · 宏观",
        "window_hours": NEWS_WINDOW_HOURS_7X24,
        "collect_label": "采集近7天资讯",
        "empty_hint": "该日暂无快照。将自动补采；也可点击侧边栏手动采集。",
    },
    "entity_assessment": {
        "path": "/entity-assessment",
        "title": "主体评估",
        "subtitle": "近三个月公开信息与舆情预警",
    },
    "deep_reports": {
        "path": "/deep-reports",
        "title": "行业分析",
        "subtitle": "行业与授信长篇结构化分析",
    },
    "intl_ratings": {
        "path": "/intl-ratings",
        "title": "国际评级",
        "subtitle": "发行体国际信用评级与监测",
    },
}


def modules_for_page(page_key: str) -> dict[str, str]:
    """返回某页面允许展示的模块 code→名称 子集。"""
    codes = PAGE_MODULES.get(page_key, ())
    return {c: MODULE_CODES[c] for c in codes if c in MODULE_CODES}

MODULE_A_TARGETS = [
    "Godiva",
    "普洛斯",
    "三菱商事",
    "三井物産",
    "伊藤忠商事",
    "住友商事",
    "丸紅",
    "デンソー",
    "日本郵船",
    "三菱UFJ",
    "三井住友FG",
    "みずほ",
    "野村",
    "大和証券",
    "SMBC日興",
]
MODULE_A_CATEGORIES = [
    "司法与行政监管",
    "金融与经营数据",
    "公开舆论与社交媒体",
    "供应链与关联方",
]

MODULE_B_REGION_HINT = (
    "中东区域全量动态跟踪：严格限定中东地理范围（沙特、阿联酋、伊朗、以色列、卡塔尔、科威特等）；"
    "收录国家政策、官方声明、地缘政治与区域市场；有明确新信息即收，不要求必须是高风险。"
    "与宏观板块交叉时：常规外交/局部冲突/例行声明归本板块；"
    "若同时引发全球资产剧烈波动，可双投至每日宏观与市场情报。"
)

MODULE_C_COMPANIES = [
    ("三菱商事", "Mitsubishi Corporation"),
    ("三井物産", "Mitsui & Co"),
    ("伊藤忠商事", "Itochu"),
    ("住友商事", "Sumitomo Corporation"),
    ("丸紅", "Marubeni"),
    ("デンソー", "Denso"),
    ("日本郵船", "NYK Line"),
    ("大和証券", "Daiwa Securities"),
    ("三菱UFJ", "Mitsubishi UFJ Financial Group"),
    ("三井住友FG", "Sumitomo Mitsui Financial Group"),
    ("みずほ", "Mizuho Financial Group"),
    ("野村", "Nomura Holdings"),
    ("SMBC日興", "SMBC Nikko Securities"),
]
MODULE_C_TARGETS = [
    name
    for pair in MODULE_C_COMPANIES
    for name in pair
]

MODULE_C_PILLARS = [
    "核心业务与市场周期",
    "运营与供应链",
    "强合规、监管处分与法律纠纷",
    "转型与搁浅资产",
    "集团治理与内部风险",
]

MODULE_D_TOPICS = [
    "原油",
    "LNG",
    "天然气",
    "贵金属",
    "黄金白银",
    "股市资本市场异动",
    "美股 日股",
    "日本央行货币政策",
    "美联储货币政策",
    "美元日元汇率",
    "重大地缘事件",
]

# 新闻日报三板块路由说明（供提示词 / 文档）
DAILY_NEWS_ROUTE_HINT = (
    "分类路由优先级："
    "1) 主语为日本监控九企之一→C；"
    "2) 大宗商品价格/供需/通胀→D，仅中东产油国本土政策声明且未涉全球价格→B；"
    "3) 中东常规动态→B；全球（含中东）重大地缘且引发资产强反应→D；可双投[B,D]。"
)

MODULE_E_TEMPLATES = [
    "授信报告模板",
    "航空业风险分析",
    "行业信用基准分析",
]

RISK_LEVELS = ["低", "中", "高", "极高"]

# 兼容现有数据库字段的公开信息预警灯号。
CREDIT_LEVELS = ["正常", "关注", "预警", "高风险"]
CREDIT_LEVEL_ORDER = {"正常": 1, "关注": 2, "预警": 3, "高风险": 4}

# 仅保留给旧调用兼容；新主体规则使用独立的 credit_impact 字段。
RISK_TO_CREDIT = {
    "低": "正常",
    "中": "关注",
    "高": "预警",
    "极高": "高风险",
}

# 默认监控主体与主体专属信源统一维护在 config/entity_targets.yaml。

STRUCTURED_FIELDS_CN = [
    "标题",
    "关联企业",
    "风险类别",
    "风险等级",
    "核心摘要",
    "影响分析",
    "来源链接",
    "来源名称",
    "发布时间",
    # 主体评估专用；其它模块可留空。
    "资讯重要度",
    "影响方向",
    "信用风险信号",
    "主体相关性",
    "置信度",
]


def module_search_queries(
    module: str,
    report_date: str,
    *,
    entity_targets: list[str] | None = None,
    window_hours: int = NEWS_WINDOW_HOURS_24,
    calendar_day: date | None = None,
) -> list[dict[str, Any]]:
    """为各模块生成近 N 小时资讯检索查询。

    entity_targets: 主体评估（模块 A）可限定只搜指定主体名称列表。
    window_hours: 时效窗口；168 时按近一周检索。
    calendar_day: 7×24 历史日补采时，用软日期提示（Google 另附 after/before）。
    """
    queries: list[dict[str, Any]] = []
    # 避免把具体日期塞进检索词（易导致秘塔“未找到相关数据”）；历史日除外
    if calendar_day is not None:
        d = calendar_day
        recency = (
            f"{d.isoformat()} OR {d.year}年{d.month}月{d.day}日 "
            f"OR {d.strftime('%Y/%m/%d')} OR {d.strftime('%Y-%m-%d')}"
        )
    elif int(window_hours or NEWS_WINDOW_HOURS_24) >= NEWS_WINDOW_HOURS_7X24:
        recency = "最新 过去一周 OR past week OR 近7天 OR 7 days OR 速报"
    else:
        recency = "最新 过去24小时 OR today OR 速报"
    if module == "A":
        targets = entity_targets or MODULE_A_TARGETS
        for target in targets:
            for cat in MODULE_A_CATEGORIES:
                queries.append(
                    {
                        "module": "A",
                        "query": f"{target} {cat} 新闻 动态 {recency}",
                        "metadata": {"target": target, "category": cat},
                    }
                )
    elif module == "B":
        for query in (
            "中东 新闻 政策 官方声明 地缘政治",
            "中东 能源 石油 LNG 航运 港口 制裁",
            "Middle East news geopolitics official statement",
            "Middle East oil LNG shipping ports sanctions market",
        ):
            queries.append(
                {
                    "module": "B",
                    "query": f"{query} {recency}",
                    "metadata": {"region": "中东"},
                }
            )
    elif module == "C":
        # 日语正式社名 + 適時開示/IR，减少中文转载噪音
        for jp_name, en_name in MODULE_C_COMPANIES:
            queries.append(
                {
                    "module": "C",
                    "query": (
                        f"{jp_name} OR {en_name} "
                        f"適時開示 OR ニュースリリース OR IR OR 決算 {recency}"
                    ),
                    "metadata": {"company": jp_name},
                }
            )
    elif module == "D":
        for topic in MODULE_D_TOPICS:
            queries.append(
                {
                    "module": "D",
                    "query": f"{topic} 市场 新闻 {recency}",
                    "metadata": {"topic": topic},
                }
            )
    elif module == "E":
        queries.append(
            {
                "module": "E",
                "query": f"行业风险 授信 分析 {recency}",
                "metadata": {"topic": "industry"},
            }
        )
        queries.append(
            {
                "module": "E",
                "query": f"授信 行业信用 航空业 风险分析 {recency}",
                "metadata": {"template": "行业分析"},
            }
        )
    return queries
