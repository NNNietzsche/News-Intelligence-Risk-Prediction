"""python-docx 中文日报导出。"""

from __future__ import annotations

from datetime import date, datetime
from io import BytesIO
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import urlparse

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Inches, Pt, RGBColor

from app.config import MODULE_CODES, NEWS_WINDOW_HOURS_24, news_window_label
from app.database.models import DailyRiskEntry, EntityRisk, IndustryReport, TargetEntity
from app.services.chart_generator import extract_chart_specs, render_chart_png
from app.services.citation_validation import CITATION_RE
from app.services.cjk_fonts import word_east_asia_font

# 24小时核心新闻情报汇总 — 视觉规范色
COLOR_PRIMARY = RGBColor(0x1B, 0x36, 0x5D)  # 深蓝标题/分割线
COLOR_TITLE = RGBColor(0x2C, 0x3E, 0x50)  # 新闻标题
COLOR_META = RGBColor(0x7F, 0x8C, 0x8D)  # 元信息/副标题灰
COLOR_BODY = RGBColor(0x33, 0x33, 0x33)
COLOR_DIVIDER = "BDC3C7"


def _set_run_font(
    run,
    size_pt: float = 11,
    bold: bool = False,
    italic: bool = False,
    color: Optional[RGBColor] = None,
):
    font_name = word_east_asia_font()
    run.font.name = font_name
    run._element.rPr.rFonts.set(qn("w:eastAsia"), font_name)
    run.font.size = Pt(size_pt)
    run.font.bold = bold
    run.font.italic = italic
    if color:
        run.font.color.rgb = color


def _add_heading(doc: Document, text: str, level: int = 1) -> None:
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(12 if level == 1 else 8)
    p.paragraph_format.space_after = Pt(6)
    run = p.add_run(text)
    sizes = {1: 18, 2: 14, 3: 12}
    _set_run_font(run, size_pt=sizes.get(level, 12), bold=True, color=COLOR_PRIMARY)


def _add_body(doc: Document, text: str, indent: bool = False) -> None:
    p = doc.add_paragraph()
    if indent:
        p.paragraph_format.left_indent = Pt(18)
    p.paragraph_format.line_spacing = 1.25
    run = p.add_run(text)
    _set_run_font(run, size_pt=11, color=COLOR_BODY)


def _add_meta_line(doc: Document, label: str, value: str) -> None:
    p = doc.add_paragraph()
    r1 = p.add_run(f"{label}：")
    _set_run_font(r1, bold=True, color=RGBColor(0x44, 0x44, 0x44))
    r2 = p.add_run(value)
    _set_run_font(r2, color=COLOR_BODY)


def _add_light_divider(doc: Document) -> None:
    """浅灰色细分割线（段后间距约 6pt）。"""
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(2)
    p.paragraph_format.space_after = Pt(6)
    p_pr = p._p.get_or_add_pPr()
    p_bdr = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    bottom.set(qn("w:val"), "single")
    bottom.set(qn("w:sz"), "6")
    bottom.set(qn("w:space"), "1")
    bottom.set(qn("w:color"), COLOR_DIVIDER)
    p_bdr.append(bottom)
    p_pr.append(p_bdr)


def _add_hyperlink(paragraph, text: str, url: str, size_pt: float = 9.5) -> None:
    """在段落中写入可点击外链（蓝字下划线，非斜体）。"""
    r_id = paragraph.part.relate_to(url, RT.HYPERLINK, is_external=True)
    hyperlink = OxmlElement("w:hyperlink")
    hyperlink.set(qn("r:id"), r_id)

    run = OxmlElement("w:r")
    r_pr = OxmlElement("w:rPr")

    font_name = word_east_asia_font()
    r_fonts = OxmlElement("w:rFonts")
    r_fonts.set(qn("w:ascii"), font_name)
    r_fonts.set(qn("w:hAnsi"), font_name)
    r_fonts.set(qn("w:eastAsia"), font_name)
    r_pr.append(r_fonts)

    sz = OxmlElement("w:sz")
    sz.set(qn("w:val"), str(int(size_pt * 2)))
    r_pr.append(sz)
    sz_cs = OxmlElement("w:szCs")
    sz_cs.set(qn("w:val"), str(int(size_pt * 2)))
    r_pr.append(sz_cs)

    color = OxmlElement("w:color")
    color.set(qn("w:val"), "0563C1")
    r_pr.append(color)

    u = OxmlElement("w:u")
    u.set(qn("w:val"), "single")
    r_pr.append(u)

    run.append(r_pr)
    text_el = OxmlElement("w:t")
    text_el.set(qn("xml:space"), "preserve")
    text_el.text = text
    run.append(text_el)
    hyperlink.append(run)
    paragraph._p.append(hyperlink)


