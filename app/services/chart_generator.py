"""从文本/结构化数据中提取图表规格并生成 ECharts 配置。"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

CHART_COLORS = ["#6b7c93", "#8a939f", "#a8956e", "#7a9a82", "#9aa3ad"]


def extract_chart_specs(text: str) -> list[dict[str, Any]]:
    """从文本中识别时间序列或对比数据，生成图表规格列表。"""
    specs: list[dict[str, Any]] = []

    ts = _extract_time_series(text)
    if ts:
        specs.append(ts)

    comp = _extract_comparison_table(text)
    if comp:
        specs.append(comp)

    return specs


def _extract_time_series(text: str) -> Optional[dict[str, Any]]:
    pattern = re.compile(
        r"(\d{4}[-/年]\d{1,2}(?:[-/月]\d{1,2}日?)?|\d{4}年|\d{1,2}月)\s*[:：,，]?\s*(-?\d+(?:\.\d+)?%?)"
    )
    matches = pattern.findall(text)
    if len(matches) < 3:
        return None
    labels: list[str] = []
    values: list[float] = []
    for label, val in matches[:24]:
        labels.append(label.strip())
        num = val.replace("%", "").replace(",", "")
        try:
            values.append(float(num))
        except ValueError:
            continue
    if len(labels) < 3:
        return None
    return {
        "id": "chart_timeseries",
        "type": "line",
        "title": "指标时间序列",
        "labels": labels,
        "series": [{"name": "数值", "data": values[: len(labels)]}],
    }


def _extract_comparison_table(text: str) -> Optional[dict[str, Any]]:
    rows = [ln.strip() for ln in text.splitlines() if "|" in ln]
    if len(rows) < 2:
        return None
    labels: list[str] = []
    values: list[float] = []
    for row in rows[:12]:
        cells = [c.strip() for c in row.split("|") if c.strip()]
        if len(cells) < 2:
            continue
        name = cells[0]
        num_match = re.search(r"-?\d+(?:\.\d+)?", cells[1].replace(",", ""))
        if not num_match:
            continue
        labels.append(name[:20])
        values.append(float(num_match.group()))
    if len(labels) < 2:
        return None
    return {
        "id": "chart_comparison",
        "type": "bar",
        "title": "对比指标",
        "labels": labels,
        "series": [{"name": "数值", "data": values}],
    }


def to_echarts_option(spec: dict[str, Any]) -> dict[str, Any]:
    chart_type = spec.get("type", "bar")
    labels = spec.get("labels", [])
    series_raw = spec.get("series", [])
    series = []
    for idx, s in enumerate(series_raw):
        series.append(
            {
                "name": s.get("name", f"系列{idx + 1}"),
                "type": chart_type if chart_type != "line" else "line",
                "data": s.get("data", []),
                "smooth": chart_type == "line",
                "itemStyle": {"color": CHART_COLORS[idx % len(CHART_COLORS)]},
            }
        )
    return {
        "title": {"text": spec.get("title", ""), "left": "center", "textStyle": {"color": "#4a5160", "fontSize": 14, "fontFamily": "Microsoft YaHei, 微软雅黑, sans-serif", "fontWeight": 400}},
        "tooltip": {"trigger": "axis" if chart_type == "line" else "item"},
        "legend": {"bottom": 0, "textStyle": {"color": "#7a828e", "fontFamily": "Microsoft YaHei, 微软雅黑, sans-serif", "fontWeight": 400}},
        "grid": {"left": "8%", "right": "4%", "bottom": "15%", "containLabel": True},
        "xAxis": {
            "type": "category",
            "data": labels,
            "axisLabel": {"color": "#7a828e", "rotate": 30 if len(labels) > 6 else 0, "fontFamily": "Microsoft YaHei, 微软雅黑, sans-serif", "fontWeight": 400},
        },
        "yAxis": {"type": "value", "axisLabel": {"color": "#7a828e", "fontFamily": "Microsoft YaHei, 微软雅黑, sans-serif", "fontWeight": 400}},
        "series": series,
        "color": CHART_COLORS,
    }


def specs_to_echarts_list(specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"id": s.get("id"), "option": to_echarts_option(s)} for s in specs]


def render_chart_png(spec: dict[str, Any], output_path: Path) -> Optional[Path]:
    """使用 matplotlib 生成静态图供 Word 导出。"""
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from app.services.cjk_fonts import matplotlib_font

        cjk_font = matplotlib_font()
        if cjk_font:
            plt.rcParams["font.sans-serif"] = [cjk_font]
            plt.rcParams["axes.unicode_minus"] = False
    except ImportError:
        logger.warning("matplotlib 未安装，跳过 Word 图表嵌入")
        return None

    labels = spec.get("labels", [])
    series = spec.get("series", [])
    if not labels or not series:
        return None

    fig, ax = plt.subplots(figsize=(7, 3.5))
    fig.patch.set_facecolor("#fafbfc")
    ax.set_facecolor("#fafbfc")
    ax.tick_params(colors="#7a828e")
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["bottom"].set_color("#e3e6ea")
    ax.spines["left"].set_color("#e3e6ea")

    chart_type = spec.get("type", "bar")
    data = series[0].get("data", [])
    color = CHART_COLORS[0]
    if chart_type == "line":
        ax.plot(labels, data, color=color, marker="o", linewidth=1.5)
    else:
        ax.bar(labels, data, color=color, alpha=0.85)

    ax.set_title(spec.get("title", ""), color="#4a5160", fontsize=11)
    plt.xticks(rotation=30 if len(labels) > 6 else 0, ha="right", fontsize=8)
    plt.tight_layout()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(str(output_path), dpi=150, bbox_inches="tight")
    plt.close(fig)
    return output_path


def extract_and_build_charts(text: str) -> tuple[list[dict[str, Any]], str]:
    specs = extract_chart_specs(text)
    return specs, json.dumps(specs_to_echarts_list(specs), ensure_ascii=False)
