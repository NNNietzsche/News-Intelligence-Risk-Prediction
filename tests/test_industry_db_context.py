"""行业库依赖清理在跨 Context 场景下不应再抛出次生异常。"""

from __future__ import annotations

import unittest
from contextvars import copy_context

from app.database.industry_db import _close_industry_session, current_industry_sector


class _FakeSession:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class IndustryDbContextTests(unittest.TestCase):
    def test_close_tolerates_value_set_in_another_context(self) -> None:
        session = _FakeSession()

        def enter() -> None:
            current_industry_sector.set("demo-sector")

        copy_context().run(enter)
        # FastAPI sync 依赖可能在不同 Context 中退出；关闭会话仍应成功。
        _close_industry_session(session)
        self.assertTrue(session.closed)


if __name__ == "__main__":
    unittest.main()