def _source_label(entry: DailyRiskEntry) -> str:
    if entry.source_title and entry.source_title.strip():
        return entry.source_title.strip()
    if entry.source_url:
        host = urlparse(entry.source_url).netloc
        if host:
            return host.removeprefix("www.")
    return "未知来源"


def _format_published_at(entry: DailyRiskEntry, fallback_date: date) -> str:
    if entry.published_at:
        return entry.published_at.strftime("%Y-%m-%d %H:%M")
    return fallback_date.isoformat()


def _category_tag(entry: DailyRiskEntry, module_name: str) -> str:
    parts: list[str] = []
    if entry.pillar_or_topic:
        parts.append(entry.pillar_or_topic)
    elif entry.risk_category:
        parts.append(entry.risk_category)
    if module_name and module_name not in parts:
        parts.append(module_name)
    return " / ".join(parts) if parts else "未分类"


def build_daily_report_docx(
    entries: Iterable[DailyRiskEntry],
    report_date: date,
    institution_name: str = "风险管理部",
    module_codes: Optional[list[str]] = None,
    window_hours: int = NEWS_WINDOW_HOURS_24,
) -> Document:
    """生成核心新闻情报汇总（仅标题/元数据/概要，不含风险研判与图表）。"""
    _ = institution_name  # 新版版式不再展示机构署名

    doc = Document()
    section = doc.sections[0]
    # 标准 Word 页边距：上下 2.54cm，左右 3.18cm
    section.top_margin = Cm(2.54)
    section.bottom_margin = Cm(2.54)
    section.left_margin = Cm(3.18)
    section.right_margin = Cm(3.18)

    if module_codes:
        codes = [c.upper() for c in module_codes if c.upper() in MODULE_CODES]
        module_map = {c: MODULE_CODES[c] for c in codes}
    else:
        module_map = dict(MODULE_CODES)

    items = [e for e in entries if e.module_code in module_map]
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M")
    report_title = f"{news_window_label(window_hours)}核心新闻情报汇总"

    title_p = doc.add_paragraph()
    title_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    title_p.paragraph_format.space_after = Pt(4)
    tr = title_p.add_run(report_title)
    _set_run_font(tr, size_pt=22, bold=True, color=COLOR_PRIMARY)

    sub = doc.add_paragraph()
    sub.alignment = WD_ALIGN_PARAGRAPH.CENTER
    sub.paragraph_format.space_after = Pt(10)
    sr = sub.add_run(f"生成时间：{generated_at}  |  资讯总量：{len(items)} 条")
    _set_run_font(sr, size_pt=9, color=COLOR_META)

    # 标题下主色细分割线
    rule = doc.add_paragraph()
    rule.paragraph_format.space_before = Pt(0)
    rule.paragraph_format.space_after = Pt(12)
    rule_pr = rule._p.get_or_add_pPr()
    rule_bdr = OxmlElement("w:pBdr")
    rule_bottom = OxmlElement("w:bottom")
    rule_bottom.set(qn("w:val"), "single")
    rule_bottom.set(qn("w:sz"), "12")
    rule_bottom.set(qn("w:space"), "1")
    rule_bottom.set(qn("w:color"), "1B365D")
    rule_bdr.append(rule_bottom)
    rule_pr.append(rule_bdr)

    if not items:
        empty = doc.add_paragraph()
        empty.alignment = WD_ALIGN_PARAGRAPH.CENTER
        er = empty.add_run("本日暂无核心资讯条目。")
        _set_run_font(er, size_pt=10.5, color=COLOR_META)
        return doc

    for idx, e in enumerate(items, start=1):
        module_name = module_map.get(e.module_code, e.module_code)

        # 第一行：序号与标题（保持不变）
        title_line = doc.add_paragraph()
        title_line.paragraph_format.space_before = Pt(4)
        title_line.paragraph_format.space_after = Pt(2)
        tr_item = title_line.add_run(f"{idx}. {e.title}")
        _set_run_font(tr_item, size_pt=12, bold=True, color=COLOR_TITLE)

        # 第二行：来源 / 发布时间 / 分类标签（内容不变，正文体浅灰）
        meta = doc.add_paragraph()
        meta.paragraph_format.space_before = Pt(0)
        meta.paragraph_format.space_after = Pt(2)
        meta_text = (
            f"来源：{_source_label(e)}  |  "
            f"发布时间：{_format_published_at(e, report_date)}  |  "
            f"分类标签：{_category_tag(e, module_name)}"
        )
        mr = meta.add_run(meta_text)
        _set_run_font(mr, size_pt=10.5, color=COLOR_META)

        # 第三行：内容概要
        summary = doc.add_paragraph()
        summary.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
        summary.paragraph_format.line_spacing = 1.25
        summary.paragraph_format.line_spacing_rule = WD_LINE_SPACING.MULTIPLE
        summary.paragraph_format.first_line_indent = Pt(21)  # 10.5pt × 2 字符
        summary.paragraph_format.space_before = Pt(0)
        summary.paragraph_format.space_after = Pt(2)
        sr_body = summary.add_run(e.summary or "")
        _set_run_font(sr_body, size_pt=10.5, color=COLOR_BODY)

        # 第四行：来源链接（稍小、非斜体、可点击超链接）
        link_p = doc.add_paragraph()
        link_p.paragraph_format.space_before = Pt(0)
        link_p.paragraph_format.space_after = Pt(4)
        label_run = link_p.add_run("来源链接：")
        _set_run_font(label_run, size_pt=9.5, color=COLOR_META)
        if e.source_url:
            _add_hyperlink(link_p, e.source_url, e.source_url, size_pt=9.5)
        else:
            empty_run = link_p.add_run("暂无")
            _set_run_font(empty_run, size_pt=9.5, color=COLOR_META)

        _add_light_divider(doc)

    footer = doc.add_paragraph()
    footer.alignment = WD_ALIGN_PARAGRAPH.CENTER
    footer.paragraph_format.space_before = Pt(8)
    fr = footer.add_run("— 内部资料，未经许可不得对外传播 —")
    _set_run_font(fr, size_pt=9, color=COLOR_META)

    return doc

