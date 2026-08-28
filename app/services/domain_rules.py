"""域名白名单/黑名单数据库访问。"""

from sqlalchemy.orm import Session

from app.database.models import DomainBlacklist, DomainWhitelist


def get_active_whitelist(db: Session, module_code: str | None = None) -> list[str]:
    q = db.query(DomainWhitelist).filter(DomainWhitelist.is_active.is_(True))
    if module_code:
        q = q.filter(
            (DomainWhitelist.module_code == module_code)
            | (DomainWhitelist.module_code.is_(None))
            | (DomainWhitelist.module_code == "")
        )
    return [row.domain for row in q.all()]


def get_active_blacklist(db: Session) -> list[str]:
    rows = db.query(DomainBlacklist).filter(DomainBlacklist.is_active.is_(True)).all()
    return [row.domain for row in rows]


def seed_default_domains(db: Session) -> None:
    """预置权威来源域名（可在管理界面扩展）。"""
    defaults_whitelist = [
        ("tdnet.info", "C"),
        ("disclosure.edinet-fsa.go.jp", "C"),
        ("jpx.co.jp", "C"),
        ("mitsubishicorp.com", "C"),
        ("mitsui.com", "C"),
        ("itochu.co.jp", "C"),
        ("sumitomocorp.com", "C"),
        ("marubeni.com", "C"),
        ("denso.com", "C"),
        ("nyk.com", "C"),
        ("daiwa-grp.jp", "C"),
        ("daiwa.jp", "C"),
        ("mufg.jp", "C"),
        ("smfg.co.jp", "C"),
        ("mizuho-fg.co.jp", "C"),
        ("nomuraholdings.com", "C"),
        ("nomura.com", "C"),
        ("smbcnikko.co.jp", "C"),
        ("digital.go.jp", None),
        ("courts.go.jp", "A"),
        ("reuters.com", None),
        ("bloomberg.com", None),
        ("gov.cn", None),
    ]
    defaults_blacklist = [
        ("reddit.com", "社交媒体噪声"),
        ("twitter.com", "非白名单社交"),
        ("x.com", "非白名单社交"),
    ]
    for domain, mod in defaults_whitelist:
        if not db.query(DomainWhitelist).filter(DomainWhitelist.domain == domain).first():
            db.add(DomainWhitelist(domain=domain, module_code=mod, note="系统预置"))
    for domain, reason in defaults_blacklist:
        if not db.query(DomainBlacklist).filter(DomainBlacklist.domain == domain).first():
            db.add(DomainBlacklist(domain=domain, reason=reason))
    db.commit()
