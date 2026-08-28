"""一次性清理新闻日报越界旧数据。"""

from app.database.models import DailyRiskEntry, NewsArticle
from app.database.session import SessionLocal
from app.services.news_section_router import item_in_module_scope

db = SessionLocal()
try:
    rows = db.query(NewsArticle).filter(NewsArticle.module_code.in_(("B", "C", "D"))).all()
    removed = 0
    for row in rows:
        content = f"{row.summary or ''} {row.impact_analysis or ''}"
        ok, reason = item_in_module_scope(
            row.module_code,
            title=row.title or "",
            content=content,
            source=str(row.source_title or row.source_url or ""),
            related_company=str(row.related_company or ""),
        )
        if ok:
            continue
        title_safe = (row.title or "").encode("ascii", "replace").decode("ascii")[:48]
        reason_safe = reason.encode("ascii", "replace").decode("ascii")[:40]
        print("DEL", row.module_code, title_safe, "|", reason_safe)
        legacy_id = row.legacy_entry_id
        row.legacy_entry_id = None
        db.flush()
        db.delete(row)
        db.flush()
        if legacy_id:
            db.query(DailyRiskEntry).filter(DailyRiskEntry.id == legacy_id).delete(
                synchronize_session=False
            )
        removed += 1
    db.commit()
    print(f"removed {removed} of {len(rows)}")
finally:
    db.close()
