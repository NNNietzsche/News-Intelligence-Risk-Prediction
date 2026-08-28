"""新闻日报三板块（B/C/D）自动化分类与路由。

规则口径与产品定义一致：企业优先 → 大宗归集 → 地缘门槛 → 双投 [B,D]。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Optional

# ---------------------------------------------------------------------------
# 板块收录常量
# ---------------------------------------------------------------------------

SECTION_B = "B"
SECTION_C = "C"
SECTION_D = "D"
NEWS_SECTIONS = (SECTION_B, SECTION_C, SECTION_D)

# 监控企业（含别名；匹配时大小写不敏感）
MONITOR_COMPANIES: tuple[tuple[str, ...], ...] = (
    # 五大商社
    ("三菱商事", "三菱商事株式会社", "mitsubishi corporation", "mitsubishi corp"),
    ("三井物产", "三井物産", "三井物産株式会社", "mitsui & co", "mitsui & co.", "mitsui and co"),
    ("伊藤忠商事", "伊藤忠商事株式会社", "itochu", "伊藤忠"),
    ("住友商事", "住友商事株式会社", "sumitomo corporation", "sumitomo corp"),
    ("丸红", "丸紅", "丸紅株式会社", "marubeni"),
    # 既有重点企业
    ("デンソー", "denso", "电装"),
    ("日本邮船", "日本郵船", "nyk line", "nyk"),
    # 三大银行（金融集团）
    ("三菱UFJ", "三菱ufj", "mufg", "mitsubishi ufj financial group", "三菱ufjフィナンシャル・グループ", "三菱ufj銀行"),
    ("三井住友FG", "三井住友fg", "smfg", "sumitomo mitsui financial group", "三井住友フィナンシャルグループ", "三井住友銀行"),
    ("瑞穗金融", "瑞穂", "みずほ", "mizuho", "mizuho financial group", "みずほフィナンシャルグループ", "みずほ銀行"),
    # 三大券商
    ("野村控股", "野村", "nomura", "nomura holdings", "野村ホールディングス", "野村證券", "野村证券"),
    ("大和证券", "大和証券", "大和証券グループ", "daiwa securities", "daiwa securities group", "daiwa"),
    ("SMBC日兴证券", "smbc日興", "smbc nikko", "smbc日興証券", "smbc日兴证券"),
)

MIDDLE_EAST_GEO: tuple[str, ...] = (
    "中东",
    "中東",
    "middle east",
    "海湾",
    "海灣",
    "波斯湾",
    "霍尔木兹",
    "霍爾木茲",
    "hormuz",
    "红海",
    "紅海",
    "red sea",
    "加沙",
    "gaza",
    "西岸",
    "巴勒斯坦",
    "palestine",
    "以色列",
    "israel",
    "伊朗",
    "iran",
    "沙特",
    "saudi",
    "阿联酋",
    "阿聯酋",
    "uae",
    "迪拜",
    "dubai",
    "阿布扎比",
    "卡塔尔",
    "卡達",
    "qatar",
    "科威特",
    "kuwait",
    "阿曼",
    "oman",
    "巴林",
    "bahrain",
    "伊拉克",
    "iraq",
    "叙利亚",
    "敘利亞",
    "syria",
    "黎巴嫩",
    "lebanon",
    "也门",
    "也門",
    "yemen",
    "约旦",
    "約旦",
    "jordan",
    "埃及",
    "egypt",
    "土耳其",
    "turkey",
    "türkiye",
    "houthi",
    "胡塞",
    "哈马斯",
    "哈馬斯",
    "hamas",
    "真主党",
    "真主黨",
    "hezbollah",
    "沙特阿美",
    "saudi aramco",
    "aramco",
)

COMMODITY_MARKET: tuple[str, ...] = (
    "原油",
    "crude",
    "wti",
    "brent",
    "布伦特",
    "布倫特",
    "油价",
    "油價",
    "lng",
    "液化天然气",
    "液化天然氣",
    "天然气价格",
    "天然氣價格",
    "贵金属",
    "貴金屬",
    "黄金",
    "黃金",
    "gold",
    "白银",
    "白銀",
    "silver",
    "铂金",
    "鉑金",
    "platinum",
    "工业金属",
    "工業金屬",
    "铜价",
    "銅價",
    "copper",
    "供需",
    "库存报告",
    "庫存報告",
    "eia",
    "opec",
    "欧佩克",
    "歐佩克",
)

MACRO_MARKET: tuple[str, ...] = (
    "美联储",
    "美聯儲",
    "federal reserve",
    "fed ",
    "fomc",
    "日本央行",
    "日银",
    "日銀",
    "bank of japan",
    "boj",
    "加息",
    "降息",
    "利率决议",
    "利率決議",
    "股市",
    "股指",
    "资本市场",
    "資本市場",
    "stock market",
    "nasdaq",
    "nikkei",
    "日经",
    "日經",
    "通胀",
    "通脹",
    "inflation",
)

# 日报只保留财经/企业经营相关内容。C 板块中的已监控主体采用例外：
# 来自官网、交易所或可信财经媒体的公告、IR、业务与人事信息也可保留，
# 以免因标题未出现中文财务关键词而漏掉正式披露。
FINANCIAL_RELEVANCE: tuple[str, ...] = (
    *COMMODITY_MARKET,
    *MACRO_MARKET,
    "汇率", "外汇", "美元", "日元", "人民币", "债券", "国债", "收益率",
    "融资", "贷款", "信贷", "债务", "再融资", "利息", "流动性", "违约",
    "业绩", "营收", "收入", "利润", "亏损", "现金流", "财报", "财务",
    "股价", "股票", "上市", "分红", "回购", "估值", "市值",
    "并购", "收购", "出售", "投资", "资本开支", "投资项目", "重大项目", "订单", "合同",
    "监管", "处罚", "罚款", "诉讼", "合规", "制裁", "关税", "出口管制",
    "供应链", "供应商", "原料", "库存", "交付", "物流", "运输", "港口",
    "工厂", "停产", "召回", "裁员", "产能", "生产成本", "融资成本", "市场需求",
    "exchange rate", "currency", "bond", "yield", "debt", "financing", "earnings",
    "revenue", "profit", "cash flow", "stock", "shares", "merger", "acquisition",
    "investment", "tariff", "sanction", "supply chain", "commodity", "oil", "gold",
    "regulation", "lawsuit", "recall", "production", "shipment",
)

# C 板块采用双层信源：企业官网/交易所为一手信源；指定财经媒体为
# 可复核的二级信源。聚合站、营销稿、财经自媒体均不入选。
JAPAN_ENTERPRISE_PRIMARY_DOMAINS: tuple[str, ...] = (
    "mitsubishicorp.com", "mitsui.com", "itochu.co.jp", "sumitomocorp.com",
    "marubeni.com", "denso.com", "nyk.com", "daiwa-grp.jp", "daiwa.jp",
    "mufg.jp", "smfg.co.jp", "mizuho-fg.co.jp", "nomuraholdings.com",
    "nomura.com", "smbcnikko.co.jp", "tdnet.info", "jpx.co.jp",
    "disclosure.edinet-fsa.go.jp",
)

JAPAN_ENTERPRISE_TRUSTED_MEDIA: tuple[str, ...] = (
    "日本経済新聞", "nikkei", "nhk", "nhkニュース", "nhk news",
    "reuters", "路透", "ロイター", "bloomberg", "彭博", "ブルームバーグ",
    "共同通信", "kyodo", "時事通信", "jiji", "日刊工業新聞",
)

# 全球重大地缘（非中东专属亦可）
GLOBAL_GEOPOLITICS: tuple[str, ...] = (
    "俄乌",
    "俄烏",
    "乌克兰",
    "烏克蘭",
    "ukraine",
    "俄国",
    "俄羅斯",
    "russia",
    "台海",
    "台湾海峡",
    "台灣海峽",
    "中美",
    "贸易战",
    "貿易戰",
    "朝核",
    "朝鲜",
    "北韓",
    "north korea",
    "南海",
    "制裁",
    "sanctions",
    "军事冲突",
    "軍事衝突",
    "战争",
    "戰爭",
    "袭击",
    "襲擊",
    "封锁",
    "封鎖",
)

# 「重大」市场冲击信号（双投 / 宏观高门槛）
MAJOR_SHOCK_SIGNALS: tuple[str, ...] = (
    "暴涨",
    "暴跌",
    "大涨",
    "大跌",
    "飙升",
    "飆升",
    "骤降",
    "驟降",
    "剧烈波动",
    "劇烈波動",
    "大幅震荡",
    "大幅震盪",
    "闪崩",
    "避险情绪",
    "避險情緒",
    "风险偏好",
    "風險偏好",
    "全球市场",
    "全球資產",
    "全球资产",
    "油价大涨",
    "油價大漲",
    "金价大涨",
    "金價大漲",
    ">3%",
    "超3%",
    "升逾",
    "跌逾",
    "创年内",
    "創年内",
    "历史高位",
    "歷史高位",
)

LOCAL_ME_POLICY: tuple[str, ...] = (
    "官方声明",
    "官方聲明",
    "政策",
    "内阁",
    "內閣",
    "议会",
    "議會",
    "外交",
    "会谈",
    "會談",
    "访问",
    "訪問",
    "例行",
    "任命",
    "本土",
    "国内",
    "國內",
)

ROUTING_PROMPT = """你是风险与新闻情报系统的自动化分类与路由引擎。
根据标题、正文与来源，将条目归入板块 B/C/D（可多选，仅允许双投 B+D）。

