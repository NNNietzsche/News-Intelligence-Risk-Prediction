"""命令行：初始化数据库；仅在 --demo 时显式写入演示条目。"""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.database.session import SessionLocal, init_db
from app.services.domain_rules import seed_default_domains
from app.services.data_bridge import migrate_legacy_data


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--demo",
        action="store_true",
        help="显式写入 Godiva/普洛斯演示条目；演示数据不参与预警计算",
    )
    args = parser.parse_args()
    init_db()
    db = SessionLocal()
    try:
        seed_default_domains(db)
        stats = migrate_legacy_data(db)
        if args.demo:
            from app.services.entity_mock import seed_entity_demo_data

            demo = seed_entity_demo_data(db, force=False)
            print(f"数据库初始化完成；显式演示数据: {demo}")
        else:
            print(f"数据库初始化完成；未写入演示数据: {stats}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