def _add_cited_body(doc: Document, text: str, citation_context: dict[str, Any]) -> None:
    p = doc.add_paragraph()
    p.paragraph_format.line_spacing = 1.25
    cursor = 0
    for match in CITATION_RE.finditer(text or ""):
        if match.start() > cursor:
            _set_run_font(p.add_run(text[cursor:match.start()]), size_pt=11, color=COLOR_BODY)
        code = match.group(1)
        number = citation_context["number_map"].get(code)
        shown = f"[{number}]" if code in citation_context["valid_codes"] and number else "[引用异常]"
        run = p.add_run(shown)
        _set_run_font(run, size_pt=8.5, bold=True, color=RGBColor(0x1D, 0x4E, 0x89))
        run.font.superscript = True
        cursor = match.end()
    if cursor < len(text or ""):
        _set_run_font(p.add_run(text[cursor:]), size_pt=11, color=COLOR_BODY)


def _add_report_metadata(doc: Document, report: IndustryReport, grounded: bool) -> None:
    _add_heading(doc, "报告信息", level=2)
    _add_meta_line(doc, "报告版本", f"v{report.version}")
    _add_meta_line(doc, "生成模式", "证据约束（grounded）" if grounded else "传统生成（legacy）")
    _add_meta_line(doc, "生成时间", report.updated_at.strftime("%Y-%m-%d %H:%M") if report.updated_at else "未记录")
    if grounded:
        _add_meta_line(doc, "Prompt版本", report.prompt_version or "未记录")
        _add_meta_line(doc, "引用验证", report.citation_validation_status or "未记录")
        _add_meta_line(doc, "晋升方式", report.promotion_type or "未记录")
        if report.promotion_note:
            _add_meta_line(doc, "审批备注", report.promotion_note)
    else:
        p = doc.add_paragraph()
        run = p.add_run("本报告不是证据约束报告，正文没有经过逐条引用绑定与导出前证据复核。")
        _set_run_font(run, size_pt=10.5, bold=True, color=RGBColor(0x9A, 0x34, 0x12))


def _display_url(value: str) -> str:
    # Zero-width opportunities let Word wrap long URLs without changing the target shown to users.
    return value.replace("/", "/\u200b").replace("?", "?\u200b").replace("&", "&\u200b")


