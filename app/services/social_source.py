"""媒体/平台来源识别：按链接域名映射中文媒体名（含社媒），供日报卡片展示。"""

from __future__ import annotations

import json
import re
from typing import Any, Optional
from urllib.parse import urlparse

# 展示名 → 匹配域名（小写）；一律用 list，避免单元素 tuple 写成字符串
_MEDIA_SOURCES: list[tuple[str, list[str]]] = [
    # 国际通讯社 / 财经
    ("路透社", ["reuters.com", "reutersagency.com"]),
    ("彭博社", ["bloomberg.com", "bloom.bg"]),
    ("美联社", ["apnews.com", "ap.org"]),
    ("法新社", ["afp.com", "afpforum.com"]),
    ("华尔街日报", ["wsj.com", "dowjones.com"]),
    ("金融时报", ["ft.com"]),
    ("经济学人", ["economist.com"]),
    ("CNBC", ["cnbc.com"]),
    ("市场观察", ["marketwatch.com"]),
    ("雅虎财经", ["finance.yahoo.com", "yahoo.com"]),
    # 英美综合
    ("BBC", ["bbc.com", "bbc.co.uk", "bbci.co.uk"]),
    ("CNN", ["cnn.com"]),
    ("《卫报》", ["theguardian.com", "guardian.com"]),
    ("纽约时报", ["nytimes.com"]),
    ("华盛顿邮报", ["washingtonpost.com", "wapo.st"]),
    # 日本
    ("日本经济新闻", ["nikkei.com", "nikkei.co.jp"]),
    ("NHK", ["nhk.or.jp"]),
    ("共同社", ["kyodonews.net", "kyodo.co.jp"]),
    ("时事通讯社", ["jiji.com"]),
    ("读卖新闻", ["yomiuri.co.jp"]),
    ("朝日新闻", ["asahi.com"]),
    ("每日新闻", ["mainichi.jp"]),
    ("产经新闻", ["sankei.com"]),
    ("东京新闻", ["tokyo-np.co.jp"]),
    ("TBS", ["tbs.co.jp"]),
    ("富士新闻网", ["fnn.jp"]),
    ("PR TIMES", ["prtimes.jp"]),
    ("TDnet", ["tdnet.info", "release.tdnet.info"]),
    ("EDINET", ["edinet-fsa.go.jp", "disclosure.edinet-fsa.go.jp"]),
    ("日本取引所", ["jpx.co.jp"]),
    # 中文媒体 / 门户
    ("新华社", ["xinhuanet.com", "news.cn"]),
    ("央视新闻", ["cctv.com", "cntv.cn"]),
    ("人民网", ["people.com.cn"]),
    ("中国日报", ["chinadaily.com.cn"]),
    ("财新", ["caixin.com"]),
    ("第一财经", ["yicai.com", "cbnweek.com"]),
    ("证券时报", ["stcn.com"]),
    ("上海证券报", ["cnstock.com"]),
    ("界面新闻", ["jiemian.com"]),
    ("澎湃新闻", ["thepaper.cn"]),
    ("观察者网", ["guancha.cn"]),
    ("凤凰网", ["ifeng.com"]),
    ("新浪财经", ["finance.sina.com.cn", "sina.com.cn"]),
    ("网易新闻", ["163.com"]),
    ("腾讯新闻", ["qq.com", "new.qq.com"]),
    # 谷歌新闻聚合
    ("谷歌新闻", ["news.google.com"]),
    # 社媒
    ("X / Twitter", ["twitter.com", "x.com", "t.co"]),
    ("微博", ["weibo.com", "weibo.cn", "t.cn"]),
    ("Facebook", ["facebook.com", "fb.com", "fb.watch"]),
    ("LinkedIn", ["linkedin.com", "lnkd.in"]),
    ("YouTube", ["youtube.com", "youtu.be"]),
    ("Instagram", ["instagram.com"]),
    ("TikTok", ["tiktok.com", "douyin.com"]),
    ("Reddit", ["reddit.com"]),
    ("Telegram", ["t.me", "telegram.me"]),
    ("微信公众号", ["mp.weixin.qq.com"]),
    ("小红书", ["xiaohongshu.com", "xhslink.com"]),
    ("知乎", ["zhihu.com"]),
]

# 二级域名噪音，推导媒体名时剥离
_STRIP_LABELS = {
    "www",
    "m",
    "mobile",
    "news",
    "www2",
    "edition",
    "feeds",
    "rss",
    "cdn",
    "amp",
    "api",
    "release",
}