板块定义：
- B 中东日报：严格中东区域全量动态（政策/声明/地缘/区域市场），低门槛。
- C 日本方面内容：以监控企业（三菱商事、三井物产、伊藤忠、住友商事、丸红、デンソー、日本邮船、大和证券）及其核心业务关联方为主；内容为 IR/开示/经营/监管等财经事项。
- D 每日宏观与市场情报：全球宏观、大宗（原油/LNG/贵金属等）、美联储/日银/股市，以及可能剧烈冲击金融市场的重大地缘。

交叉规则（按优先级）：
1. 主语为监控企业 → 优先 C。
2. 大宗商品价格/供需/通胀影响 → D；仅中东产油国本土政策声明且未涉全球价格 → B。
3. 中东常规外交/局部冲突/例行声明 → B；全球（含中东）重大地缘且引发全球资产强反应 → D；同时满足则可 [B,D]。

只输出 JSON：{"sections":["B"],"reason":"简短理由"}"""


# 新闻日报范围外：娱乐/体育/科普趣味/奇闻等（命中即丢弃）
EXCLUDED_TOPICS: tuple[str, ...] = (
    "娱乐",
    "综艺",
    "明星",
    "八卦",
    "绯闻",
    "celebrity",
    "hollywood",
    "体育",
    "足球",
    "篮球",
    "网球",
    "奥运",
    "世界杯",
    "fifa",
    "英超",
    "nba",
    "mlb",
    "彩票",
    "乐透",
    "lottery",
    "中奖",
    "时尚",
    "美食",
    "旅游",
    "生活方式",
    "lifestyle",
    "科普",
    "趣味",
    "奇闻",
    "科学家感到兴奋",
    "感到兴奋",
    "为什么一些科学家",
    "撞上月球",
    "撞月",
    "thrilled",
    "scientists are",
    "collides with the moon",
    "rocket hitting the moon",
    "lottery ticket",
    "lottery",
    "garbage worker",
    "human interest",
    "人类趣闻",
    "暖心",
    "萌宠",
    "电影",
    "电视剧",
    "演唱会",
    "选秀",
)

# 非中东南亚/其它易误入 B 的地理词（无中东词时加强排除）
NON_ME_REGION_HINTS: tuple[str, ...] = (
    "印度",
    "india",
    "莫迪",
    "modi",
    "孟加拉",
    "bangladesh",
    "哈西娜",
    "hasina",
    "巴基斯坦",
    "pakistan",
    "斯里兰卡",
    "阿富汗",
    "afghanistan",
    "意大利",
    "italy",
    "罗马",
    "法国",
    "巴黎",
    "德国",
    "柏林",
    "英国",
    "伦敦",
    "日本",
    "东京",
    "韩国",
    "首尔",
    "巴西",
    "阿根廷",
    "澳大利亚",
    "加拿大",
)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip().lower()


def _contains_any(text: str, keywords: Iterable[str]) -> bool:
    return any(k.lower() in text for k in keywords if k)


def is_excluded_news_topic(*, title: str = "", content: str = "") -> bool:
    """是否属于新闻日报范围外主题（娱乐/体育/科普趣味等）。"""
    blob = _norm(f"{title} {content}")
    if not blob:
        return False
    return _contains_any(blob, EXCLUDED_TOPICS)


def is_financial_news_item(*, title: str = "", content: str = "") -> bool:
    """日报三板块的财经实质白名单，排除只有地区/公司名的泛资讯。"""
    blob = _norm(f"{title} {content}")
    return bool(blob) and _contains_any(blob, FINANCIAL_RELEVANCE)


def is_japan_enterprise_trusted_source(source: str) -> bool:
    """C 板块只展示一手披露或指定可信财经媒体。"""
    text = _norm(source).lower()
    return bool(text) and (
        _contains_any(text, JAPAN_ENTERPRISE_PRIMARY_DOMAINS)
        or _contains_any(text, JAPAN_ENTERPRISE_TRUSTED_MEDIA)
    )


def item_in_module_scope(
    module_code: str,
    *,
    title: str = "",
    content: str = "",
    source: str = "",
    related_company: str = "",
) -> tuple[bool, str]:
    """采集/入库前的板块范围判断。返回 (是否保留, 原因)。"""
    code = (module_code or "").upper()
    if is_excluded_news_topic(title=title, content=content):
        return False, "排除：非新闻日报主题（娱乐/体育/科普趣味等）"
    if code not in NEWS_SECTIONS:
        return True, "非新闻三板块，跳过板块校验"
    company = _matched_company(_norm(f"{title} {content} {source}"), related_company)
    if code == SECTION_C:
        if not company:
            return False, "排除：未命中大型企业监控名单"
        if not is_japan_enterprise_trusted_source(source):
            return False, "排除：大型企业仅保留官网、交易所或可信财经媒体信源"
        if not is_financial_news_item(title=title, content=content):
            return True, f"可信信源命中监控企业「{company}」，保留企业公告/业务动态"
    elif not is_financial_news_item(title=title, content=content):
        return False, "排除：不含财经、企业经营或市场风险实质"
    result = route_news_sections(
        title=title,
        content=content,
        source=source,
        related_company=related_company,
    )
    if code in result.sections:
        return True, result.reason
    # B：无中东命中但含明显其它地区 → 明确剔除
    if code == SECTION_B:
        blob = _norm(f"{title} {content} {source} {related_company}")
        if _contains_any(blob, NON_ME_REGION_HINTS) and not _contains_any(
            blob, MIDDLE_EAST_GEO
        ):
            return False, "排除：非中东区域内容"
    if not result.sections:
        return False, f"排除：未命中{code}板块规则"
    return False, f"排除：路由至{','.join(result.sections)}，不属于{code}"


@dataclass(frozen=True)
class RouteResult:
    sections: tuple[str, ...]
    reason: str

    def includes(self, module_code: str) -> bool:
        return module_code.upper() in self.sections


def _matched_company(text: str, related: str = "") -> Optional[str]:
    blob = f"{text} {_norm(related)}"
    for aliases in MONITOR_COMPANIES:
        for alias in aliases:
            if alias.lower() in blob:
                return aliases[0]
    return None


def _has_pct_shock(text: str) -> bool:
    """油价/金价等出现明显百分比波动表述。"""
    if re.search(
        r"(油价|油價|原油|金价|金價|黄金|黃金|股市|股指|指数).{0,12}"
        r"(涨|跌|升|下滑|上涨|下跌).{0,6}\d+(\.\d+)?\s*%",
        text,
    ):
        return True
    if re.search(r"(涨|跌|升).{0,4}([3-9]|[1-9]\d)(\.\d+)?\s*%", text):
        return True
    return False


def route_news_sections(
    *,
    title: str = "",
    content: str = "",
    source: str = "",
    related_company: str = "",
) -> RouteResult:
    """按产品规则将一条新闻路由到 B/C/D。

    返回 sections 为有序去重后的板块代码；无法判断时默认空（由调用方决定是否保留原模块）。
    """
    if is_excluded_news_topic(title=title, content=content):
        return RouteResult(sections=(), reason="排除：非新闻日报主题")

    title_n = _norm(title)
    content_n = _norm(content)
    source_n = _norm(source)
    related_n = _norm(related_company)
    blob = f"{title_n} {content_n} {source_n} {related_n}"

    # 1) 企业优先
    company = _matched_company(blob, related_n)
    if company:
        return RouteResult(
            sections=(SECTION_C,),
            reason=f"主语/关联命中监控企业「{company}」，企业优先归入 C",
        )

    is_me = _contains_any(blob, MIDDLE_EAST_GEO)
    is_commodity = _contains_any(blob, COMMODITY_MARKET)
    is_macro = _contains_any(blob, MACRO_MARKET)
    is_global_geo = _contains_any(blob, GLOBAL_GEOPOLITICS)
    is_major_shock = _contains_any(blob, MAJOR_SHOCK_SIGNALS) or _has_pct_shock(blob)
    is_local_policy = _contains_any(blob, LOCAL_ME_POLICY)

    # 2) 大宗商品归集（中东地缘+剧烈价格冲击 → 双投）
    if is_commodity:
        if is_me and is_major_shock:
            return RouteResult(
                sections=(SECTION_B, SECTION_D),
                reason="中东地缘伴随大宗/资产剧烈波动，双投 B+D",
            )
        # 中东产油国本土政策、未体现全球价格/供需异动 → B
        if is_me and is_local_policy and not is_major_shock and not re.search(
            r"(价格|價格|price|涨|跌|供需|库存|庫存|opec|欧佩克)", blob
        ):
            return RouteResult(
                sections=(SECTION_B,),
                reason="中东产油国/本土政策声明，未涉及全球价格异动，归入 B",
            )
        return RouteResult(
            sections=(SECTION_D,),
            reason="大宗商品价格/供需/市场影响，归集 D",
        )

    if is_macro and not is_me:
        return RouteResult(
            sections=(SECTION_D,),
            reason="货币与资本市场宏观动态，归入 D",
        )

    # 3) 地缘事件门槛
    if is_me:
        if is_major_shock or (is_global_geo and is_macro):
            return RouteResult(
                sections=(SECTION_B, SECTION_D),
                reason="中东地缘达重大市场冲击门槛，双投 B+D",
            )
        return RouteResult(
            sections=(SECTION_B,),
            reason="中东区域常规动态（低门槛），归入 B",
        )

    if is_global_geo or (is_macro and is_major_shock):
        return RouteResult(
            sections=(SECTION_D,),
            reason="全球重大地缘或宏观冲击，归入 D",
        )

    if is_macro:
        return RouteResult(
            sections=(SECTION_D,),
            reason="宏观/市场相关，归入 D",
        )

    return RouteResult(sections=(), reason="未命中 B/C/D 明确规则")


def route_structured_row(row: dict[str, Any]) -> RouteResult:
    """适配流水线结构化行（中文字段）。"""
    return route_news_sections(
        title=str(row.get("标题") or row.get("title") or ""),
        content=str(
            row.get("核心摘要")
            or row.get("影响分析")
            or row.get("summary")
            or row.get("content")
            or ""
        ),
        source=str(
            row.get("来源名称")
            or row.get("source")
            or row.get("来源链接")
            or ""
        ),
        related_company=str(row.get("关联企业") or row.get("related_company") or ""),
    )


def filter_candidate_items(
    items: list[dict[str, Any]],
    module_code: str,
) -> tuple[list[dict[str, Any]], int]:
    """采集候选进入 LLM 前按板块范围预过滤。"""
    code = (module_code or "").upper()
    if code not in NEWS_SECTIONS:
        return items, 0
    kept: list[dict[str, Any]] = []
    dropped = 0
    for it in items:
        title = str(it.get("title") or "")
        content = str(it.get("snippet") or it.get("body") or "")
        ok, _reason = item_in_module_scope(
            code,
            title=title,
            content=content,
            # Google RSS 的链接域名是 news.google.com；用其标注的真实发布者
            # 才能正确执行 C 板块的可信媒体筛选。
            source=str(
                it.get("publisher") or it.get("source_domain") or it.get("feed") or ""
            ),
            related_company=str(it.get("company") or ""),
        )
        if ok:
            kept.append(it)
        else:
            dropped += 1
    return kept, dropped


def filter_rows_for_module(
    rows: list[dict[str, Any]],
    module_code: str,
    *,
    keep_unmatched: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """保留应归属当前模块的行；为每行写入 _route_sections / _route_reason。

    keep_unmatched 默认 False：未命中板块规则的条目一律剔除（避免串板块）。
    """
    code = module_code.upper()
    kept: list[dict[str, Any]] = []
    stats = {
        "routed_in": 0,
        "routed_out": 0,
        "unmatched_kept": 0,
        "excluded_topic": 0,
        "dual": 0,
    }

    for row in rows:
        title = str(row.get("标题") or row.get("title") or "")
        content = str(
            row.get("核心摘要")
            or row.get("影响分析")
            or row.get("summary")
            or ""
        )
        if is_excluded_news_topic(title=title, content=content):
            stats["excluded_topic"] += 1
            stats["routed_out"] += 1
            continue

        ok, reason = item_in_module_scope(
            code,
            title=title,
            content=content,
            source=str(row.get("来源名称") or row.get("来源链接") or ""),
            related_company=str(row.get("关联企业") or ""),
        )
        result = route_structured_row(row)
        enriched = dict(row)
        enriched["_route_sections"] = list(result.sections)
        enriched["_route_reason"] = reason or result.reason

        if not ok:
            stats["routed_out"] += 1
            continue

        if len(result.sections) > 1:
            stats["dual"] += 1
        stats["routed_in"] += 1
        kept.append(enriched)

    return kept, stats


def dual_mirror_targets(sections: Iterable[str], current_module: str) -> list[str]:
    """双投时需要镜像写入的其它板块。"""
    cur = current_module.upper()
    return [s for s in sections if s in NEWS_SECTIONS and s != cur]