def _add_grounded_appendices(doc: Document, context: dict[str, Any]) -> None:
    doc.add_page_break()
    _add_heading(doc, "附录一：引用与来源", level=1)
    for item in context["citations"]:
        _add_heading(doc, f"[{item['display_number']}] {item['source_name']}", level=2)
        if item.get("source_publisher"):
            _add_meta_line(doc, "发布机构", item["source_publisher"])
        _add_meta_line(doc, "来源类型", item.get("source_origin") or "未记录")
        _add_meta_line(doc, "证据等级", item.get("evidence_grade") or "未记录")
        _add_meta_line(doc, "原文位置", item.get("locator") or "未记录")
        if item.get("published_at"):
            _add_meta_line(doc, "发布日期", item["published_at"])
        if item.get("retrieved_at"):
            _add_meta_line(doc, "获取时间", item["retrieved_at"])
        if item.get("url"):
            _add_meta_line(doc, "网页地址", _display_url(item["url"]))
        _add_meta_line(doc, "原文摘录", item.get("original_quote") or "未记录")

    doc.add_page_break()
    _add_heading(doc, "附录二：冲突与限制", level=1)
    reported_conflicts = [str(item) for item in context.get("unresolved_conflicts") or []]
    for item in reported_conflicts:
        _add_body(doc, f"• {item}")
    seen: set[str] = set()
    for item in context["citations"]:
        for conflict in item.get("related_conflicts") or []:
            code = str(conflict.get("conflict_code") or "")
            if not code or code in seen:
                continue
            seen.add(code)
            _add_heading(doc, f"{code} · {conflict.get('status') or '未记录'}", level=2)
            _add_body(doc, str(conflict.get("description") or ""))
            if conflict.get("resolution_note"):
                _add_meta_line(doc, "处理说明", str(conflict["resolution_note"]))
    if not seen and not reported_conflicts:
        _add_body(doc, "当前引用未关联已登记的跨信源冲突。")
    limitations = list(context.get("limitations") or [])
    source_limitations = sorted({
        limitation
        for item in context["citations"] for limitation in item.get("limitations") or []
    })
    _add_heading(doc, "资料与方法限制", level=2)
    for limitation in limitations + source_limitations:
        _add_body(doc, f"• {limitation}")
    if not limitations and not source_limitations:
        _add_body(doc, "未登记额外限制。")

    doc.add_page_break()
    _add_heading(doc, "附录三：证据覆盖", level=1)
    coverage = context.get("coverage") or {}
    labels = {
        "verified_evidence_count": "可用验证证据数",
        "independent_source_count": "独立来源数",
        "partial_text_count": "部分正文证据数",
        "unresolved_conflict_count": "未解决冲突数",
        "resolved_conflict_count": "已处理冲突数",
        "network_lead_count": "网络线索数",
        "missing_topic_count": "缺失主题数",
        "factual_citation_coverage": "事实句引用覆盖率",
    }
    shown = False
    for key, label in labels.items():
        if key in coverage:
            value = coverage[key]
            if key == "factual_citation_coverage" and isinstance(value, (int, float)):
                value = f"{value:.1%}"
            _add_meta_line(doc, label, str(value))
            shown = True
    missing_topics = coverage.get("missing_topics") or []
    if missing_topics:
        _add_meta_line(doc, "缺失主题", "、".join(str(item) for item in missing_topics))
        shown = True
    if not shown:
        _add_body(doc, "没有可展示的覆盖统计。")


def _source_kind_label(source: Any) -> str:
    origin = str(getattr(source, "source_origin", None) or "")
    source_type = str(getattr(source, "source_type", None) or "")
    if origin == "network_search" or source_type == "network_search":
        return "网络搜索"
    if origin == "customer_url" or source_type == "url":
        return "网址"
    if origin == "customer_file" or source_type == "file":
        return "文件"
    return source_type or "数据源"


def _add_legacy_source_list(doc: Document, sources: Iterable[Any]) -> None:
    _add_heading(doc, "来源列表", level=1)
    items = list(sources or [])
    if not items:
        _add_body(doc, "本次生成未写入可用数据源。")
        return
    for idx, src in enumerate(items, 1):
        name = str(getattr(src, "name", None) or "未命名来源")
        kind = _source_kind_label(src)
        url = str(getattr(src, "url", None) or "").strip()
        _add_body(doc, f"{idx}. {kind} · {name}")
        if url:
            link_p = doc.add_paragraph()
            _add_hyperlink(link_p, url, url, size_pt=9.5)


