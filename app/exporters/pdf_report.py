"""日报 PDF 导出（中文 CID 字体，保留来源链接文本）。"""

from datetime import date
from pathlib import Path

from app.config import MODULE_CODES


def export_daily_pdf(entries, report_date: date, output_path: Path) -> Path:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import cm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer

    output_path.parent.mkdir(parents=True, exist_ok=True)
    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    styles = getSampleStyleSheet()
    title = ParagraphStyle("cn-title", parent=styles["Title"], fontName="STSong-Light", fontSize=18, leading=25)
    head = ParagraphStyle("cn-head", parent=styles["Heading2"], fontName="STSong-Light", fontSize=13, leading=19, spaceBefore=12)
    body = ParagraphStyle("cn-body", parent=styles["BodyText"], fontName="STSong-Light", fontSize=9.5, leading=15)
    story = [Paragraph(f"每日风险情报汇总 - {report_date.isoformat()}", title), Spacer(1, 0.3 * cm)]
    grouped = {}
    for row in entries:
        grouped.setdefault(row.module_code, []).append(row)
    for code, rows in grouped.items():
        story.append(Paragraph(MODULE_CODES.get(code, code), head))
        for row in rows:
            story.append(Paragraph(f"<b>标题：</b>{row.title or '未命名'}", body))
            story.append(Paragraph(f"<b>概况：</b>{row.summary or '暂无'}", body))
            story.append(Paragraph(f"<b>风险点提示：</b>{row.impact_analysis or '暂无'}", body))
            if row.source_url:
                story.append(Paragraph(f"<b>来源：</b>{row.source_title or row.source_url}<br/>{row.source_url}", body))
            story.append(Spacer(1, 0.25 * cm))
    SimpleDocTemplate(str(output_path), pagesize=A4, leftMargin=1.6*cm, rightMargin=1.6*cm, topMargin=1.5*cm, bottomMargin=1.5*cm).build(story)
    return output_path
