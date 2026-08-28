"""本机 CJK 字体选择：中文 Windows 优先宋体/雅黑，日文 Windows 回退到游ゴシック。"""

from __future__ import annotations

from functools import lru_cache

# matplotlib 内部名 -> Word eastAsia 名
WORD_FONT_MAP = (
    ("SimSun", "宋体"),
    ("Microsoft YaHei", "微软雅黑"),
    ("Yu Gothic", "Yu Gothic"),
    ("MS Gothic", "MS Gothic"),
)
MATPLOTLIB_FONT_CANDIDATES = (
    "Microsoft YaHei",
    "SimHei",
    "SimSun",
    "Yu Gothic",
    "MS Gothic",
    "Meiryo",
)


@lru_cache(maxsize=1)
def _installed_font_names() -> frozenset[str]:
    try:
        from matplotlib import font_manager
    except ImportError:
        return frozenset()
    return frozenset(item.name for item in font_manager.fontManager.ttflist)


@lru_cache(maxsize=1)
def word_east_asia_font() -> str:
    installed = _installed_font_names()
    for mpl_name, word_name in WORD_FONT_MAP:
        if mpl_name in installed:
            return word_name
    return "宋体"


@lru_cache(maxsize=1)
def matplotlib_font() -> str | None:
    installed = _installed_font_names()
    for name in MATPLOTLIB_FONT_CANDIDATES:
        if name in installed:
            return name
    return None