def build_industry_report_docx(
    report: IndustryReport,
    citation_context: Optional[dict[str, Any]] = None,
    sources: Optional[Iterable[Any]] = None,
) -> Document:
    import json

    doc = Document()
    title_p = doc.add_paragraph()
    title_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    tr = title_p.add_run(f"{report.industry_name} — 行业分析报告")
    _set_run_font(tr, size_pt=20, bold=True, color=RGBColor(0x0F, 0x17, 0x2A))

    if report.company_name:
        sub = doc.add_paragraph()
        sub.alignment = WD_ALIGN_PARAGRAPH.CENTER
        sr = sub.add_run(f"分析对象: {report.company_name}")
        _set_run_font(sr, size_pt=11, color=RGBColor(0x55, 0x55, 0x55))

    grounded = report.generation_mode == "grounded"
    if grounded and citation_context is None:
        raise ValueError("grounded_report_requires_validated_citation_context")
    _add_report_metadata(doc, report, grounded)

    payload = {}
    if report.report_json:
        try:
            payload = json.loads(report.report_json)
        except json.JSONDecodeError:
            payload = {}

    if payload.get("summary"):
        _add_heading(doc, "执行摘要", level=1)
        if grounded:
            _add_cited_body(doc, str(payload["summary"]), citation_context)
        else:
            _add_body(doc, str(payload["summary"]))
    for sec in payload.get("sections") or []:
        if isinstance(sec, dict):
            _add_heading(doc, str(sec.get("heading", "")), level=2)
            if grounded:
                _add_cited_body(doc, str(sec.get("content", "")), citation_context)
            else:
                _add_body(doc, str(sec.get("content", "")))
    if payload.get("risk_outlook"):
        _add_heading(doc, "风险展望", level=1)
        if grounded:
            _add_cited_body(doc, str(payload["risk_outlook"]), citation_context)
        else:
            _add_body(doc, str(payload["risk_outlook"]))

    if payload.get("key_metrics"):
        _add_heading(doc, "关键指标", level=1)
        for metric in payload["key_metrics"]:
            if not isinstance(metric, dict):
                continue
            text = f"{metric.get('name', '')}：{metric.get('value', '')}"
            code = str(metric.get("evidence_code") or "")
            if grounded and code:
                text += f"[{code}]"
                _add_cited_body(doc, text, citation_context)
            else:
                _add_body(doc, text)

    if report.chart_specs:
        try:
            charts = json.loads(report.chart_specs)
        except json.JSONDecodeError:
            charts = []
        for idx, item in enumerate(charts):
            opt = item.get("option") if isinstance(item, dict) else None
            if not opt:
                continue
            spec = {
                "type": (opt.get("series") or [{}])[0].get("type", "bar"),
                "title": (opt.get("title") or {}).get("text", "图表"),
                "labels": (opt.get("xAxis") or {}).get("data", []),
                "series": [{"name": s.get("name", ""), "data": s.get("data", [])} for s in opt.get("series", [])],
            }
            img_path = render_chart_png(spec, Path(f"data/exports/charts/industry_{report.id}_{idx}.png"))
            if img_path and img_path.is_file():
                doc.add_picture(str(img_path), width=Inches(5.5))

    if grounded:
        _add_grounded_appendices(doc, citation_context)
    elif sources is not None:
        _add_legacy_source_list(doc, sources)

    footer = doc.add_paragraph()
    footer.alignment = WD_ALIGN_PARAGRAPH.CENTER
    fr = footer.add_run("— 内部资料，未经许可不得对外传播 —")
    _set_run_font(fr, size_pt=9, color=RGBColor(0x77, 0x77, 0x77))
    return doc


def export_industry_report_to_path(
    report: IndustryReport, output_path: Path,
    citation_context: Optional[dict[str, Any]] = None,
    sources: Optional[Iterable[Any]] = None,
) -> Path:
    doc = build_industry_report_docx(
        report, citation_context=citation_context, sources=sources,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(output_path))
    return output_path


def export_daily_report_to_path(
    entries: list[DailyRiskEntry],
    report_date: date,
    output_path: Path,
    institution_name: str = "风险管理部",
    module_codes: Optional[list[str]] = None,
    window_hours: int = NEWS_WINDOW_HOURS_24,
) -> Path:
    doc = build_daily_report_docx(
        entries,
        report_date,
        institution_name,
        module_codes=module_codes,
        window_hours=window_hours,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(output_path))
    return output_path