# 域名回退得到的 slug → 规范展示名
_BRAND_DISPLAY = {
    "nhk": "NHK",
    "bbc": "BBC",
    "cnn": "CNN",
    "cnbc": "CNBC",
    "tdnet": "TDnet",
    "edinet": "EDINET",
    "jpx": "日本取引所",
    "reuters": "路透社",
    "bloomberg": "彭博社",
    "nikkei": "日本经济新闻",
}


def _host(url: str) -> str:
    try:
        host = (urlparse(url).netloc or "").lower()
    except Exception:
        return ""
    if "@" in host:
        host = host.split("@", 1)[-1]
    if host.startswith("www."):
        host = host[4:]
    return host


def detect_media_from_url(url: Optional[str]) -> tuple[str, Optional[str]]:
    """返回 (媒体展示名, url)；无法识别时尽量用域名主段，仍失败则 暂无。"""
    u = (url or "").strip()
    if not u.startswith("http"):
        return "暂无", None
    host = _host(u)
    if not host:
        return "暂无", None

    # 精确 / 后缀匹配（优先更长域名）
    best: Optional[tuple[int, str]] = None
    for label, domains in _MEDIA_SOURCES:
        for d in domains:
            if host == d or host.endswith("." + d):
                score = len(d)
                if best is None or score > best[0]:
                    best = (score, label)
    if best:
        return best[1], u

    # 回退：从域名推导可读名（如 toyokeizai.net → toyokeizai）
    derived = _derive_label_from_host(host)
    if derived:
        return derived, u
    return "暂无", None


def _derive_label_from_host(host: str) -> str:
    parts = [p for p in host.split(".") if p and p not in _STRIP_LABELS]
    if not parts:
        return ""
    # 去掉常见公共后缀
    public_suffixes = {
        "com",
        "cn",
        "jp",
        "co",
        "or",
        "ne",
        "ac",
        "go",
        "org",
        "net",
        "info",
        "gov",
        "edu",
        "uk",
        "au",
        "sg",
        "hk",
        "tw",
    }
    while len(parts) > 1 and parts[-1] in public_suffixes:
        parts.pop()
    if not parts:
        return ""
    slug = parts[-1] if len(parts) == 1 else parts[-2] if parts[-1] in public_suffixes else parts[-1]
    # 再剥一层 jp 公司二级
    if slug in {"co", "or", "ne", "ac", "go"} and len(parts) >= 2:
        slug = parts[-2]
    if len(slug) < 2:
        return ""
    return _BRAND_DISPLAY.get(slug.lower(), slug)


def _parse_structured_media(structured_json: Optional[str]) -> tuple[str, Optional[str]]:
    if not structured_json:
        return "暂无", None
    try:
        data = json.loads(structured_json)
    except (json.JSONDecodeError, TypeError):
        return "暂无", None
    if not isinstance(data, dict):
        return "暂无", None
    raw = str(
        data.get("来源名称")
        or data.get("信息来源")
        or data.get("社媒来源")
        or data.get("social_source")
        or data.get("source_name")
        or ""
    ).strip()
    if not raw or raw in ("无", "暂无", "N/A", "-", "——"):
        return "暂无", None
    urls = re.findall(r"https?://[^\s）)】]+", raw)
    if urls:
        label, link = detect_media_from_url(urls[0])
        if label != "暂无":
            return label, link
        name = raw.split("http")[0].strip(" ：:")
        return (name[:40] if name else "暂无"), urls[0]
    return raw[:40], None


def resolve_social_source(
    *,
    source_url: Optional[str] = None,
    structured_json: Optional[str] = None,
    social_field: Optional[str] = None,
    source_domain: Optional[str] = None,
) -> dict[str, Any]:
    """
    展示策略：
    1) 结构化「来源名称 / 社媒来源」字段；
    2) 主链接域名 → 媒体中文名；
    3) 采集时的 source_domain；
    4) 域名主段回退；否则「暂无」。
    """
    if social_field and str(social_field).strip():
        label, link = _parse_structured_media(
            json.dumps({"来源名称": social_field}, ensure_ascii=False)
        )
        if label != "暂无":
            return {"social_source_label": label, "social_source_url": link}

    label, link = _parse_structured_media(structured_json)
    if label != "暂无":
        return {"social_source_label": label, "social_source_url": link}

    label, link = detect_media_from_url(source_url)
    if label != "暂无":
        return {"social_source_label": label, "social_source_url": link}

    if source_domain:
        fake = f"https://{source_domain.strip().lstrip('/')}/"
        label, _ = detect_media_from_url(fake)
        if label != "暂无":
            return {"social_source_label": label, "social_source_url": source_url}

    return {"social_source_label": "暂无", "social_source_url": None}


# 兼容旧导入名
detect_social_from_url = detect_media_from_url