def export_daily_report_to_bytes(
    entries: list[DailyRiskEntry],
    report_date: date,
    institution_name: str = "风险管理部",
    module_codes: Optional[list[str]] = None,
    window_hours: int = NEWS_WINDOW_HOURS_24,
) -> bytes:
    doc = build_daily_report_docx(
        entries,
        report_date,
        institution_name,
        module_codes=module_codes,
        window_hours=window_hours,
    )
    buffer = BytesIO()
    doc.save(buffer)
    return buffer.getvalue()


def build_entity_assessment_docx(
    entity: TargetEntity,
    *,
    report_date: date,
    risks: Iterable[EntityRisk],
    institution_name: str = "风险管理部",
) -> Document:
    """生成《企业公开信息风险监测简报》。"""
    doc = Document()
    section = doc.sections[0]
    section.top_margin = Pt(72)
    section.bottom_margin = Pt(72)
    section.left_margin = Pt(72)
    section.right_margin = Pt(72)

    display = entity.display_name or entity.name

    title_p = doc.add_paragraph()
    title_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    tr = title_p.add_run("企业公开信息风险监测简报")
    _set_run_font(tr, size_pt=22, bold=True, color=RGBColor(0x0F, 0x17, 0x2A))

    sub = doc.add_paragraph()
    sub.alignment = WD_ALIGN_PARAGRAPH.CENTER
    sr = sub.add_run(f"{institution_name} | 评估日期 {report_date.isoformat()}")
    _set_run_font(sr, size_pt=11, color=RGBColor(0x55, 0x55, 0x55))

    doc.add_paragraph()
    _add_heading(doc, "一、主体概况", level=1)
    _add_meta_line(doc, "主体名称", display)
    if entity.industry:
        _add_meta_line(doc, "所属行业", entity.industry)
    if entity.region:
        _add_meta_line(doc, "区域", entity.region)
    _add_meta_line(doc, "当前舆情预警灯号", entity.credit_level or "正常")
    _add_meta_line(doc, "监控状态", entity.monitor_status or "active")
    _add_body(
        doc,
        "声明：本简报仅汇总公开信息并触发人工复核，不构成客户内部信用评级、授信审批意见或金融资产风险分类。",
        indent=True,
    )

    _add_heading(doc, "二、近三个月公开信息事件", level=1)
    risk_list = list(risks)
    if not risk_list:
        _add_body(doc, "近期暂无风险事件。", indent=True)
    else:
        for idx, e in enumerate(risk_list, start=1):
            _add_heading(doc, f"{idx}. {e.title}", level=2)
            _add_meta_line(doc, "报告日期", e.report_date.isoformat())
            _add_meta_line(
                doc,
                "源发布时间",
                e.published_at.isoformat(sep=" ", timespec="minutes")
                if e.published_at
                else "待核验",
            )
            _add_meta_line(doc, "信用风险信号级别", e.risk_level)
            if e.news_importance:
                _add_meta_line(doc, "资讯重要度", e.news_importance)
            if e.risk_category:
                _add_meta_line(doc, "风险类别", e.risk_category)
            _add_meta_line(doc, "主体相关性", e.relevance or "unknown")
            _add_meta_line(doc, "数据血缘", e.provenance or "real")
            _add_meta_line(doc, "核心摘要", e.summary or "")
            if e.impact_analysis:
                _add_meta_line(doc, "AI 风险影响", e.impact_analysis)
            if e.source_url:
                _add_meta_line(
                    doc,
                    "数据来源",
                    f"{e.source_name or '未命名来源'} | {e.source_url}",
                )

    footer = doc.add_paragraph()
    footer.alignment = WD_ALIGN_PARAGRAPH.CENTER
    fr = footer.add_run("— 内部资料，未经许可不得对外传播 —")
    _set_run_font(fr, size_pt=9, color=RGBColor(0x77, 0x77, 0x77))
    return doc


def export_entity_assessment_to_path(
    entity: TargetEntity,
    *,
    report_date: date,
    risks: list[EntityRisk],
    output_path: Path,
    institution_name: str = "风险管理部",
) -> Path:
    doc = build_entity_assessment_docx(
        entity,
        report_date=report_date,
        risks=risks,
        institution_name=institution_name,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(output_path))
    return output_path
